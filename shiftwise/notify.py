"""In-app notifications subsystem."""
from datetime import datetime


def notify(conn, user_id, kind, message, shift_id=None):
    """Insert an in-app notification for a user."""
    conn.execute(
        "INSERT INTO notifications (user_id, shift_id, kind, message, created_at) "
        "VALUES (?,?,?,?,?)",
        (user_id, shift_id, kind, message, datetime.now().isoformat(timespec="seconds")),
    )
