"""Manager conflict resolution and schedule override routes."""
from collections import defaultdict
from datetime import date, datetime, timedelta

from flask import (
    Blueprint,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)

from shiftwise.auth import login_required
from shiftwise.db import db, monday_of
from shiftwise.domain.rules import priority_key
from shiftwise.notify import notify
from shiftwise.scheduler.engine import run_scheduler

conflicts_bp = Blueprint("conflicts", __name__)


@conflicts_bp.route("/manager/conflicts")
@login_required(role="manager")
def conflicts():
    """Shifts where picks (at the same rank) exceed open slots, with
    claimants sorted by the priority lineup (FT first, then seniority)."""
    week = request.args.get("week", "") or monday_of(date.today()).isoformat()
    try:
        week = monday_of(date.fromisoformat(week)).isoformat()
    except ValueError:
        week = monday_of(date.today()).isoformat()
    conn = db()
    shifts = conn.execute(
        "SELECT * FROM shifts WHERE week_start=? ORDER BY id", (week,)).fetchall()
    conflict_list = []
    for s in shifts:
        claimants = conn.execute(
            "SELECT p.user_id, p.rank, u.name, u.employment_type, u.hired_on, "
            "u.weekly_hours, u.station FROM picks p JOIN users u ON u.id=p.user_id "
            "WHERE p.shift_id=? AND u.station=? ORDER BY p.rank",
            (s["id"], s["area"])).fetchall()
        if not claimants:
            continue
        assigned_rows = conn.execute(
            "SELECT u.name, u.id, a.status FROM assignments a "
            "JOIN users u ON u.id=a.user_id WHERE a.shift_id=? "
            "AND a.status NOT IN ('sick','swap_requested')", (s["id"],)).fetchall()
        assigned_ids = {r["id"] for r in assigned_rows}
        contested = [c for c in claimants if c["user_id"] not in assigned_ids]
        if len(assigned_rows) >= s["slots"] and not contested:
            continue
        # same-rank ties are the true conflicts: group contested claimants by rank
        by_rank = defaultdict(list)
        for c in contested:
            by_rank[c["rank"]].append(c)
        rank_conflicts = []
        open_slots = s["slots"] - len(assigned_rows)
        for rank in sorted(by_rank):
            group = by_rank[rank]
            # a rank group is a conflict when its claimants exceed the
            # slots still open at that point in the lineup; fitting groups
            # consume their slots so later ranks see what's left
            if len(group) > open_slots:
                rank_conflicts.append({
                    "rank": rank,
                    "claimants": sorted(group, key=lambda c: priority_key(c)),
                    "slots_needed": len(group),
                    "open_slots": max(open_slots, 0),
                })
                open_slots = 0
            else:
                open_slots -= len(group)
        if rank_conflicts:
            conflict_list.append({
                "shift": s, "assigned": assigned_rows,
                "ranks": rank_conflicts,
                "open_slots": s["slots"] - len(assigned_rows),
            })
    pending_picks = conn.execute(
        "SELECT COUNT(DISTINCT p.user_id) c FROM picks p "
        "JOIN shifts s ON s.id=p.shift_id WHERE s.week_start=? "
        "AND p.user_id NOT IN (SELECT user_id FROM assignments a "
        "JOIN shifts s2 ON s2.id=a.shift_id WHERE s2.week_start=?)",
        (week, week)).fetchone()["c"]
    conn.close()
    conn2 = db()
    all_emps = conn2.execute(
        "SELECT id, name, station FROM users WHERE role='employee' ORDER BY name"
    ).fetchall()
    conn2.close()
    d = date.fromisoformat(week)
    return render_template(
        "conflicts.html", conflicts=conflict_list, week=week,
        employees=all_emps,
        prev_week=(d - timedelta(days=7)).isoformat(),
        next_week=(d + timedelta(days=7)).isoformat(),
        this_week=monday_of(date.today()).isoformat(),
        employees_without_shifts=pending_picks)


@conflicts_bp.route("/manager/unassign/<int:shift_id>/<int:user_id>", methods=["POST"])
@login_required(role="manager")
def unassign(shift_id, user_id):
    conn = db()
    shift = conn.execute("SELECT week_start FROM shifts WHERE id=?",
                         (shift_id,)).fetchone()
    deleted = conn.execute("DELETE FROM assignments WHERE shift_id=? AND user_id=?",
                           (shift_id, user_id)).rowcount
    if deleted:
        conn.execute(
            "INSERT INTO requests (user_id, kind, shift_id, status, created_at) "
            "VALUES (?,?,?,?,?)",
            (user_id, "manager_unassign", shift_id, "approved_ok",
             datetime.now().isoformat(timespec="seconds")))
        notify(conn, user_id, "conflict",
               "One of your shifts was reassigned by the manager — check your schedule.")
    conn.commit()
    conn.close()
    if deleted and shift:
        run_scheduler(shift["week_start"])
    return redirect(url_for("conflicts"))


@conflicts_bp.route("/manager/assign/<int:shift_id>/<int:user_id>", methods=["POST"])
@login_required(role="manager")
def manager_assign(shift_id, user_id):
    """MANAGER OVERRIDE: placing any employee onto shifts in their house."""
    conn = db()
    shift = conn.execute("SELECT * FROM shifts WHERE id=?", (shift_id,)).fetchone()
    user = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if not shift or not user or user["role"] != "employee":
        conn.close()
        flash("Invalid shift or employee.")
        return redirect(url_for("conflicts"))
    if user["station"] != shift["area"]:
        conn.close()
        flash(f"{user['name']} works "
              f"{'front' if user['station'] == 'front' else 'back'} of house — "
              "they can only be placed on shifts in their own house.")
        return redirect(url_for("conflicts"))
    staffed = conn.execute(
        "SELECT COUNT(*) c FROM assignments WHERE shift_id=? "
        "AND status NOT IN ('sick','swap_requested')",
        (shift_id,)).fetchone()["c"]
    if staffed >= shift["slots"] and not conn.execute(
            "SELECT 1 FROM assignments WHERE shift_id=? AND user_id=?",
            (shift_id, user_id)).fetchone():
        conn.close()
        flash(f"Can't override: {shift['day']} is already full. Unassign "
              "someone first to reopen a slot.")
        return redirect(url_for("conflicts"))
    override_day_off = False
    if conn.execute(
            "SELECT 1 FROM requests WHERE user_id=? AND kind='day_off' AND "
            "day=? AND status='approved_ok'", (user_id, shift["day"])).fetchone():
        conn.execute(
            "UPDATE requests SET status='overridden_by_manager' WHERE "
            "user_id=? AND kind='day_off' AND day=? AND status='approved_ok'",
            (user_id, shift["day"]))
        override_day_off = True
    conn.execute("DELETE FROM picks WHERE user_id=? AND shift_id=?",
                 (user_id, shift_id))
    conn.execute("DELETE FROM assignments WHERE shift_id=? AND user_id=?",
                 (shift_id, user_id))
    conn.execute(
        "INSERT INTO assignments (shift_id, user_id, status) VALUES (?,?,"
        "'manager_fixed')", (shift_id, user_id))
    notify(conn, user_id, "assignment",
           f"Manager placed you on {shift['day']} "
           f"{shift['start_time']}-{shift['end_time']}."
           + (" This overrides a day-off you had approved." if override_day_off else ""))
    conn.commit()
    conn.close()
    run_scheduler(shift["week_start"])
    flash(f"Override: {user['name']} placed on {shift['day']} "
          f"{shift['start_time']}-{shift['end_time']} (fixed — the "
          "auto-scheduler won't move them).")
    return redirect(url_for("conflicts"))
