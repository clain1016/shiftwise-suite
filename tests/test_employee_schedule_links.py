import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import app as appmod


class EmployeeScheduleLinkTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="shiftwise-links-")
        appmod.DB_PATH = Path(self.temporary.name) / "scheduler.db"
        appmod.init_db(seed_demo=True)
        self.client = appmod.app.test_client()
        self.client.post("/login", data={"username": "manager", "password": "manager"})
        appmod.app.config["SHIFTWISE_PUBLIC_URL"] = "https://staff.example.test"

    def tearDown(self):
        appmod.app.config.pop("SHIFTWISE_PUBLIC_URL", None)
        self.temporary.cleanup()

    def employee_id(self, username="alex"):
        conn = appmod.db()
        row = conn.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
        conn.close()
        return row["id"]

    def test_roster_saves_employee_email_and_phone(self):
        uid = self.employee_id()
        response = self.client.post("/manager/roster", data={
            f"type_{uid}": "full_time", f"hired_{uid}": "2021-03-01",
            f"cap_{uid}": "30", f"station_{uid}": "front",
            f"email_{uid}": "alex@example.test", f"phone_{uid}": "+15551234567",
        }, follow_redirects=True)
        self.assertIn(b"Roster updated", response.data)
        conn = appmod.db()
        row = conn.execute("SELECT email, phone FROM users WHERE id=?", (uid,)).fetchone()
        conn.close()
        self.assertEqual(row["email"], "alex@example.test")
        self.assertEqual(row["phone"], "+15551234567")

    def test_new_employee_can_be_added_with_contact_details(self):
        response = self.client.post("/manager/roster/add", data={
            "username": "newhire", "name": "New Hire",
            "password": "a-private-long-password", "employment_type": "part_time",
            "weekly_hours": "20", "email": "newhire@example.test",
            "phone": "+15551234567",
        }, follow_redirects=True)
        self.assertIn(b"Employee added", response.data)
        conn = appmod.db()
        row = conn.execute(
            "SELECT email, phone FROM users WHERE username='newhire'").fetchone()
        conn.close()
        self.assertEqual(tuple(row), ("newhire@example.test", "+15551234567"))

    def test_roster_page_displays_contact_fields_and_send_actions(self):
        response = self.client.get("/manager/roster")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Email link", response.data)
        self.assertIn(b"Text link", response.data)
        self.assertIn(b"Phone", response.data)

    def test_manager_can_send_employee_schedule_link_by_email(self):
        uid = self.employee_id()
        conn = appmod.db()
        conn.execute("UPDATE users SET email=? WHERE id=?", ("alex@example.test", uid))
        conn.commit()
        conn.close()
        with patch.object(appmod, "send_schedule_link", return_value="email") as send:
            response = self.client.post(
                f"/manager/roster/{uid}/send-link", data={"channel": "email"},
                follow_redirects=True)
        self.assertIn(b"Schedule link sent by email", response.data)
        send.assert_called_once_with("email", "alex@example.test", "Alex Rivera",
                                     "https://staff.example.test/login")

    def test_employee_cannot_send_schedule_link(self):
        uid = self.employee_id()
        self.client.get("/logout")
        self.client.post("/login", data={"username": "alex", "password": "alex"})
        with patch.object(appmod, "send_schedule_link") as send:
            response = self.client.post(f"/manager/roster/{uid}/send-link",
                                        data={"channel": "email"})
        self.assertEqual(response.status_code, 403)
        send.assert_not_called()

    def test_email_delivery_uses_configured_smtp(self):
        with patch.dict(os.environ, {
                "SHIFTWISE_SMTP_HOST": "smtp.example.test",
                "SHIFTWISE_SMTP_FROM": "schedule@example.test",
                "SHIFTWISE_SMTP_PORT": "587"}), \
                patch.object(appmod.smtplib, "SMTP") as smtp:
            appmod.send_schedule_link("email", "alex@example.test", "Alex",
                                      "https://staff.example.test/login")
        server = smtp.return_value.__enter__.return_value
        server.starttls.assert_called_once_with()
        sent = server.send_message.call_args.args[0]
        self.assertEqual(sent["To"], "alex@example.test")
        self.assertIn("https://staff.example.test/login", sent.get_content())

    def test_text_delivery_uses_configured_twilio(self):
        with patch.dict(os.environ, {
                "SHIFTWISE_TWILIO_ACCOUNT_SID": "ACtest",
                "SHIFTWISE_TWILIO_AUTH_TOKEN": "test-token",
                "SHIFTWISE_TWILIO_FROM": "+15550000000"}), \
                patch.object(appmod.urllib.request, "urlopen") as urlopen:
            urlopen.return_value.__enter__.return_value.status = 201
            appmod.send_schedule_link("sms", "+15551234567", "Alex",
                                      "https://staff.example.test/login")
        request_obj = urlopen.call_args.args[0]
        self.assertIn("api.twilio.com/2010-04-01/Accounts/ACtest/Messages.json",
                      request_obj.full_url)
        body = appmod.urllib.parse.parse_qs(request_obj.data.decode())["Body"][0]
        self.assertIn("https://staff.example.test/login", body)

    def test_invalid_contact_details_are_rejected_when_saving_roster(self):
        uid = self.employee_id()
        response = self.client.post("/manager/roster", data={
            f"type_{uid}": "full_time", f"hired_{uid}": "2021-03-01",
            f"cap_{uid}": "30", f"station_{uid}": "front",
            f"email_{uid}": "not-an-email", f"phone_{uid}": "5551234567",
        }, follow_redirects=True)
        self.assertIn(b"valid email address", response.data)


if __name__ == "__main__":
    unittest.main()
