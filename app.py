"""Employee scheduling site.

Flow:
  1. Manager defines weekly shift slots (day + time range + slots to fill).
  2. System notifies employees in-app; they rank the days they want to work.
  3. Auto-scheduler assigns shifts: preferred picks first, then the best
     alternative (least-contested backup, fewest assigned hours, backfill)
     when someone's preferred slot is taken.
  4. Employees see their schedule; shifts are assigned automatically with
   no employee confirmation step (swap requests still ping the manager).

Run: .venv/bin/python app.py  ->  http://<your-ip>:5000
"""
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, date
from functools import wraps
from pathlib import Path

from flask import (Flask, request, session, redirect, url_for, flash,
                   render_template, abort)

DB_PATH = Path(__file__).parent / "scheduler.db"
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
MIN_DAYS_OFF = 2  # every employee gets at least 2 days off per week

app = Flask(__name__)
app.secret_key = "change-me-in-production"

# ---------------------------------------------------------------- database
SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    password TEXT NOT NULL,
    name TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'employee',
    weekly_hours INTEGER DEFAULT 40,
    employment_type TEXT NOT NULL DEFAULT 'part_time',
    hired_on TEXT
);
CREATE TABLE IF NOT EXISTS shifts (
    id INTEGER PRIMARY KEY,
    week_start TEXT NOT NULL,
    day TEXT NOT NULL,
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL,
    slots INTEGER NOT NULL DEFAULT 1,
    note TEXT
);
CREATE TABLE IF NOT EXISTS picks (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    shift_id INTEGER NOT NULL,
    rank INTEGER NOT NULL,
    UNIQUE(user_id, shift_id)
);
CREATE TABLE IF NOT EXISTS assignments (
    id INTEGER PRIMARY KEY,
    shift_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'proposed',
    UNIQUE(shift_id, user_id)
);
CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    kind TEXT NOT NULL,              -- day_off / vacation / sick / swap / switch
    shift_id INTEGER,                -- sick: the shift missed; switch: the shift given up
    day TEXT,                        -- day_off: which weekday
    target_shift_id INTEGER,         -- switch: the desired shift
    vacation_start TEXT,             -- vacation: range start (inclusive)
    vacation_end TEXT,               -- vacation: range end (inclusive)
    status TEXT NOT NULL DEFAULT 'approved',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    shift_id INTEGER,
    kind TEXT NOT NULL,
    message TEXT NOT NULL,
    created_at TEXT NOT NULL,
    read INTEGER NOT NULL DEFAULT 0
);
"""


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def monday_of(d):
    return d - timedelta(days=d.weekday())


def vacation_blocked_uids(conn):
    """User ids whose CURRENT approved vacation covers the given shift's
    date. Used as a hard filter in the lineup + backfill so a vacationing
    employee is never assigned (or re-assigned) onto a shift inside their
    range. Only status-approved vacation requests count."""
    rows = conn.execute(
        "SELECT user_id, vacation_start, vacation_end FROM requests "
        "WHERE kind='vacation' AND status IN ('approved', 'approved_ok') "
        "AND vacation_start IS NOT NULL").fetchall()
    return {r["user_id"]: (r["vacation_start"], r["vacation_end"]) for r in rows}


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    # migration: add new columns to an existing users table if missing
    cols = [r[1] for r in conn.execute("PRAGMA table_info(users)")]
    if "employment_type" not in cols:
        conn.execute(
            "ALTER TABLE users ADD COLUMN employment_type TEXT NOT NULL DEFAULT 'part_time'")
    if "hired_on" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN hired_on TEXT")
    rcols = [r[1] for r in conn.execute("PRAGMA table_info(requests)")]
    if "vacation_start" not in rcols:
        conn.execute("ALTER TABLE requests ADD COLUMN vacation_start TEXT")
    if "vacation_end" not in rcols:
        conn.execute("ALTER TABLE requests ADD COLUMN vacation_end TEXT")
    if not conn.execute("SELECT 1 FROM users LIMIT 1").fetchone():
        conn.executemany(
            "INSERT INTO users (username, password, name, role, weekly_hours,"
            " employment_type, hired_on) VALUES (?,?,?,?,?,?,?)",
            [
                ("manager", "manager", "Store Manager", "manager", 40, "full_time", "2020-01-15"),
                ("alex", "alex", "Alex Rivera", "employee", 30, "full_time", "2021-03-01"),
                ("sam", "sam", "Sam Chen", "employee", 25, "part_time", "2023-06-10"),
                ("jordan", "jordan", "Jordan Diaz", "employee", 35, "part_time", "2024-11-20"),
            ],
        )
        week = monday_of(date.today()).isoformat()
        demo = [
            (week, "Mon", "09:00", "17:00", 2, None),
            (week, "Tue", "09:00", "17:00", 2, None),
            (week, "Wed", "09:00", "17:00", 2, None),
            (week, "Thu", "09:00", "17:00", 2, None),
            (week, "Fri", "09:00", "17:00", 2, None),
            (week, "Sat", "10:00", "18:00", 3, "Weekend rush"),
            (week, "Sun", "11:00", "16:00", 2, "Short day"),
        ]
        conn.executemany(
            "INSERT INTO shifts (week_start, day, start_time, end_time, slots, note) "
            "VALUES (?,?,?,?,?,?)", demo)
    conn.commit()
    conn.close()


def notify(conn, user_id, kind, message, shift_id=None):
    conn.execute(
        "INSERT INTO notifications (user_id, shift_id, kind, message, created_at) "
        "VALUES (?,?,?,?,?)",
        (user_id, shift_id, kind, message, datetime.now().isoformat(timespec="seconds")))


# ---------------------------------------------------------------- auth
def login_required(role=None):
    def deco(f):
        @wraps(f)
        def wrapper(*a, **kw):
            if "uid" not in session:
                return redirect(url_for("login"))
            if role and session.get("role") != role:
                abort(403)
            return f(*a, **kw)
        return wrapper
    return deco


def shift_hours(s, e):
    sh, sm = map(int, s.split(":"))
    eh, em = map(int, e.split(":"))
    return (eh * 60 + em - sh * 60 - sm) / 60.0


def priority_key(user_row):
    """Sort key for the priority lineup: full-time before part-time, then
    earliest hire date first. Shared by the auto-scheduler and the
    conflict-resolution page so both sort identically."""
    seniority = 0 if not user_row["hired_on"] else (
        datetime.now() - datetime.fromisoformat(user_row["hired_on"])).days
    return (0 if user_row["employment_type"] == "full_time" else 1, -seniority)


def coverage_plan(conn, week, out_shift_id, out_uid):
    """Pick the next-in-line coverer for a shift its holder is leaving.

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
        "SELECT * FROM users WHERE role='employee' ORDER BY id").fetchall()
    # current week hours + working days for every employee, excluding the
    # shift being covered (its holder's hours leave with them) and any
    # existing 'sick' rows (a sick assignment counts for nobody)
    arows = conn.execute(
        "SELECT a.shift_id, a.user_id, a.status, s.start_time, s.end_time, s.day "
        "FROM assignments a JOIN shifts s ON s.id=a.shift_id "
        "WHERE s.week_start=? AND a.shift_id!=?",
        (week, out_shift_id)).fetchall()
    hours = defaultdict(float)
    days = defaultdict(set)
    for r in arows:
        if r["status"] == "sick":
            continue
        hours[r["user_id"]] += shift_hours(r["start_time"], r["end_time"])
        days[r["user_id"]].add(r["day"])
    sh = shift_hours(shift["start_time"], shift["end_time"])
    # people already on this shift are not cover candidates (slots are 1
    # per person — UNIQUE(shift_id, user_id))
    already_on = {r["user_id"] for r in conn.execute(
        "SELECT user_id FROM assignments WHERE shift_id=?", (out_shift_id,))}

    def can_cover(u):
        uid = u["id"]
        if uid in already_on:
            return False
        vac = vacation_blocked_uids(conn).get(uid)
        if vac is not None:
            shift_date = date.fromisoformat(shift["week_start"]) + \
                timedelta(days=DAYS.index(shift["day"]))
            if date.fromisoformat(vac[0]) <= shift_date <= date.fromisoformat(vac[1]):
                return False  # on approved vacation that day
        cap = u["weekly_hours"] or 40
        same_day = shift["day"] in days.get(uid, set())
        if not same_day and len(days.get(uid, set())) >= 7 - MIN_DAYS_OFF:
            return False  # would break the days-off rule
        if hours.get(uid, 0) + sh > cap:
            return False  # would break the hours cap
        return True

    # 1. people who picked this shift but didn't get it — best in lineup.
    # With out_uid=None (backfill pass) this prefers employees who picked
    # the shift at ALL (even ones already holding it rank-first — they
    # skip out below via already_on) before falling to the least-loaded.
    picker_ids = {r["user_id"] for r in conn.execute(
        "SELECT DISTINCT user_id FROM picks WHERE shift_id=? AND user_id!=?",
        (out_shift_id, out_uid))}
    for u in sorted((u for u in employees if u["id"] in picker_ids),
                    key=priority_key):
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
        "SELECT * FROM assignments WHERE shift_id=? AND user_id=?",
        (shift_id, uid)).fetchone()
    if not a or a["status"] == "sick":
        return False
    shift = conn.execute("SELECT * FROM shifts WHERE id=?", (shift_id,)).fetchone()
    if not shift:
        return False
    week = shift["week_start"]
    conn.execute("UPDATE assignments SET status='sick' WHERE shift_id=? AND user_id=?",
                 (shift_id, uid))
    notify(conn, uid, "conflict",
           f"Sick call logged for {shift['day']} "
           f"{shift['start_time']}-{shift['end_time']} — get well soon.")
    cover_uid = coverage_plan(conn, week, shift_id, uid)
    if cover_uid:
        conn.execute(
            "INSERT OR IGNORE INTO assignments (shift_id, user_id) VALUES (?,?)",
            (shift_id, cover_uid))
        conn.execute(
            "UPDATE assignments SET status='notified' WHERE shift_id=? AND user_id=? "
            "AND status='proposed'", (shift_id, cover_uid))
        notify(conn, cover_uid, "assignment",
               f"Coverage: you're now on {shift['day']} "
               f"{shift['start_time']}-{shift['end_time']} (covering a sick call).")
        return True
    mgr = conn.execute("SELECT id FROM users WHERE role='manager'").fetchone()
    if mgr:
        notify(conn, mgr["id"], "conflict",
               f"No cover found for {shift['day']} "
               f"{shift['start_time']}-{shift['end_time']} — needs manual coverage.")
    return False


