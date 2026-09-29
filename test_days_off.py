"""Tests for the minimum 2-days-off rule in the auto-scheduler."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))
import app as appmod

WEEK = "2026-09-28"


def fresh(overrides=None):
    DB = appmod.DB_PATH
    if DB.exists():
        DB.unlink()
    appmod.init_db()
    conn = appmod.db()
    conn.execute("DELETE FROM shifts")
    conn.execute("DELETE FROM picks")
    if overrides:
        for uid, field, val in overrides:
            conn.execute(f"UPDATE users SET {field}=? WHERE username=?", (val, uid))
    conn.commit()
    return conn


def shift(conn, day):
    return conn.execute(
        "INSERT INTO shifts (week_start, day, start_time, end_time, slots) VALUES (?,?,?,?,?)",
        (WEEK, day, "09:00", "17:00", 3)).lastrowid


def pick(conn, username, sid, rank):
    uid = conn.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()[0]
    conn.execute("INSERT OR REPLACE INTO picks (user_id, shift_id, rank) VALUES (?,?,?)",
                 (uid, sid, rank))


def days_for(conn, username):
    rows = conn.execute(
        "SELECT s.day FROM assignments a JOIN users u ON u.id=a.user_id "
        "JOIN shifts s ON s.id=a.shift_id WHERE u.username=?", (username,)).fetchall()
    return {r["day"] for r in rows}


# --- 1. employee with picks on all 7 days gets at most 5 working days
conn = fresh(overrides=[("alex", "weekly_hours", 80)])
ids = {d: shift(conn, d) for d in ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")}
for i, d in enumerate(("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"), 1):
    pick(conn, "alex", ids[d], i)   # FT, plenty of cap: would take all 7
conn.commit()
appmod.run_scheduler(WEEK)
conn = appmod.db()
d_alex = days_for(conn, "alex")
conn.close()
assert len(d_alex) == 5, f"max 5 working days (2 off), got {len(d_alex)}: {d_alex}"
assert d_alex == {"Mon", "Tue", "Wed", "Thu", "Fri"}, "highest-ranked days win"
print("1. Picks on all 7 days -> exactly 5 days worked, 2 off (top-ranked kept): OK")

# --- 2. a 6-day picker also gets capped at 5
conn = fresh(overrides=[("alex", "weekly_hours", 80)])
ids = {d: shift(conn, d) for d in ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat")}
for i, d in enumerate(("Mon", "Tue", "Wed", "Thu", "Fri", "Sat"), 1):
    pick(conn, "alex", ids[d], i)
conn.commit()
appmod.run_scheduler(WEEK)
conn = appmod.db()
d_devon = days_for(conn, "alex")
note = conn.execute(
    "SELECT message FROM notifications n JOIN users u ON u.id=n.user_id "
    "WHERE u.username='alex' AND message LIKE '%days off%'").fetchall()
conn.close()
assert len(d_devon) == 5, f"expected 5 days, got {len(d_devon)}"
assert note, "expected a days-off notification"
print("2. 6-day picker capped at 5, notified why: OK")

# --- 3. two shifts on the SAME day count once (8h+8h same day = 1 working day)
conn = fresh(overrides=[("alex", "weekly_hours", 60)])
m1 = conn.execute(
    "INSERT INTO shifts (week_start, day, start_time, end_time, slots) VALUES (?,?,?,?,?)",
    (WEEK, "Mon", "07:00", "15:00", 3)).lastrowid
m2 = conn.execute(
    "INSERT INTO shifts (week_start, day, start_time, end_time, slots) VALUES (?,?,?,?,?)",
    (WEEK, "Mon", "15:00", "23:00", 3)).lastrowid
t = conn.execute(
    "INSERT INTO shifts (week_start, day, start_time, end_time, slots) VALUES (?,?,?,?,?)",
    (WEEK, "Tue", "09:00", "17:00", 3)).lastrowid
w = conn.execute(
    "INSERT INTO shifts (week_start, day, start_time, end_time, slots) VALUES (?,?,?,?,?)",
    (WEEK, "Wed", "09:00", "17:00", 3)).lastrowid
th = conn.execute(
    "INSERT INTO shifts (week_start, day, start_time, end_time, slots) VALUES (?,?,?,?,?)",
    (WEEK, "Thu", "09:00", "17:00", 3)).lastrowid
f = conn.execute(
    "INSERT INTO shifts (week_start, day, start_time, end_time, slots) VALUES (?,?,?,?,?)",
    (WEEK, "Fri", "09:00", "17:00", 3)).lastrowid
pick(conn, "alex", m1, 1)
pick(conn, "alex", t, 2)
pick(conn, "alex", w, 3)
pick(conn, "alex", th, 4)
pick(conn, "alex", f, 5)
pick(conn, "alex", m2, 6)   # second Monday shift: same day, 60h cap allows
conn.commit()
appmod.run_scheduler(WEEK)
conn = appmod.db()
d_alex = days_for(conn, "alex")
n_shifts = conn.execute(
    "SELECT COUNT(*) c FROM assignments a JOIN users u ON u.id=a.user_id "
    "WHERE u.username='alex'").fetchone()["c"]
conn.close()
assert len(d_alex) == 5 and n_shifts == 6, \
    f"double-Monday should be allowed within 5 distinct days (6 shifts), got {n_shifts} on {d_alex}"
print("3. Two shifts on the same day = 1 working day (double allowed): OK")

# --- 4. days-off rule beats hours cap interplay: PT with high cap still limited to 5 days
conn = fresh(overrides=[("sam", "employment_type", "part_time"),
                        ("sam", "weekly_hours", 80)])
ids = {d: shift(conn, d) for d in ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")}
for i, d in enumerate(("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"), 1):
    pick(conn, "sam", ids[d], i)
conn.commit()
appmod.run_scheduler(WEEK)
conn = appmod.db()
d_sam = days_for(conn, "sam")
conn.close()
assert len(d_sam) == 5, f"even with a huge cap, max 5 days: got {len(d_sam)}"
print("4. Days-off rule overrides a large hours cap: OK")

# --- 5. regular demo week still assigns sensibly (no regression)
conn = fresh()
if appmod.DB_PATH.exists():
    appmod.DB_PATH.unlink()
appmod.init_db()
appmod.run_scheduler(WEEK)
conn = appmod.db()
for u in ("alex", "sam", "jordan"):
    d = days_for(conn, u)
    assert len(d) <= 5, f"{u} over 5 days"
conn.close()
print("5. Standard week: everyone within 5 working days: OK")

print("\nALL DAYS-OFF TESTS PASSED")
