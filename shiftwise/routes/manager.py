"""Manager administration routes: shifts, requests, and scheduling triggers."""
from collections import defaultdict
from datetime import date, datetime, timedelta

from flask import (
    Blueprint,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from shiftwise.auth import login_required
from shiftwise.db import db, monday_of
from shiftwise.domain.constants import DAYS
from shiftwise.domain.rules import assignment_block_reason
from shiftwise.notify import notify
from shiftwise.scheduler.coverage import coverage_plan
from shiftwise.scheduler.engine import run_scheduler

manager_bp = Blueprint("manager", __name__)


@manager_bp.route("/manager")
@login_required(role="manager")
def manager():
    conn = db()
    week = monday_of(date.today()).isoformat()
    shifts = conn.execute(
        "SELECT * FROM shifts WHERE week_start=? ORDER BY id", (week,)).fetchall()
    picks = conn.execute(
        "SELECT p.rank, u.name, s.day, s.start_time, s.end_time FROM picks p "
        "JOIN users u ON u.id=p.user_id JOIN shifts s ON s.id=p.shift_id "
        "WHERE s.week_start=? ORDER BY u.name, p.rank", (week,)).fetchall()
    assigned = defaultdict(list)
    label = {"notified": "scheduled", "switch_fixed": "scheduled (switched)",
             "coverage_fixed": "scheduled (covering)", "manager_fixed": "MANAGER-SET"}
    for r in conn.execute(
            "SELECT a.shift_id, u.name, a.status FROM assignments a "
            "JOIN users u ON u.id=a.user_id "
            "WHERE a.status NOT IN ('sick','swap_requested')"):
        assigned[r["shift_id"]].append(f"{r['name']} ({label.get(r['status'], r['status'])})")
    unassigned = conn.execute(
        "SELECT DISTINCT u.name FROM users u WHERE u.role='employee' AND u.id NOT IN "
        "(SELECT a.user_id FROM assignments a JOIN shifts s ON s.id=a.shift_id "
        " WHERE s.week_start=?)", (week,)).fetchall()
    conn.close()
    front_shifts = [s for s in shifts if s["area"] == "front"]
    back_shifts = [s for s in shifts if s["area"] == "back"]
    return render_template("manager.html", shifts=shifts, picks=picks, DAYS=DAYS,
                           assigned=assigned, week=week,
                           front_shifts=front_shifts, back_shifts=back_shifts,
                           unassigned=[u["name"] for u in unassigned])


@manager_bp.route("/manager/shift/add", methods=["POST"])
@login_required(role="manager")
def add_shift():
    area = request.form.get("area", "front")
    if area not in ("front", "back"):
        area = "front"
    day = request.form.get("day", "")
    start = request.form.get("start", "")
    end = request.form.get("end", "")
    try:
        start_time = datetime.strptime(start, "%H:%M").time()
        end_time = datetime.strptime(end, "%H:%M").time()
        slots = int(request.form.get("slots", ""))
    except ValueError:
        flash("Enter a valid shift time and slot count.")
        return redirect(url_for("manager"))
    if day not in DAYS or end_time <= start_time or not 1 <= slots <= 10:
        flash("Choose a day, an end time after the start, and 1–10 slots.")
        return redirect(url_for("manager"))
    conn = db()
    week = monday_of(date.today()).isoformat()
    start_text = start_time.strftime("%H:%M")
    end_text = end_time.strftime("%H:%M")
    note = request.form.get("note") or None
    areas = (area, "back" if area == "front" else "front")
    for shift_area in areas:
        exists = conn.execute(
            "SELECT 1 FROM shifts WHERE week_start=? AND day=? AND start_time=? "
            "AND end_time=? AND area=? LIMIT 1",
            (week, day, start_text, end_text, shift_area)).fetchone()
        if exists:
            continue
        conn.execute(
            "INSERT INTO shifts (week_start, day, start_time, end_time, slots, note, area) "
            "VALUES (?,?,?,?,?,?,?)",
            (week, day, start_text, end_text, slots, note, shift_area))
        for emp in conn.execute(
                "SELECT id FROM users WHERE role='employee' AND station=?",
                (shift_area,)):
            notify(conn, emp["id"], "new_schedule",
                   f"New {day} shift posted ({start}-{end})"
                   f" for your area — submit your picks!")
    conn.commit()
    conn.close()
    run_scheduler(week)
    flash("Shift added and employees notified.")
    return redirect(url_for("manager.manager"))


@manager_bp.route("/manager/shift/delete/<int:shift_id>", methods=["POST"])
@login_required(role="manager")
def delete_shift(shift_id):
    conn = db()
    shift = conn.execute("SELECT week_start FROM shifts WHERE id=?",
                         (shift_id,)).fetchone()
    conn.execute(
        "UPDATE requests SET status='superseded' WHERE "
        "(shift_id=? OR target_shift_id=?) AND "
        "kind IN ('swap','switch','manager_unassign','sick')",
        (shift_id, shift_id))
    conn.execute("DELETE FROM picks WHERE shift_id=?", (shift_id,))
    conn.execute("DELETE FROM assignments WHERE shift_id=?", (shift_id,))
    conn.execute("DELETE FROM shifts WHERE id=?", (shift_id,))
    conn.commit()
    conn.close()
    if shift:
        run_scheduler(shift["week_start"])
    return redirect(url_for("manager"))


@manager_bp.route("/manager/run", methods=["POST"])
@login_required(role="manager")
def run():
    n = run_scheduler(monday_of(date.today()).isoformat())
    flash(f"Scheduler ran over {n} picks — check assignments below.")
    return redirect(url_for("manager"))


@manager_bp.route("/manager/requests")
@login_required(role="manager")
def requests():
    conn = db()
    rows = conn.execute(
        "SELECT r.*, u.name FROM requests r JOIN users u ON u.id=r.user_id "
        "WHERE r.kind!='manager_unassign' ORDER BY r.id DESC").fetchall()
    items = []
    for r in rows:
        item = dict(r)
        if r["shift_id"]:
            s = conn.execute("SELECT day, start_time, end_time FROM shifts WHERE id=?",
                             (r["shift_id"],)).fetchone()
            if s:
                item["shift_desc"] = f"{s['day']} {s['start_time']}-{s['end_time']}"
        if r["target_shift_id"]:
            s = conn.execute("SELECT day, start_time, end_time FROM shifts WHERE id=?",
                             (r["target_shift_id"],)).fetchone()
            if s:
                item["target_desc"] = f"{s['day']} {s['start_time']}-{s['end_time']}"
        if r["kind"] == "vacation" and r["status"] == "approved":
            start = date.fromisoformat(r["vacation_start"])
            end = date.fromisoformat(r["vacation_end"])
            gaps = []
            for shift in conn.execute(
                    "SELECT * FROM shifts WHERE week_start>=? AND week_start<=?",
                    (monday_of(start).isoformat(), end.isoformat())):
                shift_date = date.fromisoformat(shift["week_start"]) + timedelta(
                    days=DAYS.index(shift["day"]))
                if not start <= shift_date <= end:
                    continue
                staffed = conn.execute(
                    "SELECT COUNT(*) FROM assignments WHERE shift_id=? "
                    "AND status NOT IN ('sick','swap_requested')",
                    (shift["id"],)).fetchone()[0]
                if staffed < shift["slots"]:
                    gaps.append(f"{shift['day']} {shift['start_time']}-{shift['end_time']} "
                                f"({shift['slots'] - staffed} open)")
            item["coverage_gaps"] = gaps
        items.append(item)
    conn.close()
    return render_template("requests.html", items=items)


@manager_bp.route("/manager/requests/<int:req_id>/approve", methods=["POST"])
@login_required(role="manager")
def approve_request(req_id):
    conn = db()
    r = conn.execute("SELECT * FROM requests WHERE id=?", (req_id,)).fetchone()
    if not r or r["status"] != "approved":
        conn.close()
        flash("Request not found or already handled.")
        return redirect(url_for("manager.requests"))
    if r["kind"] == "day_off":
        conn.execute("UPDATE requests SET status='approved_ok' WHERE id=?", (req_id,))
        notify(conn, r["user_id"], "assignment",
               f"Your day-off request for {r['day']} was approved.")
    elif r["kind"] == "vacation":
        start = date.fromisoformat(r["vacation_start"])
        end = date.fromisoformat(r["vacation_end"])
        gaps = []
        for shift in conn.execute(
                "SELECT * FROM shifts WHERE week_start>=? AND week_start<=?",
                (monday_of(start).isoformat(), end.isoformat())):
            shift_date = date.fromisoformat(shift["week_start"]) + timedelta(
                days=DAYS.index(shift["day"]))
            if not start <= shift_date <= end:
                continue
            staffed = conn.execute(
                "SELECT COUNT(*) FROM assignments WHERE shift_id=? "
                "AND status NOT IN ('sick','swap_requested')",
                (shift["id"],)).fetchone()[0]
            if staffed < shift["slots"]:
                gaps.append(f"{shift['day']} {shift['start_time']}-{shift['end_time']}")
        if gaps:
            conn.close()
            flash("Vacation can't be approved until coverage is arranged for: "
                  + ", ".join(gaps) + ".")
            return redirect(url_for("manager.requests"))
        conn.execute("UPDATE requests SET status='approved_ok' WHERE id=?", (req_id,))
        notify(conn, r["user_id"], "assignment",
               f"Your vacation request for {r['vacation_start']} to "
               f"{r['vacation_end']} was approved.")
    elif r["kind"] == "swap":
        shift = conn.execute("SELECT * FROM shifts WHERE id=?", (r["shift_id"],)).fetchone()
        pending = conn.execute(
            "SELECT 1 FROM assignments WHERE shift_id=? AND user_id=? "
            "AND status='swap_requested'", (r["shift_id"], r["user_id"])).fetchone()
        cover_uid = (coverage_plan(conn, shift["week_start"], shift["id"], r["user_id"])
                     if shift and pending else None)
        if cover_uid is None:
            conn.close()
            flash("No eligible coverer is available for that swap.")
            return redirect(url_for("requests"))
        conn.execute("DELETE FROM assignments WHERE shift_id=? AND user_id=?",
                     (shift["id"], r["user_id"]))
        conn.execute("DELETE FROM picks WHERE shift_id=? AND user_id=?",
                     (shift["id"], r["user_id"]))
        conn.execute(
            "INSERT INTO assignments (shift_id, user_id, status) VALUES (?,?,?)",
            (shift["id"], cover_uid, "coverage_fixed"))
        conn.execute("UPDATE requests SET status='approved_ok' WHERE id=?", (req_id,))
        notify(conn, cover_uid, "assignment",
               f"Coverage: you're now on {shift['day']} "
               f"{shift['start_time']}-{shift['end_time']} (covering a swap).")
        notify(conn, r["user_id"], "assignment",
               f"Your swap for {shift['day']} {shift['start_time']}-{shift['end_time']} was covered.")
        conn.commit()
        conn.close()
        run_scheduler(shift["week_start"])
        flash("Swap covered.")
        return redirect(url_for("requests"))
    elif r["kind"] == "switch" and r["shift_id"] and r["target_shift_id"]:
        target = conn.execute("SELECT * FROM shifts WHERE id=?",
                              (r["target_shift_id"],)).fetchone()
        source = conn.execute(
            "SELECT s.* FROM assignments a JOIN shifts s ON s.id=a.shift_id "
            "WHERE a.user_id=? AND a.shift_id=? AND a.status "
            "NOT IN ('sick','swap_requested')",
            (r["user_id"], r["shift_id"])).fetchone()
        if target and source and target["week_start"] == source["week_start"] \
                and target["id"] != source["id"]:
            staffed = conn.execute(
                "SELECT COUNT(*) c FROM assignments WHERE shift_id=? "
                "AND status NOT IN ('sick','swap_requested')",
                (r["target_shift_id"],)).fetchone()["c"]
            block_reason = assignment_block_reason(
                conn, r["user_id"], target, exclude_shift_id=source["id"])
            if staffed >= target["slots"] or block_reason:
                conn.execute("UPDATE requests SET status='denied' WHERE id=?",
                             (req_id,))
                notify(conn, r["user_id"], "conflict",
                       f"Switch declined: {target['day']} "
                       f"{target['start_time']}-{target['end_time']} is unavailable.")
            else:
                conn.execute("DELETE FROM assignments WHERE shift_id=? AND user_id=?",
                             (r["shift_id"], r["user_id"]))
                conn.execute("DELETE FROM assignments WHERE shift_id=? AND user_id=?",
                             (r["target_shift_id"], r["user_id"]))
                conn.execute(
                    "INSERT OR IGNORE INTO assignments (shift_id, user_id) VALUES (?,?)",
                    (r["target_shift_id"], r["user_id"]))
                conn.execute(
                    "UPDATE assignments SET status='switch_fixed' WHERE shift_id=? "
                    "AND user_id=?", (r["target_shift_id"], r["user_id"]))
                conn.execute("DELETE FROM picks WHERE user_id=? AND shift_id=?",
                             (r["user_id"], r["shift_id"]))
                conn.execute("UPDATE requests SET status='approved_ok' WHERE id=?",
                             (req_id,))
                notify(conn, r["user_id"], "assignment",
                       f"Switch approved: you're now on {target['day']} "
                       f"{target['start_time']}-{target['end_time']}.")
        else:
            conn.execute("UPDATE requests SET status='denied' WHERE id=?", (req_id,))
        conn.commit()
        conn.close()
        run_scheduler(source["week_start"] if source else monday_of(date.today()).isoformat())
        flash("Switch request reviewed.")
        return redirect(url_for("requests"))
    conn.commit()
    conn.close()
    if r["kind"] == "day_off":
        run_scheduler(r["week_start"] or monday_of(date.today()).isoformat())
    flash("Request approved.")
    return redirect(url_for("requests"))


@manager_bp.route("/manager/requests/<int:req_id>/deny", methods=["POST"])
@login_required(role="manager")
def deny_request(req_id):
    conn = db()
    r = conn.execute("SELECT * FROM requests WHERE id=?", (req_id,)).fetchone()
    if not r or r["status"] != "approved":
        conn.close()
        flash("Request not found or already handled.")
        return redirect(url_for("requests"))
    conn.execute("UPDATE requests SET status='denied' WHERE id=?", (req_id,))
    if r["kind"] == "swap":
        conn.execute(
            "UPDATE assignments SET status='confirmed' WHERE shift_id=? "
            "AND user_id=? AND status='swap_requested'",
            (r["shift_id"], r["user_id"]))
    notify(conn, r["user_id"], "conflict",
           "One of your requests was declined by the manager — check the "
           "Requests page or talk to them.")
    conn.commit()
    conn.close()
    if r["kind"] == "day_off":
        run_scheduler(r["week_start"] or monday_of(date.today()).isoformat())
    elif r["kind"] == "vacation":
        conn = db()
        weeks = [w["week_start"] for w in conn.execute(
            "SELECT DISTINCT week_start FROM shifts WHERE week_start>=? "
            "AND week_start<=?",
            (monday_of(date.fromisoformat(r["vacation_start"])).isoformat(),
             r["vacation_end"]))]
        conn.close()
        for week in weeks:
            run_scheduler(week)
    elif r["kind"] == "swap":
        conn = db()
        shift = conn.execute("SELECT week_start FROM shifts WHERE id=?",
                             (r["shift_id"],)).fetchone()
        conn.close()
        if shift:
            run_scheduler(shift["week_start"])
    flash("Request denied.")
    return redirect(url_for("requests"))
