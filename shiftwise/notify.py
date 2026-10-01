"""In-app notifications and external schedule link delivery."""
import base64
import os
import smtplib
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from email.message import EmailMessage

# Module-level handles for the delivery libraries. Tests patch these names
# (e.g. `patch("shiftwise.notify._smtplib")`) instead of reaching through the
# app facade (see docs/REFACTORING_BACKLOG.md R1).
_smtplib = smtplib
_urllib = urllib


def notify(conn, user_id, kind, message, shift_id=None):
    """Insert an in-app notification for a user."""
    conn.execute(
        "INSERT INTO notifications (user_id, shift_id, kind, message, created_at) "
        "VALUES (?,?,?,?,?)",
        (user_id, shift_id, kind, message, datetime.now().isoformat(timespec="seconds")),
    )


def send_schedule_link(channel, destination, employee_name, link):
    """Deliver a sign-in link through the configured SMTP or Twilio account."""
    message_text = (f"Hi {employee_name}, use this link to sign in to ShiftWise "
                    f"and submit your schedule preferences: {link}")
    if channel == "email":
        host = os.environ.get("SHIFTWISE_SMTP_HOST")
        sender = os.environ.get("SHIFTWISE_SMTP_FROM")
        if not host or not sender:
            raise ValueError("Email delivery is not configured (SMTP host/from missing).")
        msg = EmailMessage()
        msg["Subject"] = "Your ShiftWise schedule link"
        msg["From"] = sender
        msg["To"] = destination
        msg.set_content(message_text)
        port = int(os.environ.get("SHIFTWISE_SMTP_PORT", "587"))
        with _smtplib.SMTP(host, port, timeout=20) as server:
            server.starttls()
            username = os.environ.get("SHIFTWISE_SMTP_USER")
            password = os.environ.get("SHIFTWISE_SMTP_PASSWORD")
            if username:
                server.login(username, password or "")
            server.send_message(msg)
        return
    if channel == "sms":
        sid = os.environ.get("SHIFTWISE_TWILIO_ACCOUNT_SID")
        token = os.environ.get("SHIFTWISE_TWILIO_AUTH_TOKEN")
        sender = os.environ.get("SHIFTWISE_TWILIO_FROM")
        if not sid or not token or not sender:
            raise ValueError("Text delivery is not configured (Twilio credentials/from missing).")
        data = _urllib.parse.urlencode({"To": destination, "From": sender,
                                        "Body": message_text}).encode()
        request_obj = _urllib.request.Request(
            f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
            data=data, headers={"Authorization": "Basic " +
                                base64.b64encode(
                                    f"{sid}:{token}".encode()).decode()})
        try:
            with _urllib.request.urlopen(request_obj, timeout=20) as response:
                if response.status >= 300:
                    raise ValueError("Text provider rejected the message.")
        except _urllib.error.HTTPError as exc:
            raise ValueError("Text provider rejected the message.") from exc
        return
    raise ValueError("Choose email or text delivery.")
