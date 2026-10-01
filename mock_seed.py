"""Shared canonical mock seeding for the ShiftWise environment.

Keeps the fake roster and the Mon-Sun demo week in one place.
Roster is split by house: 7 front-of-house and 7 back-of-house employees.
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
    ("alex",   "alex",   "Alex Rivera",   "employee", 40, "full_time", "2023-03-01", "front"),
    ("sam",    "sam",    "Sam Chen",      "employee", 40, "part_time", "2023-09-15", "front"),
    ("erin",   "erin",   "Erin Park",     "employee", 40, "part_time", "2025-04-10", "front"),
    ("quinn",  "quinn",  "Quinn Bell",    "employee", 40, "part_time", "2026-01-08", "front"),
    ("jordan", "jordan", "Jordan Diaz",   "employee", 40, "part_time", "2024-05-20", "back"),
    ("taylor", "taylor", "Taylor Brooks", "employee", 40, "part_time", "2025-02-11", "back"),
    ("riley",  "riley", "Riley Nguyen",  "employee", 40, "part_time", "2026-06-01", "back"),
    ("casey",  "casey", "Casey Boots",   "employee", 40, "part_time", "2024-01-15", "back"),
    ("morgan", "morgan", "Morgan Vale",   "employee", 40, "full_time", "2021-10-04", "back"),
    ("lee",    "lee",    "Lee Morgan",    "employee", 40, "part_time", "2025-07-12", "back"),
    ("sky",    "sky",    "Sky Rivera",    "employee", 40, "part_time", "2026-02-20", "back"),
]

DEMO_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
SHIFT_WINDOWS = (
    ("07:00", "15:00", "Opening"),
    ("11:00", "19:00", "Mid shift"),
    ("15:00", "23:00", "Closing"),
)
PEAK_DAYS = {"Thu", "Fri", "Sat"}
DEMO_SHIFTS = [
    (area, day, start, end, 2 if day in PEAK_DAYS else 1, label)
    for area in ("front", "back")
    for day in DEMO_DAYS
    for start, end, label in SHIFT_WINDOWS
]

# Each day-rank ordering expands to a unique rank for all three shifts on all
# seven days: ranks 1–21. Staff preferences are prefilled so the mock is ready
# to use without manual setup.
PREFS = {
    # front of house
    "maria":  {"Mon": 1, "Tue": 2, "Fri": 3, "Sat": 4, "Wed": 5, "Thu": 6, "Sun": 7},
    "devon":  {"Fri": 1, "Sat": 2, "Mon": 3, "Tue": 4, "Wed": 5, "Thu": 6, "Sun": 7},
    "priya":  {"Fri": 1, "Sat": 2, "Wed": 3, "Sun": 4, "Mon": 5, "Tue": 6, "Thu": 7},
    "alex":   {"Sat": 1, "Fri": 2, "Tue": 3, "Mon": 4, "Wed": 5, "Thu": 6, "Sun": 7},
    "sam":    {"Fri": 1, "Mon": 2, "Sun": 3, "Tue": 4, "Wed": 5, "Thu": 6, "Sat": 7},
    "erin":   {"Sat": 1, "Fri": 2, "Thu": 3, "Mon": 4, "Tue": 5, "Wed": 6, "Sun": 7},
    "quinn":  {"Wed": 1, "Thu": 2, "Fri": 3, "Sat": 4, "Mon": 5, "Tue": 6, "Sun": 7},
    # back of house
    "jordan": {"Sat": 1, "Fri": 2, "Thu": 3, "Mon": 4, "Tue": 5, "Wed": 6, "Sun": 7},
    "taylor": {"Fri": 1, "Sat": 2, "Wed": 3, "Mon": 4, "Tue": 5, "Thu": 6, "Sun": 7},
    "riley":  {"Sat": 1, "Sun": 2, "Fri": 3, "Mon": 4, "Tue": 5, "Wed": 6, "Thu": 7},
    "casey":  {"Fri": 1, "Mon": 2, "Tue": 3, "Wed": 4, "Thu": 5, "Sat": 6, "Sun": 7},
    "morgan": {"Sat": 1, "Fri": 2, "Mon": 3, "Wed": 4, "Thu": 5, "Tue": 6, "Sun": 7},
    "lee":    {"Thu": 1, "Fri": 2, "Sat": 3, "Mon": 4, "Tue": 5, "Wed": 6, "Sun": 7},
    "sky":    {"Tue": 1, "Wed": 2, "Thu": 3, "Fri": 4, "Sat": 5, "Mon": 6, "Sun": 7},
}


def monday_of(d):
    return d - timedelta(days=d.weekday())


def seed(appmod, force=False):
    """Wipe and reseed appmod's DB with the mock roster + full demo week.

    Every employee also gets pre-seeded picks (full 21-shift ranking) so the
    site is immediately testable without manual input.

    Refuses to wipe a non-empty database unless force=True, so an accidental
    call can never destroy real data.
    """
    conn = appmod.db()
    if not force:
        existing = conn.execute("SELECT 1 FROM users LIMIT 1").fetchone()
        if existing is not None:
            conn.close()
            raise RuntimeError(
                "mock_seed.seed() refuses to wipe a non-empty database "
                "without force=True"
            )
    for table in ("picks", "coverage_preferences", "assignments", "notifications",
                  "requests", "login_attempts", "shifts", "users"):
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

    # PostgreSQL returns TIME columns as datetime.time, SQLite as TEXT;
    # normalize to HH:MM so the lookup key matches SHIFT_WINDOWS.
    shift_ids = {(r["area"], r["day"], str(r["start_time"])[:5]): r["id"]
                 for r in conn.execute("SELECT id, day, area, start_time FROM shifts")}
    for username, prefs in PREFS.items():
        uid = conn.execute(
            "SELECT id, station FROM users WHERE username=?", (username,)).fetchone()
        for day, day_rank in prefs.items():
            # employees only pick shifts in their own house
            for window_rank, (start, _end, _label) in enumerate(SHIFT_WINDOWS, start=1):
                sid = shift_ids[(uid["station"], day, start)]
                rank = (day_rank - 1) * len(SHIFT_WINDOWS) + window_rank
                conn.execute(
                    "INSERT INTO picks (user_id, shift_id, rank) VALUES (?,?,?)",
                    (uid["id"], sid, rank))

    conn.commit()
    n = conn.execute("SELECT COUNT(*) c FROM users WHERE role='employee'").fetchone()["c"]
    d = conn.execute("SELECT COUNT(DISTINCT day) c FROM shifts").fetchone()["c"]
    p = conn.execute("SELECT COUNT(*) c FROM picks").fetchone()["c"]
    conn.close()
    assert n == 14, f"expected 14 employees, got {n}"
    assert d == 7, f"expected Mon-Sun demo week, got {d} days"
    assert p == sum(len(v) for v in PREFS.values()) * len(SHIFT_WINDOWS), "picks not fully seeded"
    return n, d, p