# ---------------------------------------------------------------- scheduler
def run_scheduler(week_start, actor="system"):
    """Assign picks to shifts across the week.

    Round-based, with a priority lineup: employees are ordered by
    employment type (full-time before part-time), then by seniority
    (earliest hire date first). In round N every employee claims their
    rank-N pick; contested slots go to the highest-priority employee in
    line. Employees are never scheduled past their weekly hours cap
    (full-time default 40h, part-time as set on the roster). If a slot is
    full, the employee falls through to their next pick — the best
    available alternative on their ranked list — and gets a notification
    explaining the fallback. When their whole list is exhausted they get a
    'nothing had room' notification.

    Rebuild semantics: every call wipes the week's 'proposed' assignments
    and recomputes from scratch, so it can be auto-triggered after any
    pick save, roster change, or shift change. 'confirmed' (and pending
    swap) assignments are kept and count against capacity.
    """
    conn = db()
    try:
        shifts = conn.execute(
            "SELECT * FROM shifts WHERE week_start=? ORDER BY id", (week_start,)
        ).fetchall()
        shift_ids = [s["id"] for s in shifts]
        if not shift_ids:
            return 0
        ph = ",".join("?" * len(shift_ids))
        picks = conn.execute(
            f"SELECT * FROM picks WHERE shift_id IN ({ph})", shift_ids).fetchall()
        users = {u["id"]: u for u in conn.execute(
            "SELECT * FROM users WHERE role='employee'")}

        # confirmed / pending-swap / manager-approved-switch assignments
        # are fixed: keep the rows and seed the in-memory state with them
        # before re-running the lineup. ('notified' rows are NOT fixed —
        # they're regular auto-assignments and must be recomputed so a
        # higher-priority pick can displace an earlier lower-priority one.)
        fixed = conn.execute(
            f"SELECT shift_id, user_id FROM assignments WHERE shift_id IN ({ph}) "
            "AND status IN ('confirmed', 'swap_requested')", shift_ids).fetchall()
        # manager-approved switches get a 'switch_fixed' tag row (status=
        # 'switch_fixed' until the next manual touch): fixed like confirmed
        # but rendered as 'scheduled'. Seed from a helper view so both the
        # tag and normal confirmed behave identically in capacity math.
        fixed += conn.execute(
            f"SELECT shift_id, user_id FROM assignments WHERE shift_id IN ({ph}) "
            "AND status='switch_fixed'", shift_ids).fetchall()
        conn.execute(
            f"DELETE FROM assignments WHERE shift_id IN ({ph}) AND "
            "status IN ('proposed', 'notified')", shift_ids)

        capacity = {s["id"]: s["slots"] for s in shifts}
        assigned = defaultdict(int)          # shift_id -> count
        user_hours = defaultdict(float)      # user_id -> assigned hours this week
        user_assignments = defaultdict(list) # user_id -> [(shift_id, marker)]
                                             # marker: True = fixed confirmed,
                                             # False = new, 'sick' = out sick
        shift_hours_map = {s["id"]: shift_hours(s["start_time"], s["end_time"])
                           for s in shifts}
        shift_by_id = {s["id"]: s for s in shifts}

        for r in fixed:
            if r["user_id"] in users:
                assigned[r["shift_id"]] += 1
                user_hours[r["user_id"]] += shift_hours_map[r["shift_id"]]
                user_assignments[r["user_id"]].append((r["shift_id"], True))

        # 'sick' rows survive the wipe (they're neither fixed nor deletable):
        # the out-sick employee keeps the row so they don't get re-claimed
        # onto that same shift, but it does NOT fill capacity and does NOT
        # count toward their hours or working days.
        sick_rows = conn.execute(
            f"SELECT shift_id, user_id FROM assignments WHERE shift_id IN ({ph}) "
            "AND status='sick'", shift_ids).fetchall()
        for r in sick_rows:
            if r["user_id"] in users:
                user_assignments[r["user_id"]].append((r["shift_id"], "sick"))

        # defensive: ignore picks belonging to non-employees (e.g. stale
        # rows created before the manager role check existed)
        picks = [p for p in picks if p["user_id"] in users]

        # user_id -> {rank: [shift_ids]} — tied ranks allowed, resolved in
        # submission order within the round
        user_prefs = defaultdict(lambda: defaultdict(list))
        for p in picks:
            user_prefs[p["user_id"]][p["rank"]].append(p["shift_id"])

        # Priority lineup: full-time before part-time, then earliest hire
        # date first. Employees missing a hire date rank last within their
        # group (treated as newest).
        priority = lambda uid: priority_key(users[uid])
        vac_blocked = vacation_blocked_uids(conn)

        max_rank = max((max(prefs) for prefs in user_prefs.values()), default=0)
        for rnd in range(1, max_rank + 1):
            # who wants a slot this round, highest priority first
            # (contested slots go to the most senior / full-time employee)
            claimants = []
            for uid, prefs in user_prefs.items():
                for sid in prefs.get(rnd, []):
                    if sid in [a[0] for a in user_assignments[uid]]:
                        continue
                    claimants.append((priority(uid), uid, sid))
            claimants.sort()
            for _, uid, sid in claimants:
                if sid in [a[0] for a in user_assignments[uid]]:
                    continue
                cap = users[uid]["weekly_hours"]
                if user_hours[uid] + shift_hours_map[sid] > cap:
                    if actor == "system":
                        notify(conn, uid, "conflict",
                               f"Passing on {shift_by_id[sid]['day']} — it would put "
                               f"you over your {cap}h weekly cap.")
                    continue
                # days-off guard: max 5 distinct working days per week
                # (two shifts on the same day don't add a second working day)
                already_days = {shift_by_id[s]["day"] for s, _ in user_assignments[uid]}
                if shift_by_id[sid]["day"] not in already_days and \
                        len(already_days) + 1 > 7 - MIN_DAYS_OFF:
                    if actor == "system":
                        notify(conn, uid, "conflict",
                               f"Passing on {shift_by_id[sid]['day']} — you need "
                               f"at least {MIN_DAYS_OFF} days off this week.")
                    continue
                # vacation guard: never assign a shift whose date falls
                # inside the employee's approved vacation range
                vac = vac_blocked.get(uid)
                if vac is not None:
                    shift_date = date.fromisoformat(week_start) + \
                        timedelta(days=DAYS.index(shift_by_id[sid]["day"]))
                    if date.fromisoformat(vac[0]) <= shift_date <= date.fromisoformat(vac[1]):
                        continue
                if assigned[sid] < capacity[sid]:
                    assigned[sid] += 1
                    user_hours[uid] += shift_hours_map[sid]
                    user_assignments[uid].append((sid, False))
                    conn.execute(
                        "INSERT OR IGNORE INTO assignments (shift_id, user_id) VALUES (?,?)",
                        (sid, uid))
                elif actor == "system":
                    shift = shift_by_id[sid]
                    notify(conn, uid, "conflict",
                           f"Your #{rnd} pick ({shift['day']} "
                           f"{shift['start_time']}-{shift['end_time']}) was full — "
                           "trying your next choice.")

        # coverage backfill: after the lineup, any shift that still has
        # empty slots (typical cause: a sick row vacated a taken slot)
        # is auto-covered by the best available employee — those who
        # picked it in lineup order first, then least-loaded with room.
        # Capacity math: assigned[] counts real workers only (sick rows
        # were never counted), so a shift with 2 slots + 1 sick row + 0
        # covers shows 1 open slot here and gets exactly one coverer.
        if actor == "system":
            for s in shifts:
                open_n = capacity[s["id"]] - assigned[s["id"]]
                if open_n <= 0:
                    continue
                for _ in range(open_n):
                    cover_uid = coverage_plan(conn, week_start, s["id"], None)
                    if cover_uid is None:
                        mgr = conn.execute(
                            "SELECT id FROM users WHERE role='manager'").fetchone()
                        if mgr:
                            notify(conn, mgr["id"], "conflict",
                                   f"No cover available for {s['day']} "
                                   f"{s['start_time']}-{s['end_time']} — "
                                   "needs manual coverage.")
                        break
                    # record + notify (hours/days tracking stays DB-accurate
                    # via the next build; insert directly to assignments)
                    conn.execute(
                        "INSERT OR IGNORE INTO assignments (shift_id, user_id) "
                        "VALUES (?,?)", (s["id"], cover_uid))
                    conn.execute(
                        "UPDATE assignments SET status='notified' "
                        "WHERE shift_id=? AND user_id=? AND status='proposed'",
                        (s["id"], cover_uid))
                    assigned[s["id"]] += 1
                    user_hours[cover_uid] += shift_hours_map[s["id"]]
                    user_assignments[cover_uid].append((s["id"], False))
                    notify(conn, cover_uid, "assignment",
                           f"Coverage: you're now on {s['day']} "
                           f"{s['start_time']}-{s['end_time']} (covering an "
                           "open slot).")

        # wrap-up notifications — only for employees whose schedule changed
        # (an auto-rebuild shouldn't spam unchanged schedules)
        got_sets = {uid: {s for s, _ in user_assignments[uid]}
                    for uid in user_prefs}
        for uid, prefs in user_prefs.items():
            u = users.get(uid)
            if not u:
                continue
            got = sorted(got_sets[uid])
            prev = conn.execute(
                "SELECT shift_id FROM assignments WHERE user_id=? AND status='notified'",
                (uid,)).fetchall()
            prev_set = {r["shift_id"] for r in prev}
            if got and got == sorted(prev_set):
                continue  # unchanged since last auto-build, no re-notify
            if got:
                days = ", ".join(shift_by_id[s]["day"] for s in got)
                notify(conn, uid, "assignment",
                       f"Schedule posted: you're on {days} this week.")
            else:
                notify(conn, uid, "conflict",
                       "None of your picks had room this week — check open shifts "
                       "or message the manager.")

        # mark fresh proposed assignments as 'notified' so the next rebuild
        # can tell whether anything changed for this employee
        conn.execute(f"""
            UPDATE assignments SET status='notified'
            WHERE shift_id IN ({ph}) AND status='proposed'""", shift_ids)
        conn.commit()
        return len(picks)
    finally:
        conn.close()


