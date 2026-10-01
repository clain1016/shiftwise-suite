"""Legacy app patches must still intercept delivery after the R1 refactor."""
import importlib
import urllib
from unittest.mock import MagicMock, patch

import pytest

import app as appmod

notify_module = importlib.import_module("shiftwise.notify")
roster_module = importlib.import_module("shiftwise.routes.roster")


@pytest.mark.parametrize("replace_module", [False, True])
def test_legacy_smtp_patch_intercepts_delivery(monkeypatch, replace_module):
    monkeypatch.setenv("SHIFTWISE_SMTP_HOST", "smtp.example.test")
    monkeypatch.setenv("SHIFTWISE_SMTP_FROM", "schedule@example.test")
    original = notify_module._smtplib
    # A broken facade must fail locally, never try a real SMTP connection.
    with patch("smtplib.SMTP", side_effect=AssertionError("unmocked SMTP")):
        target, name = (appmod, "smtplib") if replace_module else (appmod.smtplib, "SMTP")
        with patch.object(target, name) as replacement:
            appmod.send_schedule_link("email", "alex@example.test", "Alex",
                                      "https://staff.example.test/login")
            smtp = replacement.SMTP if replace_module else replacement
            smtp.assert_called_once()
            server = smtp.return_value.__enter__.return_value
            assert server.send_message.call_args.args[0]["To"] == "alex@example.test"
    assert notify_module._smtplib is original


@pytest.mark.parametrize("replace_module", [False, True])
def test_legacy_urllib_patch_intercepts_delivery(monkeypatch, replace_module):
    monkeypatch.setenv("SHIFTWISE_TWILIO_ACCOUNT_SID", "ACtest")
    monkeypatch.setenv("SHIFTWISE_TWILIO_AUTH_TOKEN", "test-token")
    monkeypatch.setenv("SHIFTWISE_TWILIO_FROM", "+15550000000")
    original = notify_module._urllib
    with patch("urllib.request.urlopen", side_effect=AssertionError("unmocked HTTP")):
        if replace_module:
            target, name = appmod, "urllib"
            replacement = MagicMock(wraps=urllib)
            replacement.request = MagicMock(wraps=urllib.request)
            replacement.request.urlopen = MagicMock()
            replacement.error = urllib.error
            urlopen = replacement.request.urlopen
        else:
            target, name = appmod.urllib.request, "urlopen"
            replacement = urlopen = MagicMock()
        urlopen.return_value.__enter__.return_value.status = 201
        with patch.object(target, name, replacement):
            appmod.send_schedule_link("sms", "+15551234567", "Alex",
                                      "https://staff.example.test/login")
            urlopen.assert_called_once()
            request = urlopen.call_args.args[0]
            assert urllib.parse.parse_qs(request.data.decode())["To"] == ["+15551234567"]
    assert notify_module._urllib is original


def test_legacy_sender_patch_intercepts_roster_and_restores(isolated_db, monkeypatch):
    appmod.init_db(seed_demo=True)
    monkeypatch.setitem(appmod.app.config, "SHIFTWISE_PUBLIC_URL", "https://staff.example.test")
    conn = appmod.db()
    conn.execute("UPDATE users SET email=? WHERE username='alex'", ("alex@example.test",))
    uid = conn.execute("SELECT id FROM users WHERE username='alex'").fetchone()[0]
    conn.commit()
    conn.close()
    client = appmod.app.test_client()
    client.post("/login", data={"username": "manager", "password": "manager"})
    original = roster_module.send_schedule_link
    with patch.object(appmod, "send_schedule_link") as sender:
        # Prevent external calls if the legacy patch fails to reach the route.
        with patch("smtplib.SMTP", side_effect=AssertionError("unmocked SMTP")), \
                patch("urllib.request.urlopen", side_effect=AssertionError("unmocked HTTP")):
            response = client.post(f"/manager/roster/{uid}/send-link",
                                   data={"channel": "email"}, follow_redirects=True)
        sender.assert_called_once_with("email", "alex@example.test", "Alex Rivera",
                                       "https://staff.example.test/login")
        assert b"Schedule link sent by email" in response.data
    assert roster_module.send_schedule_link is original
