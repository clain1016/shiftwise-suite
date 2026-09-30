"""Calendar view and container health check routes."""
from collections import defaultdict
from datetime import date, timedelta

from flask import (
    Blueprint,
    render_template,
    request,
    session,
)

from shiftwise.auth import login_required
from shiftwise.db import db, monday_of
from shiftwise.domain.constants import DAYS

calendar_bp = Blueprint("calendar", __name__)


@calendar_bp.route("/healthz")
def healthz():
    try:
        conn = db()
        # the users table only exists after init_db ran: a missing table
        # means the database was never initialized (sqlite auto-creates
        # the file on connect, so SELECT 1 alone would falsely report ok)
        conn.execute("SELECT 1 FROM users LIMIT 1").fetchone()
        conn.close()
        return {"status": "ok"}, 200
    except Exception:
        # never leak internal error details to unauthenticated callers
        return {"status": "error"}, 503


def calendar_days(conn, week, user_id, area=None):
    """7-day grid for a week: shifts per day, assignment status + staff for user_id."""
    if area in ("front", "back"):
        shifts = conn.execute(
            "SELECT * FROM shifts WHERE week_start=? AND area=? ORDER BY id",
            (week, area)).fetchall()
    else:
        shifts = conn.execute(
            "SELECT * FROM shifts WHERE week_start=? ORDER BY id", (week,)).fetchall()
    by_day = defaultdict(list)
    for s in shifts:
        rows = conn.execute(
            "SELECT a.status, a.user_id, u.name FROM assignments a "
            "JOIN users u ON u.id=a.user_id WHERE a.shift_id=? "
            "AND a.status NOT IN ('sick','swap_requested')",
            (s["id"],)).fetchall()
        status = next((r["status"] for r in rows if r["user_id"] == user_id), None)
        by_day[s["day"]].append({
            "id": s["id"],
            "time": f"{s['start_time']}–{s['end_time']}",
            "note": s["note"],
            "status": status,
            "mine": status is not None,
            "who": ", ".join(r["name"] for r in rows) or None,
        })
    start = date.fromisoformat(week)
    today = date.today().isoformat()
    return [{
        "name": DAYS[i],
        "date": (start + timedelta(days=i)).isoformat(),
        "label": (start + timedelta(days=i)).strftime("%b %d"),
        "shifts": by_day[DAYS[i]],
        "today": (start + timedelta(days=i)).isoformat() == today,
    } for i in range(7)]


@calendar_bp.route("/calendar")
@login_required()
def calendar_view():
    raw = request.args.get("week", "")
    try:
        week = monday_of(date.fromisoformat(raw)).isoformat()
    except ValueError:
        week = monday_of(date.today()).isoformat()
    conn = db()
    uid = session["uid"]
    current_user = conn.execute(
        "SELECT station FROM users WHERE id=?", (uid,)).fetchone()
    area = request.args.get("area")
    if area not in ("front", "back"):
        area = current_user["station"] if current_user else "front"
    employees = []
    if session["role"] == "manager":
        employees = conn.execute(
            "SELECT id, name FROM users WHERE role='employee' AND station=? ORDER BY name",
            (area,)).fetchall()
        req = request.args.get("user_id", "")
        if req.isdigit():
            row = conn.execute(
                "SELECT id FROM users WHERE id=? AND role='employee' AND station=?",
                (int(req), area)).fetchone()
            if row:
                uid = row["id"]
    view_user = conn.execute("SELECT name FROM users WHERE id=?", (uid,)).fetchone()
    days = calendar_days(conn, week, uid, area)
    conn.close()
    d = date.fromisoformat(week)
    return render_template(
        "calendar.html", days=days, week=week,
        view_name=view_user["name"] if view_user else session.get("name", ""),
        employees=employees, view_id=uid, area=area,
        prev_week=(d - timedelta(days=7)).isoformat(),
        next_week=(d + timedelta(days=7)).isoformat(),
        this_week=monday_of(date.today()).isoformat())