# ---------------------------------------------------------------- routes
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        conn = db()
        u = conn.execute("SELECT * FROM users WHERE username=? AND password=?",
                         (request.form["username"], request.form["password"])).fetchone()
        conn.close()
        if u:
            session.update(uid=u["id"], role=u["role"], name=u["name"])
            return redirect(url_for("dashboard"))
        flash("Wrong username or password")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required()
def dashboard():
    conn = db()
    week = monday_of(date.today()).isoformat()
    shifts = conn.execute(
        "SELECT * FROM shifts WHERE week_start=? ORDER BY id", (week,)).fetchall()
    my_assignments = {}
    for r in conn.execute(
            "SELECT shift_id, status FROM assignments WHERE user_id=?", (session["uid"],)):
        my_assignments[r["shift_id"]] = r["status"]
    my_picks = {r["shift_id"]: r["rank"] for r in conn.execute(
        "SELECT shift_id, rank FROM picks WHERE user_id=?", (session["uid"],))}
    # who is on each shift + remaining capacity (sick rows don't count as staff)
    roster = defaultdict(list)
    for r in conn.execute(
            "SELECT a.shift_id, u.name FROM assignments a JOIN users u ON u.id=a.user_id "
            "WHERE a.status != 'sick'"):
        roster[r["shift_id"]].append(r["name"])
    notifs = conn.execute(
        "SELECT * FROM notifications WHERE user_id=? ORDER BY id DESC LIMIT 20",
        (session["uid"],)).fetchall()
    conn.execute("UPDATE notifications SET read=1 WHERE user_id=?", (session["uid"],))
    conn.commit()
    conn.close()
    return render_template("dashboard.html", shifts=shifts, DAYS=DAYS, week=week,
                           my_assignments=my_assignments, my_picks=my_picks,
                           roster=roster, notifs=notifs)


