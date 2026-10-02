"""Coverage planning and sick leave handler."""

from collections import defaultdict

from shiftwise.domain.rules import (
    assignment_block_reason,
    priority_key,
    shift_hours,
)
from shiftwise.notify import notify


def coverage_plan(conn, week, out_shift_id, out_uid, allow_over_limits=False):
    """Pick the next-in-line coverer for a shift its holder is leaving.

    Only considers coverers whose station matches the shift's area
    (front-of-house shifts are covered by front staff, back-of-house
    shifts by back staff) — a FOH/BOH employee is never pulled across.

    Candidates: other employees who picked this shift but didn't get it
    (in priority-lineup order), then anyone with room in their week
    (hours cap + days-off rule) preferring fewest assigned hours, so the
    least-loaded person covers first. Returns a user id or None if
    nobody can legally take it.
    """
    shift = conn.execute("SELECT * FROM shifts WHERE id=?", (out_shift_id,)).fetchone()
    if not shift:
        return None
    employees = conn.execute(
        "SELECT * FROM users WHERE role='employee' AND station=? ORDER BY id", (shift["area"],)
    ).fetchall()
    # current week hours + working days for every employee, excluding the
    # shift being covered (its holder's hours leave with them) and any
    # existing 'sick' rows (a sick assignment counts for nobody)
    arows = conn.execute(
        "SELECT a.shift_id, a.user_id, a.status, s.start_time, s.end_time, s.day "
        "FROM assignments a JOIN shifts s ON s.id=a.shift_id "
        "WHERE s.week_start=? AND a.shift_id!=?",
        (week, out_shift_id),
    ).fetchall()
    hours = defaultdict(float)
    for r in arows:
        if r["status"] in ("sick", "swap_requested"):
            continue
        hours[r["user_id"]] += shift_hours(r["start_time"], r["end_time"])
    # people already on this shift are not cover candidates (slots are 1
    # per person — UNIQUE(shift_id, user_id))
    already_on = {
        r["user_id"]
        for r in conn.execute("SELECT user_id FROM assignments WHERE shift_id=?", (out_shift_id,))
    }

    def can_cover(u):
        uid = u["id"]
        if uid in already_on:
            return False
        preference = conn.execute(
            "SELECT willing FROM coverage_preferences WHERE user_id=? AND shift_id=?",
            (uid, out_shift_id),
        ).fetchone()
        if preference and not preference["willing"]:
            return False
        return (
            assignment_block_reason(conn, uid, shift, allow_over_limits=allow_over_limits) is None
        )

    # 1. people who picked this shift but didn't get it — best in lineup.
    # With out_uid=None (backfill pass) every picker is eligible to be
    # considered; already assigned people are filtered by can_cover.
    picker_ids = {
        r["user_id"]
        for r in conn.execute(
            "SELECT DISTINCT user_id FROM picks WHERE shift_id=?", (out_shift_id,)
        )
        if r["user_id"] != out_uid
    }
    for u in sorted((u for u in employees if u["id"] in picker_ids), key=priority_key):
        if can_cover(u):
            return u["id"]
    # 2. fall back: least-loaded employee with room
    for u in sorted(employees, key=lambda u: hours.get(u["id"], 0)):
        if out_uid is not None and u["id"] == out_uid:
            continue
        if can_cover(u):
            return u["id"]
    return None


def apply_sick(conn, uid, shift_id, week=None):
    """Mark an assignment sick and auto-assign a cover.

    The sick employee keeps the row (status='sick'); the coverer gets
    the shift as 'notified' + a notification. If nobody can legally
    cover, the manager gets an alert.
    """
    a = conn.execute(
        "SELECT * FROM assignments WHERE shift_id=? AND user_id=?", (shift_id, uid)
    ).fetchone()
    if not a or a["status"] == "sick":
        return False
    shift = conn.execute("SELECT * FROM shifts WHERE id=?", (shift_id,)).fetchone()
    if not shift:
        return False
    week = shift["week_start"]
    conn.execute(
        "UPDATE assignments SET status='sick' WHERE shift_id=? AND user_id=?", (shift_id, uid)
    )
    notify(
        conn,
        uid,
        "conflict",
        f"Sick call logged for {shift['day']} "
        f"{shift['start_time']}-{shift['end_time']} — get well soon.",
    )
    cover_uid = coverage_plan(conn, week, shift_id, uid)
    if cover_uid:
        conn.execute(
            "INSERT INTO assignments (shift_id, user_id) VALUES (?,?) ON CONFLICT DO NOTHING",
            (shift_id, cover_uid),
        )
        conn.execute(
            "UPDATE assignments SET status='notified' WHERE shift_id=? AND user_id=? "
            "AND status='proposed'",
            (shift_id, cover_uid),
        )
        notify(
            conn,
            cover_uid,
            "assignment",
            f"Coverage: you're now on {shift['day']} "
            f"{shift['start_time']}-{shift['end_time']} (covering a sick call).",
        )
        return True
    mgr = conn.execute("SELECT id FROM users WHERE role='manager'").fetchone()
    if mgr:
        notify(
            conn,
            mgr["id"],
            "conflict",
            f"No cover available for {shift['day']} "
            f"{shift['start_time']}-{shift['end_time']} — needs manual coverage.",
        )
    return False
