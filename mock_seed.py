"""Shared canonical mock seeding for the ShiftWise environment.

Keeps the fake roster and the Mon-Sun demo week in one place.
Roster is split by house: 5 front-of-house and 5 back-of-house employees.
FOH and BOH are two separate schedules — FOH staff are never scheduled
onto BOH shifts and vice versa.
"""
from datetime import date, timedelta

# (username, password, name, role, weekly_hours, employment_type, hired_on, station)
ROSTER = [
    ("manager", "manager", "Store Manager", "manager", 40, "full_time", "2018-01-15", "front"),
    ("maria",  "maria",  "Maria Lopez",   "employee", 40, "full_time", "2019-04-02", "front"),
    ("devon",  "devon",  "Devon Carter",  "employee", 40, "full_time", "2020-08-17", "front"),
    ("priya",  "priya",  "Priya Nair",    "employee", 40, "full_time", "2022-01-10", "front"),
    ("alex",   "alex",   "Alex Rivera",   "employee", 38, "full_time", "2023-03-01", "front"),
    ("sam",    "sam",    "Sam Chen",      "employee", 24, "part_time", "2023-09-15", "front"),
    ("jordan", "jordan", "Jordan Diaz",   "employee", 28, "part_time", "2024-05-20", "back"),
    ("taylor", "taylor", "Taylor Brooks", "employee", 20, "part_time", "2025-02-11", "back"),
    ("riley",  "riley",  "Riley Nguyen",  "employee", 16, "part_time", "2026-06-01", "back"),
    ("casey",  "casey",  "Casey Boots",   "employee", 24, "part_time", "2024-01-15", "back"),
    ("morgan", "morgan", "Morgan Vale",   "employee", 30, "full_time", "2021-10-04", "back"),
]

DEMO_SHIFTS = [
    # front of house — Mon-Sun
    ("front", "Mon", "09:00", "17:00", 3, None),
    ("front", "Tue", "09:00", "17:00", 3, None),
    ("front", "Wed", "09:00", "17:00", 2, None),
    ("front", "Thu", "09:00", "17:00", 2, None),
    ("front", "Fri", "09:00", "17:00", 3, None),
    ("front", "Sat", "10:00", "18:00", 4, "Weekend rush"),
    ("front", "Sun", "11:00", "16:00", 2, "Short day"),
    # back of house — Mon-Sun
    ("back", "Mon", "06:00", "14:00", 2, None),
    ("back", "Tue", "06:00", "14:00", 2, None),
    ("back", "Wed", "06:00", "14:00", 2, None),
    ("back", "Thu", "06:00", "14:00", 2, None),
    ("back", "Fri", "11:00", "20:00", 2, "Dinner prep + service"),
    ("back", "Sat", "10:00", "20:00", 3, "Weekend covers"),
    ("back", "Sun", "10:00", "16:00", 2, "Brunch"),
]

# Preferences: every employee ranks ALL 7 days (full-week pick rule — the
# full ranking is the backup plan). Ranks are chosen to guarantee conflicts:
# everyone wants Sat or Fri. (rank: day name)
PREFS = {
    # front of house
    "maria":  {"Mon": 1, "Tue": 2, "Fri": 3, "Sat": 4, "Wed": 5, "Thu": 6, "Sun": 7},
    "devon":  {"Fri": 1, "Sat": 2, "Mon": 3, "Tue": 4, "Wed": 5, "Thu": 6, "Sun": 7},
    "priya":  {"Fri": 1, "Sat": 2, "Wed": 3, "Sun": 4, "Mon": 5, "Tue": 6, "Thu": 7},
    "alex":   {"Sat": 1, "Fri": 2, "Tue": 3, "Mon": 4, "Wed": 5, "Thu": 6, "Sun": 7},
    "sam":    {"Fri": 1, "Mon": 2, "Sun": 3, "Tue": 4, "Wed": 5, "Thu": 6, "Sat": 7},
    # back of house
    "jordan": {"Sat": 1, "Fri": 2, "Thu": 3, "Mon": 4, "Tue": 5, "Wed": 6, "Sun": 7},
    "taylor": {"Fri": 1, "Sat": 2, "Wed": 3, "Mon": 4, "Tue": 5, "Thu": 6, "Sun": 7},
    "riley":  {"Sat": 1, "Sun": 2, "Fri": 3, "Mon": 4, "Tue": 5, "Wed": 6, "Thu": 7},
    "casey":  {"Fri": 1, "Mon": 2, "Tue": 3, "Wed": 4, "Thu": 5, "Sat": 6, "Sun": 7},
    "morgan": {"Sat": 1, "Fri": 2, "Mon": 3, "Wed": 4, "Thu": 5, "Tue": 6, "Sun": 7},
}


def monday_of(d):
    return d - timedelta(days=d.weekday())


def seed(appmod):
    """Wipe and reseed appmod's DB with the mock roster + full demo week.

    Every employee also gets pre-seeded picks (full 7-day ranking) so the
    site is immediately testable without manual input.
    """
    conn = appmod.db()
    for table in ("picks", "assignments", "notifications", "requests", "login_attempts", "shifts", "users"):
        conn.execute(f"DELETE FROM {table}")
    conn.executemany(
        "INSERT INTO users (username, password, name, role, weekly_hours,"
        " employment_type, hired_on, station) VALUES (?,?,?,?,?,?,?,?)", ROSTER)
    for username, *_ in ROSTER:
        conn.execute("UPDATE users SET password=? WHERE username=?",
                     (appmod.generate_password_hash(username), username))
    week = monday_of(date.today()).isoformat()
    conn.executemany(
        "INSERT INTO shifts (week_start, day, start_time, end_time, slots, note, area) "
        "VALUES (?,?,?,?,?,?,?)",
        [(week, d, s, e, n, note, area) for area, d, s, e, n, note in DEMO_SHIFTS])

    day_to_id = {(r["area"], r["day"]): r["id"]
                 for r in conn.execute("SELECT id, day, area FROM shifts")}
    for username, prefs in PREFS.items():
        uid = conn.execute(
            "SELECT id, station FROM users WHERE username=?", (username,)).fetchone()
        for day, rank in prefs.items():
            # employees only pick shifts in their own house
            sid = day_to_id[(uid["station"], day)]
            conn.execute(
                "INSERT INTO picks (user_id, shift_id, rank) VALUES (?,?,?)",
                (uid["id"], sid, rank))

    conn.commit()
    n = conn.execute("SELECT COUNT(*) c FROM users WHERE role='employee'").fetchone()["c"]
    d = conn.execute("SELECT COUNT(DISTINCT day) c FROM shifts").fetchone()["c"]
    p = conn.execute("SELECT COUNT(*) c FROM picks").fetchone()["c"]
    conn.close()
    assert n == 10, f"expected 10 employees, got {n}"
    assert d == 7, f"expected Mon-Sun demo week, got {d} days"
    assert p == sum(len(v) for v in PREFS.values()), "picks not fully seeded"
    return n, d, p
