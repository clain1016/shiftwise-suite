"""Shared mock seeding for the scheduler-mock environment.

Used by sync.sh (after syncing from the real app) and by test scripts that
rebuild a fresh DB. Keeps the 8-employee roster and the Mon-Sun demo week
in one place, independent of whatever the real app seeds.
"""
from datetime import date

ROSTER = [
    ("manager", "manager", "Store Manager", "manager", 40, "full_time", "2018-01-15"),
    ("maria",  "maria",  "Maria Lopez",   "employee", 40, "full_time", "2019-04-02"),
    ("devon",  "devon",  "Devon Carter",  "employee", 40, "full_time", "2020-08-17"),
    ("priya",  "priya",  "Priya Nair",    "employee", 40, "full_time", "2022-01-10"),
    ("alex",   "alex",   "Alex Rivera",   "employee", 38, "full_time", "2023-03-01"),
    ("sam",    "sam",    "Sam Chen",      "employee", 24, "part_time", "2023-09-15"),
    ("jordan", "jordan", "Jordan Diaz",   "employee", 28, "part_time", "2024-05-20"),
    ("taylor", "taylor", "Taylor Brooks", "employee", 20, "part_time", "2025-02-11"),
    ("riley",  "riley",  "Riley Nguyen",  "employee", 16, "part_time", "2026-06-01"),
]

DEMO_SHIFTS = [
    ("Mon", "09:00", "17:00", 3, None),
    ("Tue", "09:00", "17:00", 3, None),
    ("Wed", "09:00", "17:00", 2, None),
    ("Thu", "09:00", "17:00", 2, None),
    ("Fri", "09:00", "17:00", 3, None),
    ("Sat", "10:00", "18:00", 4, "Weekend rush"),
    ("Sun", "11:00", "16:00", 2, "Short day"),
]


def monday_of(d):
    return d - __import__("datetime").timedelta(days=d.weekday())


def seed(appmod):
    """Wipe and reseed appmod's DB with the mock roster + full demo week.

    Every employee also gets pre-seeded picks (top 3 days + a couple of
    backups) so the site is immediately testable without manual input.
    Ranks are chosen to guarantee conflicts: everyone wants Sat or Fri.
    """
    conn = appmod.db()
    for table in ("picks", "assignments", "notifications", "shifts", "users"):
        conn.execute(f"DELETE FROM {table}")
    conn.executemany(
        "INSERT INTO users (username, password, name, role, weekly_hours,"
        " employment_type, hired_on) VALUES (?,?,?,?,?,?,?)", ROSTER)
    week = monday_of(date.today()).isoformat()
    conn.executemany(
        "INSERT INTO shifts (week_start, day, start_time, end_time, slots, note) "
        "VALUES (?,?,?,?,?,?)",
        [(week, d, s, e, n, note) for d, s, e, n, note in DEMO_SHIFTS])

    # pre-seeded preferences: every employee ranks ALL 7 days (the full
    # ranking is the backup plan — required since the full-week pick rule)
    PREFS = {
        "maria":  {"Mon": 1, "Tue": 2, "Fri": 3, "Sat": 4, "Wed": 5, "Thu": 6, "Sun": 7},
        "devon":  {"Fri": 1, "Sat": 2, "Mon": 3, "Tue": 4, "Wed": 5, "Thu": 6, "Sun": 7},
        "priya":  {"Fri": 1, "Sat": 2, "Wed": 3, "Sun": 4, "Mon": 5, "Tue": 6, "Thu": 7},
        "alex":   {"Sat": 1, "Fri": 2, "Tue": 3, "Mon": 4, "Wed": 5, "Thu": 6, "Sun": 7},
        "sam":    {"Fri": 1, "Mon": 2, "Sun": 3, "Tue": 4, "Wed": 5, "Thu": 6, "Sat": 7},
        "jordan": {"Sat": 1, "Fri": 2, "Thu": 3, "Mon": 4, "Tue": 5, "Wed": 6, "Sun": 7},
        "taylor": {"Fri": 1, "Sat": 2, "Wed": 3, "Mon": 4, "Tue": 5, "Thu": 6, "Sun": 7},
        "riley":  {"Sat": 1, "Sun": 2, "Fri": 3, "Mon": 4, "Tue": 5, "Wed": 6, "Thu": 7},
    }
    day_to_id = {r["day"]: r["id"] for r in conn.execute("SELECT id, day FROM shifts")}
    for username, prefs in PREFS.items():
        uid = conn.execute(
            "SELECT id FROM users WHERE username=?", (username,)).fetchone()[0]
        for day, rank in prefs.items():
            conn.execute(
                "INSERT INTO picks (user_id, shift_id, rank) VALUES (?,?,?)",
                (uid, day_to_id[day], rank))

    conn.commit()
    n = conn.execute("SELECT COUNT(*) c FROM users WHERE role='employee'").fetchone()["c"]
    d = conn.execute("SELECT COUNT(DISTINCT day) c FROM shifts").fetchone()["c"]
    p = conn.execute("SELECT COUNT(*) c FROM picks").fetchone()["c"]
    conn.close()
    assert n == 8, f"expected 8 employees, got {n}"
    assert d == 7, f"expected Mon-Sun demo week, got {d} days"
    assert p == sum(len(v) for v in PREFS.values()), "picks not fully seeded"
    return n, d, p
