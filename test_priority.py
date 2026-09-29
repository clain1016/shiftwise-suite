"""Tests for the auto-lineup priority scheduler (FT > PT, seniority, hours cap)."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))
import app as appmod
from collections import defaultdict

WEEK = "2026-09-28"


def fresh(overrides=None):
    DB = appmod.DB_PATH
    if DB.exists():
        DB.unlink()
    appmod.init_db()
    conn = appmod.db()
    conn.execute("DELETE FROM shifts")   # drop demo week, use controlled shifts
    if overrides:
        for uid, field, val in overrides:
            conn.execute(f"UPDATE users SET {field}=? WHERE username=?", (val, uid))
    conn.commit()
    return conn


def shift(conn, day, start, end, slots):
    cur = conn.execute(
        "INSERT INTO shifts (week_start, day, start_time, end_time, slots) VALUES (?,?,?,?,?)",
        (WEEK, day, start, end, slots))
    return cur.lastrowid


def pick(conn, username, sid, rank):
    uid = conn.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()[0]
    conn.execute("INSERT OR REPLACE INTO picks (user_id, shift_id, rank) VALUES (?,?,?)",
                 (uid, sid, rank))


def assignments(conn):
    rows = conn.execute(
        "SELECT s.day, u.username FROM assignments a "
        "JOIN shifts s ON s.id=a.shift_id JOIN users u ON u.id=a.user_id").fetchall()
    return rows


# --- 1. full-time beats part-time on a contested slot
conn = fresh()
mon = shift(conn, "Mon", "09:00", "17:00", 1)
for u in ("alex", "sam", "jordan"):
    pick(conn, u, mon, 1)
conn.commit()
appmod.run_scheduler(WEEK)
rows = assignments(conn)
assert [r["username"] for r in rows if r["day"] == "Mon"] == ["alex"], \
    f"full-time should win contested slot, got {rows}"
print("1. FT beats PT on contested slot: OK (Alex on Mon)")

# --- 2. seniority: earliest hire date wins within the same type
conn = fresh(overrides=[("alex", "employment_type", "part_time"),
                        ("sam", "employment_type", "part_time"),
                        ("jordan", "employment_type", "part_time")])
mon = shift(conn, "Mon", "09:00", "17:00", 1)
for u in ("alex", "sam", "jordan"):
    pick(conn, u, mon, 1)
conn.commit()
appmod.run_scheduler(WEEK)
rows = assignments(conn)
assert [r["username"] for r in rows if r["day"] == "Mon"] == ["alex"], \
    f"earliest hire should win among same type, got {rows}"
print("2. Seniority (earliest hire) wins within type: OK (Alex hired 2021 beats Sam 2023, Jordan 2024)")

# --- 3. same type, seniority flips when Alex is newest
conn = fresh(overrides=[("alex", "employment_type", "part_time"),
                        ("sam", "employment_type", "part_time"),
                        ("jordan", "employment_type", "part_time"),
                        ("alex", "hired_on", "2026-01-01")])
mon = shift(conn, "Mon", "09:00", "17:00", 1)
for u in ("alex", "sam", "jordan"):
    pick(conn, u, mon, 1)
conn.commit()
appmod.run_scheduler(WEEK)
rows = assignments(conn)
assert [r["username"] for r in rows if r["day"] == "Mon"] == ["sam"], \
    f"newest hire should lose, got {rows}"
print("3. Newest hire loses when type is equal: OK (Sam hired 2023 beats Alex hired 2026)")

# --- 4. hours cap: part-time employee never scheduled past their cap
conn = fresh(overrides=[("sam", "weekly_hours", 20)])
m1 = shift(conn, "Mon", "09:00", "17:00", 3)   # 8h each, 3 slots
m2 = shift(conn, "Tue", "09:00", "17:00", 3)
m3 = shift(conn, "Wed", "09:00", "17:00", 3)
pick(conn, "sam", m1, 1)
pick(conn, "sam", m2, 2)
pick(conn, "sam", m3, 3)
conn.commit()
appmod.run_scheduler(WEEK)
rows = [r for r in assignments(conn) if r["username"] == "sam"]
assert len(rows) == 2, f"Sam (cap 20h, 8h shifts) should get exactly 2 shifts, got {len(rows)}"
conn2 = appmod.db()
caps = conn2.execute(
    "SELECT message FROM notifications n JOIN users u ON u.id=n.user_id "
    "WHERE u.username='sam' AND message LIKE '%cap%'").fetchall()
conn2.close()
assert caps, "expected an over-cap notification for Sam"
print("4. Hours cap respected: OK (Sam capped at 2 shifts, notified about the cap)")

# --- 5. fallback still works: contested pick falls to next-ranked choice
conn = fresh()
mon = shift(conn, "Mon", "09:00", "17:00", 1)
tue = shift(conn, "Tue", "09:00", "17:00", 1)
pick(conn, "alex", mon, 1)
pick(conn, "alex", tue, 2)
pick(conn, "sam", mon, 1)   # sam is PT, loses Mon, falls to nothing -> 'no room'
conn.commit()
appmod.run_scheduler(WEEK)
rows = assignments(conn)
got = {(r["username"], r["day"]) for r in rows}
assert ("alex", "Mon") in got and ("sam", "Mon") not in got, got
conn2 = appmod.db()
fallback = conn2.execute(
    "SELECT COUNT(*) c FROM notifications n JOIN users u ON u.id=n.user_id "
    "WHERE u.username='sam' AND kind='conflict'").fetchone()["c"]
conn2.close()
assert fallback >= 1, "Sam should get a conflict notification"
print("5. Conflict fallback + notification: OK (Sam bumped off Mon, notified)")

# --- 6. roster page updates type/hire/cap
conn = fresh()
client = appmod.app.test_client()
client.post("/login", data={"username": "manager", "password": "manager"})
r = client.post("/manager/roster", data={
    "type_2": "part_time", "hired_2": "2025-05-05", "cap_2": "20",
    "type_3": "full_time", "hired_3": "2022-02-02", "cap_3": "40",
    "type_4": "part_time", "hired_4": "2024-11-20", "cap_4": "35",
}, follow_redirects=True)
assert b"Roster updated" in r.data
check = conn.execute("SELECT username, employment_type, hired_on, weekly_hours "
                     "FROM users ORDER BY id").fetchall()
conn.close()
by_user = {r["username"]: r for r in check}
assert by_user["alex"]["employment_type"] == "part_time" and by_user["alex"]["hired_on"] == "2025-05-05"
assert by_user["sam"]["employment_type"] == "full_time"
print("6. Roster page saves type/hire-date/cap: OK")

print("\nALL PRIORITY TESTS PASSED")