@app.route("/pick", methods=["POST"])
@login_required()
def pick():
    if session["role"] != "employee":
        flash("Picks are for employees — managers don't schedule themselves.")
        return redirect(url_for("dashboard"))
    conn = db()
    uid = session["uid"]
    week = monday_of(date.today()).isoformat()
    week_shifts = [r["id"] for r in conn.execute(
        "SELECT id FROM shifts WHERE week_start=?", (week,))]
    ranked = []
    missing = []
    for sid in week_shifts:
        r = request.form.get(f"rank_{sid}")
        if r and r.isdigit() and 1 <= int(r) <= 7:
            ranked.append((int(r), int(sid)))
        else:
            missing.append(sid)
    if missing:
        conn.close()
        flash("Rank ALL seven days (1 = top choice) — every day needs a "
              "backup so any shift can be covered if plans change.")
        return redirect(url_for("dashboard"))
    # ranks must be 1..7 with no gaps or duplicates
    if sorted(int(r) for r, _ in ranked) != list(range(1, len(week_shifts) + 1)):
        conn.close()
        flash("Use each rank 1–" + str(len(week_shifts)) + " exactly once "
              "(1 = top choice) so every day has a backup.")
        return redirect(url_for("dashboard"))
    conn.execute("DELETE FROM picks WHERE user_id=?", (uid,))
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


