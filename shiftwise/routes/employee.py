"""Employee self-service routes: dashboard, picks, swaps, and time-off requests."""
from collections import defaultdict
from datetime import date, datetime
import sqlite3

from flask import (
    Blueprint,
    abort,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

from shiftwise.auth import login_required
from shiftwise.db import db, monday_of
from shiftwise.domain.constants import DAYS
from shiftwise.domain.rules import assignment_block_reason, valid_email
from shiftwise.notify import notify
from shiftwise.scheduler.coverage import apply_sick, coverage_plan
from shiftwise.scheduler.engine import run_scheduler

employee_bp = Blueprint("employee", __name__)


@employee_bp.route("/settings", methods=["GET", "POST"])
@login_required()
def settings():
    conn = db()
    uid = session["uid"]
    if request.method == "POST":
        action = request.form.get("action", "clock")
        if action == "profile":
            name = request.form.get("name", "").strip()
            username = request.form.get("username", "").strip()
            email = request.form.get("email", "").strip()
            if not name or not username:
                conn.close()
                flash("Enter a display name and username.")
                return redirect(url_for("settings"))
            if not valid_email(email):
                conn.close()
                flash("Enter a valid email address.")
                return redirect(url_for("settings"))
            try:
                conn.execute(
                    "UPDATE users SET name=?, username=?, email=? WHERE id=?",
                    (name, username, email or None, uid),
                )
                conn.commit()
            except sqlite3.IntegrityError:
                conn.close()
                flash("That username is already in use.")
                return redirect(url_for("settings"))
            conn.close()
            session["name"] = name
            flash("Account details saved.")
            return redirect(url_for("settings"))
        if action == "password":
            current_password = request.form.get("current_password", "")
            new_password = request.form.get("new_password", "")
            user = conn.execute(
                "SELECT password FROM users WHERE id=?", (uid,)
            ).fetchone()
            if not user or not check_password_hash(user["password"], current_password):
                conn.close()
                flash("Current password is incorrect.")
                return redirect(url_for("settings") + "#password")
            if len(new_password) < 12:
                conn.close()
                flash("Use a new password of at least 12 characters.")
                return redirect(url_for("settings") + "#password")
            conn.execute(
                "UPDATE users SET password=? WHERE id=?",
                (generate_password_hash(new_password), uid),
            )
            conn.commit()
            conn.close()
            session.clear()
            flash("Password changed. Sign in again.")
            return redirect(url_for("login"))
        time_format = request.form.get("time_format", "")
        if time_format not in ("12h", "24h"):
            conn.close()
            flash("Choose either 12-hour or 24-hour time.")
            return redirect(url_for("settings"))
        conn.execute("UPDATE users SET time_format=? WHERE id=?", (time_format, uid))
        conn.commit()
        conn.close()
        flash("Clock format saved.")
        return redirect(url_for("settings"))
    user = conn.execute(
        "SELECT name, username, email, time_format FROM users WHERE id=?", (uid,)
    ).fetchone()
    conn.close()
    return render_template("settings.html", user=user, time_format=user["time_format"])


@employee_bp.route("/")
@login_required()
def dashboard():
    conn = db()
    week = monday_of(date.today()).isoformat()
    my = conn.execute("SELECT * FROM users WHERE id=?", (session["uid"],)).fetchone()
    # employees only see (and pick) shifts in their own house — FOH and BOH
    # are two separate schedules; managers see everything.
    if session["role"] == "manager":
        shifts = conn.execute(
            "SELECT * FROM shifts WHERE week_start=? ORDER BY id", (week,)).fetchall()
    else:
        shifts = conn.execute(
            "SELECT * FROM shifts WHERE week_start=? AND area=? ORDER BY id",
            (week, my["station"])).fetchall()
    my_assignments = {}
    for r in conn.execute(
            "SELECT shift_id, status FROM assignments WHERE user_id=?", (session["uid"],)):
        my_assignments[r["shift_id"]] = r["status"]
    my_picks = {r["shift_id"]: r["rank"] for r in conn.execute(
        "SELECT shift_id, rank FROM picks WHERE user_id=?", (session["uid"],))}
    cover_prefs = {r["shift_id"]: r["willing"] for r in conn.execute(
        "SELECT shift_id, willing FROM coverage_preferences WHERE user_id=?",
        (session["uid"],))}
    # who is on each shift + remaining capacity (sick rows don't count as staff)
    roster = defaultdict(list)
    for r in conn.execute(
            "SELECT a.shift_id, u.name FROM assignments a JOIN users u ON u.id=a.user_id "
            "WHERE a.status NOT IN ('sick','swap_requested')"):
        roster[r["shift_id"]].append(r["name"])
    notifs = conn.execute(
        "SELECT * FROM notifications WHERE user_id=? ORDER BY id DESC LIMIT 20",
        (session["uid"],)).fetchall()
    coworkers = conn.execute(
        "SELECT id, name FROM users WHERE role='employee' AND station=? AND id!=? ORDER BY name",
        (my["station"], session["uid"])).fetchall()
    coworker_shifts = conn.execute(
        "SELECT s.id, s.day, s.start_time, s.end_time, a.user_id, u.name "
        "FROM shifts s JOIN assignments a ON a.shift_id=s.id "
        "JOIN users u ON u.id=a.user_id WHERE s.week_start=? AND s.area=? "
        "AND a.status NOT IN ('sick','swap_requested','manager_fixed','swap_invited') "
        "AND u.role='employee' "
        "AND u.id!=? ORDER BY u.name, s.id",
        (week, my["station"], session["uid"])).fetchall()
    my_swappable_shifts = [
        shift for shift in shifts
        if my_assignments.get(shift["id"]) not in (None, "sick", "swap_requested",
                                                   "manager_fixed", "swap_invited")
    ]
    conn.execute("UPDATE notifications SET read=1 WHERE user_id=?", (session["uid"],))
    conn.commit()
    conn.close()
    return render_template("dashboard.html", shifts=shifts, DAYS=DAYS, week=week,
                           my_assignments=my_assignments, my_picks=my_picks,
                           my_station=my["station"], roster=roster, notifs=notifs,
                           cover_prefs=cover_prefs, coworkers=coworkers,
                           coworker_shifts=coworker_shifts,
                           my_swappable_shifts=my_swappable_shifts)


@employee_bp.route("/pick", methods=["POST"])
@login_required()
def pick():
    if session["role"] != "employee":
        flash("Picks are for employees — managers don't schedule themselves.")
        return redirect(url_for("dashboard"))
    conn = db()
    uid = session["uid"]
    week = monday_of(date.today()).isoformat()
    my_station = conn.execute("SELECT station FROM users WHERE id=?", (uid,)).fetchone()
    week_shifts = [r["id"] for r in conn.execute(
        "SELECT id FROM shifts WHERE week_start=? AND area=?",
        (week, my_station["station"] if my_station else "front"))]
    day_ranked = []
    for day in DAYS:
        value = request.form.get(f"rank_day_{day}")
        if not value or not value.isdigit() or not 1 <= int(value) <= 7:
            day_ranked = []
            break
        day_ranked.append((int(value), day))
    ranked = []
    if day_ranked:
        if sorted(rank for rank, _ in day_ranked) != list(range(1, 8)):
            conn.close()
            flash("Use each day rank 1–7 exactly once.")
            return redirect(url_for("dashboard"))
        day_order = [day for _, day in sorted(day_ranked)]
        shift_rows = conn.execute(
            "SELECT id, day FROM shifts WHERE week_start=? AND area=? ORDER BY id",
            (week, my_station["station"] if my_station else "front")).fetchall()
        per_day = {day: [] for day in DAYS}
        for shift in shift_rows:
            per_day[shift["day"]].append(shift["id"])
        rank = 1
        for day in day_order:
            for sid in per_day[day]:
                ranked.append((rank, sid))
                rank += 1
    else:
        # Keep accepting the legacy per-shift form for existing clients.
        missing = []
        for sid in week_shifts:
            value = request.form.get(f"rank_{sid}")
            if value and value.isdigit() and 1 <= int(value) <= len(week_shifts):
                ranked.append((int(value), int(sid)))
            else:
                missing.append(sid)
        if missing:
            conn.close()
            flash("Rank all seven days, from 1 (top choice) to 7.")
            return redirect(url_for("dashboard"))
        if sorted(rank for rank, _ in ranked) != list(range(1, len(week_shifts) + 1)):
            conn.close()
            flash("Use each shift rank exactly once so every shift has a backup.")
            return redirect(url_for("dashboard"))
    conn.execute("DELETE FROM picks WHERE user_id=?", (uid,))
    for sid in week_shifts:
        cover_values = request.form.getlist(f"cover_{sid}")
        willingness = ("yes" if "yes" in cover_values else
                       "no" if "no" in cover_values else None)
        if willingness in ("yes", "no"):
            conn.execute(
                "INSERT INTO coverage_preferences (user_id, shift_id, willing) VALUES (?,?,?) "
                "ON CONFLICT(user_id, shift_id) DO UPDATE SET willing=excluded.willing",
                (uid, sid, int(willingness == "yes")),
            )
    for rank, sid in ranked:
        conn.execute("INSERT OR REPLACE INTO picks (user_id, shift_id, rank) VALUES (?,?,?)",
                     (uid, sid, rank))
    conn.commit()
    conn.close()
    flash("Preferences saved. Shifts are assigned automatically by seniority "
          "and full-time priority.")
    # auto-rebuild: fresh picks -> re-run the lineup immediately
    run_scheduler(week)
    return redirect(url_for("dashboard"))


@employee_bp.route("/swap/<int:shift_id>", methods=["POST"])
@login_required()
def swap(shift_id):
    """Arrange an eligible cover or leave the slot open for manager review."""
    conn = db()
    uid = session["uid"]
    a = conn.execute(
        "SELECT * FROM assignments WHERE shift_id=? AND user_id=?",
        (shift_id, uid)).fetchone()
    if not a or a["status"] in ("sick", "swap_requested"):
        conn.close()
        flash("That shift can't be swapped right now.")
        return redirect(url_for("dashboard"))
    actor_rebuild = True  # rebuild after commit so it sees the deletions
    shift = conn.execute("SELECT * FROM shifts WHERE id=?", (shift_id,)).fetchone()
    cover_uid = coverage_plan(conn, shift["week_start"], shift_id, uid)
    if cover_uid:
        # fully replace the requester: row + pick gone, coverer in
        conn.execute("DELETE FROM assignments WHERE shift_id=? AND user_id=?",
                     (shift_id, uid))
        conn.execute("DELETE FROM picks WHERE user_id=? AND shift_id=?",
                     (uid, shift_id))
        conn.execute(
            "INSERT OR IGNORE INTO assignments (shift_id, user_id) VALUES (?,?)",
            (shift_id, cover_uid))
        conn.execute(
            "UPDATE assignments SET status='coverage_fixed' WHERE shift_id=? AND user_id=? "
            "AND status='proposed'", (shift_id, cover_uid))
        conn.execute(
            "INSERT INTO requests (user_id, kind, shift_id, status, created_at) "
            "VALUES (?,?,?,?,?)",
            (uid, "swap", shift_id, "approved_ok",
             datetime.now().isoformat(timespec="seconds")))
        coverer = conn.execute("SELECT name FROM users WHERE id=?", (cover_uid,)).fetchone()
        notify(conn, cover_uid, "assignment",
               f"Coverage: you're now on {shift['day']} "
               f"{shift['start_time']}-{shift['end_time']} (covering a swap).")
        notify(conn, uid, "assignment",
               f"Swap covered: {coverer['name']} is taking your {shift['day']} "
               f"{shift['start_time']}-{shift['end_time']} shift.")
        flash(f"Swap arranged — {coverer['name']} is covering that shift.")
    else:
        conn.execute("UPDATE assignments SET status='swap_requested' "
                     "WHERE shift_id=? AND user_id=?", (shift_id, uid))
        conn.execute(
            "INSERT INTO requests (user_id, kind, shift_id, status, created_at) "
            "VALUES (?,?,?,?,?)",
            (uid, "swap", shift_id, "approved",
             datetime.now().isoformat(timespec="seconds")))
        mgr = conn.execute("SELECT id FROM users WHERE role='manager'").fetchone()
        if mgr:
            notify(conn, mgr["id"], "swap_request",
                   f"{session['name']} requested a swap for {shift['day']} "
                   f"{shift['start_time']}-{shift['end_time']} but nobody can "
                   "cover it — needs manual coverage.")
        flash("Swap requested, but nobody is available to cover — the manager "
              "has been alerted.")
    # commit FIRST so the rebuild sees the deletions, then rebuild
    conn.commit()
    if actor_rebuild:
        run_scheduler(shift["week_start"])
    conn.close()
    return redirect(url_for("dashboard"))


@employee_bp.route("/request/vacation", methods=["POST"])
@login_required()
def request_vacation():
    if session["role"] != "employee":
        abort(403)
    vstart = request.form.get("vac_start", "").strip()
    vend = request.form.get("vac_end", "").strip()
    try:
        vs = date.fromisoformat(vstart)
        ve = date.fromisoformat(vend)
    except ValueError:
        flash("Enter both dates as YYYY-MM-DD.")
        return redirect(url_for("dashboard"))
    if ve < vs:
        flash("Vacation end must be on or after the start.")
        return redirect(url_for("dashboard"))
    now = date.today()
    if vs < now:
        flash("Vacation can't start in the past.")
        return redirect(url_for("dashboard"))
    conn = db()
    uid = session["uid"]
    conn.execute(
        "INSERT INTO requests (user_id, kind, vacation_start, vacation_end, created_at) "
        "VALUES (?,?,?,?,?)",
        (uid, "vacation", vs.isoformat(), ve.isoformat(),
         datetime.now().isoformat(timespec="seconds")))
    affected_weeks = [r["week_start"] for r in conn.execute(
        "SELECT DISTINCT week_start FROM shifts WHERE week_start>=? AND week_start<=?",
        (monday_of(vs).isoformat(), ve.isoformat()))]
    mgr = conn.execute("SELECT id FROM users WHERE role='manager'").fetchone()
    if mgr:
        notify(conn, mgr["id"], "swap_request",
               f"{session['name']} requested vacation {vs.isoformat()} to "
               f"{ve.isoformat()}.")
    conn.commit()
    conn.close()
    for week in affected_weeks:
        run_scheduler(week)
    flash(f"Vacation requested {vs.isoformat()} to {ve.isoformat()}.")
    return redirect(url_for("dashboard"))


@employee_bp.route("/request/day_off", methods=["POST"])
@login_required()
def request_day_off():
    if session["role"] != "employee":
        abort(403)
    day = request.form.get("day", "")
    if day not in DAYS:
        flash("Pick a valid day.")
        return redirect(url_for("dashboard"))
    conn = db()
    uid = session["uid"]
    week = monday_of(date.today()).isoformat()
    existing = conn.execute(
        "SELECT 1 FROM requests WHERE user_id=? AND kind='day_off' "
        "AND week_start=? AND day=? AND status IN ('approved', 'approved_ok')",
        (uid, week, day)).fetchone()
    if existing:
        conn.close()
        flash(f"You already have an active request for {day} off.")
        return redirect(url_for("dashboard"))
    conn.execute(
        "INSERT INTO requests (user_id, kind, day, week_start, created_at) "
        "VALUES (?,?,?,?,?)",
        (uid, "day_off", day, week, datetime.now().isoformat(timespec="seconds")))
    mgr = conn.execute("SELECT id FROM users WHERE role='manager'").fetchone()
    if mgr:
        notify(conn, mgr["id"], "swap_request",
               f"{session['name']} requested {day} off.")
    conn.commit()
    conn.close()
    run_scheduler(week)
    flash(f"Day off requested for {day}.")
    return redirect(url_for("dashboard"))


@employee_bp.route("/request/sick/<int:shift_id>", methods=["POST"])
@login_required()
def request_sick(shift_id):
    if session["role"] != "employee":
        abort(403)
    conn = db()
    ok = apply_sick(conn, session["uid"], shift_id)
    conn.commit()
    conn.close()
    flash("Sick call logged — coverage has been arranged."
          if ok else "Sick call logged.")
    return redirect(url_for("dashboard"))


@employee_bp.route("/request/switch/<int:shift_id>", methods=["POST"])
@login_required()
def request_switch(shift_id):
    if session["role"] != "employee":
        abort(403)
    target = request.form.get("target_shift")
    if not target or not target.isdigit():
        flash("Choose the shift you want to switch into.")
        return redirect(url_for("dashboard"))
    conn = db()
    uid = session["uid"]
    mine = conn.execute(
        "SELECT a.id, s.week_start FROM assignments a JOIN shifts s ON s.id=a.shift_id "
        "WHERE a.shift_id=? AND a.user_id=? "
        "AND a.status NOT IN ('sick','swap_requested')",
        (shift_id, uid)).fetchone()
    if not mine:
        conn.close()
        flash("That's not one of your shifts.")
        return redirect(url_for("dashboard"))
    target_row = conn.execute("SELECT * FROM shifts WHERE id=?", (int(target),)).fetchone()
    if not target_row or target_row["id"] == shift_id or \
            target_row["week_start"] != mine["week_start"]:
        conn.close()
        flash("Choose another shift in the same week.")
        return redirect(url_for("dashboard"))
    me = conn.execute("SELECT station FROM users WHERE id=?", (uid,)).fetchone()
    if me and target_row["area"] != me["station"]:
        conn.close()
        flash("Switches stay within your own house — you can't switch onto the other schedule.")
        return redirect(url_for("dashboard"))
    conn.execute(
        "INSERT INTO requests (user_id, kind, shift_id, target_shift_id, created_at) "
        "VALUES (?,?,?,?,?)",
        (uid, "switch", shift_id, int(target),
         datetime.now().isoformat(timespec="seconds")))
    mgr = conn.execute("SELECT id FROM users WHERE role='manager'").fetchone()
    if mgr:
        t = conn.execute("SELECT day, start_time, end_time FROM shifts WHERE id=?",
                         (shift_id,)).fetchone()
        notify(conn, mgr["id"], "swap_request",
               f"{session['name']} wants to switch their {t['day']} "
               f"{t['start_time']}-{t['end_time']} shift for "
               f"{target_row['day']} {target_row['start_time']}-{target_row['end_time']}.")
    conn.commit()
    conn.close()
    flash("Switch request sent — the manager will review it.")
    return redirect(url_for("dashboard"))


@employee_bp.route("/request/swap", methods=["POST"])
@login_required()
def request_employee_swap():
    if session["role"] != "employee":
        abort(403)
    try:
        if request.form.get("target_assignment"):
            target_uid, target_id = map(
                int, request.form["target_assignment"].split(":", 1))
        else:
            target_uid = int(request.form.get("target_user_id", ""))
            target_id = int(request.form.get("target_shift_id", ""))
        source_id = int(request.form.get("shift_id", ""))
    except (TypeError, ValueError):
        flash("Choose your shift, the employee, and the shift you want to exchange.")
        return redirect(url_for("dashboard"))

    conn = db()
    uid = session["uid"]
    source = conn.execute(
        "SELECT s.* FROM shifts s JOIN assignments a ON a.shift_id=s.id "
        "WHERE s.id=? AND a.user_id=? AND a.status NOT IN "
        "('sick','swap_requested','manager_fixed','swap_invited')",
        (source_id, uid),
    ).fetchone()
    target = conn.execute(
        "SELECT s.* FROM shifts s JOIN assignments a ON a.shift_id=s.id "
        "WHERE s.id=? AND a.user_id=? AND a.status NOT IN "
        "('sick','swap_requested','manager_fixed','swap_invited')",
        (target_id, target_uid),
    ).fetchone()
    requester = conn.execute(
        "SELECT id, station FROM users WHERE id=? AND role='employee'", (uid,)
    ).fetchone()
    employee = conn.execute(
        "SELECT id, name, role, station FROM users WHERE id=?", (target_uid,)
    ).fetchone()
    conflicting_assignment = conn.execute(
        "SELECT 1 FROM assignments WHERE (shift_id=? AND user_id=?) "
        "OR (shift_id=? AND user_id=?) LIMIT 1",
        (target_id, uid, source_id, target_uid),
    ).fetchone()
    if (not source or not target or not requester or not employee or
            employee["role"] != "employee" or target_uid == uid or
            source_id == target_id or source["week_start"] != target["week_start"] or
            source["area"] != target["area"] or requester["station"] != source["area"] or
            employee["station"] != source["area"] or conflicting_assignment):
        conn.close()
        flash(
            "Choose valid shifts in the same week and house, assigned to you "
            "and the requested employee."
        )
        return redirect(url_for("dashboard"))

    pending = conn.execute(
        "SELECT 1 FROM requests WHERE kind='swap' AND user_id=? AND shift_id=? "
        "AND status='approved'", (uid, source_id),
    ).fetchone()
    if pending:
        conn.close()
        flash("You already have a pending request for that shift.")
        return redirect(url_for("dashboard"))

    conn.execute(
        "INSERT INTO requests (user_id, kind, shift_id, target_shift_id, target_user_id, "
        "status, created_at) VALUES (?, 'swap', ?, ?, ?, 'approved', ?)",
        (uid, source_id, target_id, target_uid,
         datetime.now().isoformat(timespec="seconds")),
    )
    notify(conn, target_uid, "swap_request",
           f"{session['name']} asked to exchange {source['day']} "
           f"{source['start_time']}-{source['end_time']} for your {target['day']} "
           f"{target['start_time']}-{target['end_time']} shift.")
    # The holder keeps the shift (staffed, counts toward hours) while the
    # coworker invite is pending; accept flips both rows to 'switch_fixed'.
    conn.execute(
        "UPDATE assignments SET status='swap_invited' WHERE shift_id=? AND user_id=?",
        (source_id, uid),
    )
    conn.commit()
    conn.close()
    flash(f"Swap request sent to {employee['name']}.")
    return redirect(url_for("my_requests"))


@employee_bp.route("/my-requests")
@login_required()
def my_requests():
    if session["role"] != "employee":
        return redirect(url_for("requests"))
    conn = db()
    rows = conn.execute(
        "SELECT r.*, requester.name requester_name, target.name target_name "
        "FROM requests r JOIN users requester ON requester.id=r.user_id "
        "LEFT JOIN users target ON target.id=r.target_user_id "
        "WHERE (r.user_id=? OR r.target_user_id=?) AND r.kind!='manager_unassign' "
        "ORDER BY r.id DESC", (session["uid"], session["uid"]),
    ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        for field, key in (("shift_id", "source_desc"),
                           ("target_shift_id", "target_desc")):
            if row[field]:
                shift = conn.execute(
                    "SELECT day, start_time, end_time FROM shifts WHERE id=?",
                    (row[field],),
                ).fetchone()
                if shift:
                    item[key] = f"{shift['day']} {shift['start_time']}-{shift['end_time']}"
        items.append(item)
    conn.close()
    return render_template("my_requests.html", items=items)


@employee_bp.route("/request/<int:req_id>/respond", methods=["POST"])
@login_required()
def respond_to_swap(req_id):
    if session["role"] != "employee":
        abort(403)
    decision = request.form.get("decision")
    reason = request.form.get("reason", "").strip()
    if decision not in ("accept", "reject") or (decision == "reject" and not reason):
        flash("Choose accept or reject; include a reason when rejecting.")
        return redirect(url_for("my_requests"))
    if len(reason) > 500:
        flash("Keep the response reason to 500 characters or fewer.")
        return redirect(url_for("my_requests"))

    conn = db()
    conn.execute("BEGIN IMMEDIATE")
    req = conn.execute(
        "SELECT * FROM requests WHERE id=? AND kind='swap' AND target_user_id=? "
        "AND status='approved'", (req_id, session["uid"]),
    ).fetchone()
    if not req:
        conn.close()
        abort(404)
    source = conn.execute("SELECT * FROM shifts WHERE id=?", (req["shift_id"],)).fetchone()
    target = conn.execute("SELECT * FROM shifts WHERE id=?", (req["target_shift_id"],)).fetchone()
    requester = conn.execute(
        "SELECT id, name, role, station FROM users WHERE id=?", (req["user_id"],)
    ).fetchone()
    recipient = conn.execute(
        "SELECT id, name, role, station FROM users WHERE id=?", (session["uid"],)
    ).fetchone()
    source_row = conn.execute(
        "SELECT status FROM assignments WHERE shift_id=? AND user_id=?",
        (req["shift_id"], req["user_id"]),
    ).fetchone()
    target_row = conn.execute(
        "SELECT status FROM assignments WHERE shift_id=? AND user_id=?",
        (req["target_shift_id"], session["uid"]),
    ).fetchone()
    # Requester's source row must still carry the pending invite, and the
    # invited employee's target row must be a live, non-manager-held assignment.
    assignments_valid = (
        source_row is not None and source_row["status"] == "swap_invited" and
        target_row is not None and
        target_row["status"] not in ("sick", "swap_requested", "swap_invited",
                                     "manager_fixed"))
    conflicting_assignment = conn.execute(
        "SELECT 1 FROM assignments WHERE (shift_id=? AND user_id=?) "
        "OR (shift_id=? AND user_id=?) LIMIT 1",
        (req["target_shift_id"], req["user_id"], req["shift_id"], session["uid"]),
    ).fetchone()
    assignments_valid = assignments_valid and not conflicting_assignment
    valid_pair = (source and target and requester and recipient and
                  requester["role"] == recipient["role"] == "employee" and
                  source["week_start"] == target["week_start"] and
                  source["area"] == target["area"] == requester["station"] == recipient["station"])
    if decision == "accept" and valid_pair and assignments_valid:
        requester_block = assignment_block_reason(
            conn, req["user_id"], target, exclude_shift_id=source["id"])
        recipient_block = assignment_block_reason(
            conn, session["uid"], source, exclude_shift_id=target["id"])
        if requester_block or recipient_block:
            reason = "Swap would violate a weekly hours, days-off, or availability rule."
        else:
            conn.execute(
                "UPDATE assignments SET user_id=?, status='switch_fixed' "
                "WHERE shift_id=? AND user_id=?",
                (req["user_id"], target["id"], session["uid"]),
            )
            conn.execute(
                "UPDATE assignments SET user_id=?, status='switch_fixed' "
                "WHERE shift_id=? AND user_id=?",
                (session["uid"], source["id"], req["user_id"]),
            )
            reason = f"Accepted by {recipient['name']}."
            conn.execute("UPDATE requests SET status='approved_ok', reason=? WHERE id=?",
                         (reason, req_id))
            notify(conn, req["user_id"], "assignment",
                   f"Your shift swap with {recipient['name']} was accepted.")
            conn.commit()
            conn.close()
            flash("Swap accepted; both schedules have been updated.")
            return redirect(url_for("my_requests"))
    elif decision == "accept":
        reason = (
            "One of the shifts is no longer assigned as requested or the "
            "pair is no longer valid."
        )

    conn.execute(
        "UPDATE assignments SET status='confirmed' WHERE shift_id=? AND user_id=? "
        "AND status='swap_invited'", (req["shift_id"], req["user_id"]),
    )
    conn.execute("UPDATE requests SET status='denied', reason=? WHERE id=?",
                 (reason, req_id))
    notify(conn, req["user_id"], "conflict",
           f"Your shift swap request was declined: {reason}")
    conn.commit()
    conn.close()
    flash("Swap declined and the requester was notified.")
    return redirect(url_for("my_requests"))
