import re
from datetime import date, datetime, timedelta

from shiftwise.domain.constants import DAYS, MIN_DAYS_OFF


def valid_email(value):
    return not value or bool(re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value))


def valid_phone(value):
    return not value or bool(re.fullmatch(r"\+[1-9]\d{7,14}", value))


def unavailable_uids(conn, shift):
    """Employees with an active absence or swap request for this shift."""
    shift_date = (date.fromisoformat(shift["week_start"]) +
                  timedelta(days=DAYS.index(shift["day"]))).isoformat()
    return {r["user_id"] for r in conn.execute(
        "SELECT user_id FROM requests WHERE status IN ('approved', 'approved_ok') "
        "AND ((kind='vacation' AND vacation_start<=? AND vacation_end>=?) "
        "OR (kind='day_off' AND week_start=? AND day=?) "
        "OR (kind IN ('swap','manager_unassign') AND shift_id=?))",
        (shift_date, shift_date, shift["week_start"], shift["day"], shift["id"]))}


def shift_hours(s, e):
    """Calculate the duration in hours between start_time and end_time (HH:MM)."""
    sh, sm = map(int, s.split(":"))
    eh, em = map(int, e.split(":"))
    return (eh * 60 + em - sh * 60 - sm) / 60.0


def assignment_block_reason(conn, uid, shift, exclude_shift_id=None,
                            allow_over_limits=False):
    """Return the rule preventing a user from working a shift, if any."""
    if uid in unavailable_uids(conn, shift):
        return "unavailable"
    rows = conn.execute(
        "SELECT s.id, s.day, s.start_time, s.end_time FROM assignments a "
        "JOIN shifts s ON s.id=a.shift_id WHERE a.user_id=? AND s.week_start=? "
        "AND a.status NOT IN ('sick','swap_requested') AND s.id!=?",
        (uid, shift["week_start"], exclude_shift_id or -1)).fetchall()
    cap_row = conn.execute("SELECT weekly_hours FROM users WHERE id=?", (uid,)).fetchone()
    if not cap_row:
        return "unavailable"
    if not allow_over_limits and sum(
            shift_hours(r["start_time"], r["end_time"]) for r in rows) + \
            shift_hours(shift["start_time"], shift["end_time"]) > (cap_row[0] or 40):
        return "hours"
    days = {r["day"] for r in rows}
    if not allow_over_limits and shift["day"] not in days and \
            len(days) >= 7 - MIN_DAYS_OFF:
        return "days"
    for r in rows:
        if r["day"] == shift["day"] and \
                shift["start_time"] < r["end_time"] and \
                r["start_time"] < shift["end_time"]:
            return "overlap"
    return None


def priority_key(user_row):
    """Sort key for the priority lineup: front-of-house before back-of-house,
    then full-time before part-time, then earliest hire date first. Shared by
    the auto-scheduler and the conflict-resolution page so both sort
    identically."""
    seniority = 0 if not user_row["hired_on"] else (
        datetime.now() - datetime.fromisoformat(user_row["hired_on"])).days
    return ((0 if user_row["station"] == "front" else 1,
             0 if user_row["employment_type"] == "full_time" else 1,
             -seniority))