@app.route("/swap/<int:shift_id>", methods=["POST"])
@login_required()
def swap(shift_id):
    """Employee requests a swap: the same coverage criteria as sick calls
    assign a replacement automatically — no manager wait. On success the
    requester's assignment AND pick are deleted (they never wanted the
    shift) and the swap is logged for the manager; on failure the row
    becomes 'swap_requested' (fixed in rebuilds, doesn't fill capacity)
    and the manager is alerted to cover it by hand."""
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
            "UPDATE assignments SET status='notified' WHERE shift_id=? AND user_id=? "
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


# ---------------- employee self-service: day off / vacation / sick / switch
@app.route("/request/vacation", methods=["POST"])
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
    conn = db()
    uid = session["uid"]
    now = date.today()
    if vs < now:
        flash("Vacation can't start in the past.")
        conn.close()
        return redirect(url_for("dashboard"))
    conn.execute(
        "INSERT INTO requests (user_id, kind, vacation_start, vacation_end, created_at) "
        "VALUES (?,?,?,?,?)",
        (uid, "vacation", vs.isoformat(), ve.isoformat(),
         datetime.now().isoformat(timespec="seconds")))
    # drop this week's shifts that fall inside the range (future weeks are
    # handled at rollover — everything is pinned to the current week now)
    dropped = conn.execute(
        "SELECT a.shift_id FROM assignments a JOIN shifts s ON s.id=a.shift_id "
        "WHERE a.user_id=? AND a.status NOT IN ('sick','swap_requested')",
        (uid,)).fetchall()
    n_dropped = 0
    for r in dropped:
        s = conn.execute("SELECT day, week_start FROM shifts WHERE id=?",
                         (r["shift_id"],)).fetchone()
        if not s:
            continue
        d = date.fromisoformat(s["week_start"]) + timedelta(days=DAYS.index(s["day"]))
        if vs <= d <= ve:
            conn.execute("DELETE FROM assignments WHERE shift_id=? AND user_id=?",
                         (r["shift_id"], uid))
            conn.execute("DELETE FROM picks WHERE user_id=? AND shift_id=?",
                         (r["shift_id"], uid))
            n_dropped += 1
    mgr = conn.execute("SELECT id FROM users WHERE role='manager'").fetchone()
    if mgr:
        notify(conn, mgr["id"], "swap_request",
               f"{session['name']} requested vacation {vs.isoformat()} to "
               f"{ve.isoformat()} ({n_dropped} shift(s) dropped this week).")
    conn.commit()
    conn.close()
    flash(f"Vacation requested {vs.isoformat()} to {ve.isoformat()}"
          + (f" — {n_dropped} shift(s) freed for coverage." if n_dropped else "."))
    run_scheduler(monday_of(date.today()).isoformat())
    return redirect(url_for("dashboard"))


@app.route("/request/day_off", methods=["POST"])
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
    conn.execute(
        "INSERT INTO requests (user_id, kind, day, created_at) VALUES (?,?,?,?)",
        (uid, "day_off", day, datetime.now().isoformat(timespec="seconds")))
    # drop any picks + assignments the employee holds on that weekday
    dropped = conn.execute(
        "SELECT a.shift_id, s.day FROM assignments a JOIN shifts s ON s.id=a.shift_id "
        "WHERE a.user_id=? AND s.week_start=? AND s.day=? AND a.status NOT IN "
        "('confirmed','swap_requested')", (uid, week, day)).fetchall()
    for r in dropped:
        conn.execute("DELETE FROM assignments WHERE user_id=? AND shift_id=?",
                     (uid, r["shift_id"]))
        conn.execute("DELETE FROM picks WHERE user_id=? AND shift_id=?",
                     (uid, r["shift_id"]))
    mgr = conn.execute("SELECT id FROM users WHERE role='manager'").fetchone()
    if mgr:
        notify(conn, mgr["id"], "swap_request",
               f"{session['name']} requested {day} off.")
    conn.commit()
    conn.close()
    flash(f"Day off requested for {day}.")
    run_scheduler(week)
    return redirect(url_for("dashboard"))


@app.route("/request/sick/<int:shift_id>", methods=["POST"])
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


@app.route("/request/switch/<int:shift_id>", methods=["POST"])
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
        "SELECT id FROM assignments WHERE shift_id=? AND user_id=?",
        (shift_id, uid)).fetchone()
    if not mine:
        conn.close()
        flash("That's not one of your shifts.")
        return redirect(url_for("dashboard"))
    target_row = conn.execute("SELECT * FROM shifts WHERE id=?", (int(target),)).fetchone()
    if not target_row:
        conn.close()
        flash("Target shift not found.")
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


# ---------------- manager routes
@app.route("/manager")
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
    label = {"swap_requested": "swap requested", "notified": "scheduled",
             "sick": "out sick", "switch_fixed": "scheduled (switched)"}
    for r in conn.execute(
            "SELECT a.shift_id, u.name, a.status FROM assignments a "
            "JOIN users u ON u.id=a.user_id WHERE a.status NOT IN ('sick')"):
        assigned[r["shift_id"]].append(f"{r['name']} ({label.get(r['status'], r['status'])})")
    unassigned = conn.execute(
        "SELECT DISTINCT u.name FROM users u WHERE u.role='employee' AND u.id NOT IN "
        "(SELECT a.user_id FROM assignments a JOIN shifts s ON s.id=a.shift_id "
        " WHERE s.week_start=?)", (week,)).fetchall()
    conn.close()
    return render_template("manager.html", shifts=shifts, picks=picks, DAYS=DAYS,
                           assigned=assigned, week=week,
                           unassigned=[u["name"] for u in unassigned])


@app.route("/manager/shift/add", methods=["POST"])
@login_required(role="manager")
def add_shift():
    conn = db()
    conn.execute(
        "INSERT INTO shifts (week_start, day, start_time, end_time, slots, note) "
        "VALUES (?,?,?,?,?,?)",
        (monday_of(date.today()).isoformat(), request.form["day"],
         request.form["start"], request.form["end"], int(request.form["slots"]),
         request.form.get("note") or None))
    for emp in conn.execute("SELECT id FROM users WHERE role='employee'"):
        notify(conn, emp["id"], "new_schedule",
               f"New shift posted: {request.form['day']} "
               f"{request.form['start']}-{request.form['end']} — submit your picks!")
    conn.commit()
    conn.close()
    # auto-rebuild: new shift changes capacity -> re-run the lineup
    run_scheduler(monday_of(date.today()).isoformat())
    flash("Shift added and employees notified.")
    return redirect(url_for("manager"))


@app.route("/manager/shift/delete/<int:shift_id>", methods=["POST"])
@login_required(role="manager")
def delete_shift(shift_id):
    conn = db()
    conn.execute("DELETE FROM picks WHERE shift_id=?", (shift_id,))
    conn.execute("DELETE FROM assignments WHERE shift_id=?", (shift_id,))
    conn.execute("DELETE FROM shifts WHERE id=?", (shift_id,))
    conn.commit()
    conn.close()
    # auto-rebuild: deleted shift frees people -> re-run the lineup
    run_scheduler(monday_of(date.today()).isoformat())
    return redirect(url_for("manager"))


@app.route("/manager/run", methods=["POST"])
@login_required(role="manager")
def run():
    n = run_scheduler(monday_of(date.today()).isoformat())
    flash(f"Scheduler ran over {n} picks — check assignments below.")
    return redirect(url_for("manager"))


# ---------------------------------------------------------------- conflicts
@app.route("/manager/conflicts")
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
    conflicts = []
    for s in shifts:
        claimants = conn.execute(
            "SELECT p.user_id, p.rank, u.name, u.employment_type, u.hired_on, "
            "u.weekly_hours FROM picks p JOIN users u ON u.id=p.user_id "
            "WHERE p.shift_id=? ORDER BY p.rank", (s["id"],)).fetchall()
        if not claimants:
            continue
        assigned_rows = conn.execute(
            "SELECT u.name, u.id, a.status FROM assignments a "
            "JOIN users u ON u.id=a.user_id WHERE a.shift_id=?", (s["id"],)).fetchall()
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
            conflicts.append({
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
    d = date.fromisoformat(week)
    return render_template(
        "conflicts.html", conflicts=conflicts, week=week,
        prev_week=(d - timedelta(days=7)).isoformat(),
        next_week=(d + timedelta(days=7)).isoformat(),
        this_week=monday_of(date.today()).isoformat(),
        employees_without_shifts=pending_picks)


@app.route("/manager/unassign/<int:shift_id>/<int:user_id>", methods=["POST"])
@login_required(role="manager")
def unassign(shift_id, user_id):
    conn = db()
    conn.execute("DELETE FROM assignments WHERE shift_id=? AND user_id=?",
                 (shift_id, user_id))
    notify(conn, user_id, "conflict",
           "One of your shifts was reassigned by the manager — check your schedule.")
    conn.commit()
    conn.close()
    # auto-rebuild: freed slot -> re-run the lineup so the next-in-line gets it
    run_scheduler(monday_of(date.today()).isoformat())
    return redirect(url_for("conflicts"))


# ---------------- manager: employee requests (day off / sick / switch)
@app.route("/manager/requests")
@login_required(role="manager")
def requests():
    conn = db()
    rows = conn.execute(
        "SELECT r.*, u.name FROM requests r JOIN users u ON u.id=r.user_id "
        "ORDER BY r.id DESC").fetchall()
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
        items.append(item)
    conn.close()
    return render_template("requests.html", items=items)


@app.route("/manager/requests/<int:req_id>/approve", methods=["POST"])
@login_required(role="manager")
def approve_request(req_id):
    conn = db()
    r = conn.execute("SELECT * FROM requests WHERE id=?", (req_id,)).fetchone()
    if not r or r["status"] != "approved":
        conn.close()
        flash("Request not found or already handled.")
        return redirect(url_for("requests"))
    if r["kind"] == "day_off":
        conn.execute("UPDATE requests SET status='approved_ok' WHERE id=?", (req_id,))
        notify(conn, r["user_id"], "assignment",
               f"Your day-off request for {r['day']} was approved.")
    elif r["kind"] == "switch" and r["shift_id"] and r["target_shift_id"]:
        target = conn.execute("SELECT * FROM shifts WHERE id=?",
                              (r["target_shift_id"],)).fetchone()
        if target:
            staffed = conn.execute(
                "SELECT COUNT(*) c FROM assignments WHERE shift_id=? AND status!='sick'",
                (r["target_shift_id"],)).fetchone()["c"]
            if staffed >= target["slots"]:
                conn.execute("UPDATE requests SET status='denied' WHERE id=?",
                             (req_id,))
                notify(conn, r["user_id"], "conflict",
                       f"Switch declined: {target['day']} "
                       f"{target['start_time']}-{target['end_time']} is already full.")
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
                # drop the old-shift pick so the rebuild's lineup
                # doesn't just re-claim it for the same employee
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
        # rebuild after a switch: the approved row is 'switch_fixed' so the
        # lineup won't undo it, and the vacated old slot gets backfilled
        run_scheduler(monday_of(date.today()).isoformat())
        flash("Switch approved.")
        return redirect(url_for("requests"))
    conn.commit()
    conn.close()
    # freed slot from the switch/day-off -> next-in-line gets covered automatically
    run_scheduler(monday_of(date.today()).isoformat())
    flash("Request approved.")
    return redirect(url_for("requests"))


@app.route("/manager/requests/<int:req_id>/deny", methods=["POST"])
@login_required(role="manager")
def deny_request(req_id):
    conn = db()
    conn.execute("UPDATE requests SET status='denied' WHERE id=? AND status='approved'",
                 (req_id,))
    r = conn.execute("SELECT * FROM requests WHERE id=?", (req_id,)).fetchone()
    if r and r["status"] == "denied":
        notify(conn, r["user_id"], "conflict",
               "One of your requests was declined by the manager — check the "
               "Requests page or talk to them.")
    conn.commit()
    conn.close()
    flash("Request denied.")
    return redirect(url_for("requests"))


# ---------------------------------------------------------------- calendar
def calendar_days(conn, week, user_id):
    """7-day grid for a week: shifts per day, assignment status + staff for user_id."""
    shifts = conn.execute(
        "SELECT * FROM shifts WHERE week_start=? ORDER BY id", (week,)).fetchall()
    by_day = defaultdict(list)
    for s in shifts:
        rows = conn.execute(
            "SELECT a.status, a.user_id, u.name FROM assignments a "
            "JOIN users u ON u.id=a.user_id WHERE a.shift_id=? AND a.status != 'sick'",
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


@app.route("/calendar")
@login_required()
def calendar_view():
    raw = request.args.get("week", "")
    try:
        week = monday_of(date.fromisoformat(raw)).isoformat()
    except ValueError:
        week = monday_of(date.today()).isoformat()
    conn = db()
    uid = session["uid"]
    employees = []
    if session["role"] == "manager":
        employees = conn.execute(
            "SELECT id, name FROM users WHERE role='employee' ORDER BY name").fetchall()
        req = request.args.get("user_id", "")
        if req.isdigit():
            row = conn.execute(
                "SELECT id FROM users WHERE id=? AND role='employee'", (int(req),)).fetchone()
            if row:
                uid = row["id"]
    view_user = conn.execute("SELECT name FROM users WHERE id=?", (uid,)).fetchone()
    conn.close()
    d = date.fromisoformat(week)
    return render_template(
        "calendar.html", days=calendar_days(db(), week, uid), week=week,
        view_name=view_user["name"], employees=employees, view_id=uid,
        prev_week=(d - timedelta(days=7)).isoformat(),
        next_week=(d + timedelta(days=7)).isoformat(),
        this_week=monday_of(date.today()).isoformat())


# ---------------- roster (employment type / seniority)
@app.route("/manager/roster", methods=["GET", "POST"])
@login_required(role="manager")
def roster():
    conn = db()
    if request.method == "POST":
        for uid_row in conn.execute("SELECT id FROM users WHERE role='employee'"):
            uid = uid_row["id"]
            et = request.form.get(f"type_{uid}")
            hired = request.form.get(f"hired_{uid}", "").strip()
            cap = request.form.get(f"cap_{uid}", "").strip()
            if et in ("full_time", "part_time"):
                conn.execute("UPDATE users SET employment_type=? WHERE id=?", (et, uid))
            conn.execute("UPDATE users SET hired_on=? WHERE id=?",
                         (hired or None, uid))
            if cap.isdigit():
                conn.execute("UPDATE users SET weekly_hours=? WHERE id=?", (int(cap), uid))
        conn.commit()
        flash("Roster updated.")
        conn.close()
        # auto-rebuild: priority lineup may have changed -> re-run
        run_scheduler(monday_of(date.today()).isoformat())
        return redirect(url_for("roster"))
    employees = conn.execute(
        "SELECT * FROM users WHERE role='employee' "
        "ORDER BY employment_type='full_time' DESC, hired_on ISNULL, hired_on").fetchall()
    conn.close()
    return render_template("roster.html", employees=employees)


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5000, debug=True)
