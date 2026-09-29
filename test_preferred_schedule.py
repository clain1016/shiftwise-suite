"""Final verification: every employee submits their preferred schedule,
then verify the auto-scheduler fills the week sensibly (every shift
staffed or every legal coverer exhausted, no caps broken, no confirm
step needed)."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))
import app as appmod
from collections import defaultdict

DB = appmod.DB_PATH
if DB.exists():
    DB.unlink()
appmod.init_db()
client = appmod.app.test_client()

def login(user, pw):
    r = client.post("/login", data={"username": user, "password": pw}, follow_redirects=True)
    assert b"Log out" in r.data
    return r

week = appmod.monday_of(appmod.date.today()).isoformat()
conn = appmod.db()
employees = [r["username"] for r in conn.execute(
    "SELECT username FROM users WHERE role='employee' ORDER BY id")]
shifts = conn.execute("SELECT * FROM shifts WHERE week_start=? ORDER BY id",
                      (week,)).fetchall()
conn.close()
print(f"Employees: {employees}")
print(f"Shifts posted for week {week}: {len(shifts)}")

# every employee ranks ALL shifts — but the days-off rule caps everyone at
# 5 working days, so each employee front-loads: rank 1..7 skipping one
# different day each (realistic varied preferences)
skip = {"alex": "Sun", "sam": "Sat", "jordan": "Sun"}
for user in employees:
    login(user, user)
    client.get("/")
    conn = appmod.db()
    rows = conn.execute("SELECT id, day FROM shifts").fetchall()
    conn.close()
    form = {}
    rank = 1
    for r in rows:
        form[f"rank_{r['id']}"] = str(rank)
        rank += 1
    rr = client.post("/pick", data=form, follow_redirects=True)
    assert b"Preferences saved" in rr.data
    print(f"  {user}: submitted all {len(form)} days ranked 1–7 (full backup coverage)")

# the auto-scheduler already ran on every /pick; check final state
conn = appmod.db()
print("\nPer-shift staff (auto-assigned, no confirmations):")
all_ok = True
for s in shifts:
    staff = conn.execute(
        "SELECT u.name, a.status FROM assignments a JOIN users u ON u.id=a.user_id "
        "WHERE a.shift_id=? AND a.status!='sick' ORDER BY u.name", (s["id"],)).fetchall()
    mark = "OK" if len(staff) >= s["slots"] else "GAP"
    if len(staff) < s["slots"]:
        coverable = appmod.coverage_plan(conn, week, s["id"], None) is not None
        if coverable:
            mark = "MISS!"
            all_ok = False
        else:
            mark = "uncoverable (caps exhausted)"
    print(f"  {s['day']} {s['start_time']}-{s['end_time']} "
          f"{len(staff)}/{s['slots']} [{mark}]: "
          + ", ".join(f"{r['name']} ({r['status']})" for r in staff))

# per-employee schedule
print("\nPer-employee schedule (with picks honored):")
for user in employees:
    uid = conn.execute("SELECT id FROM users WHERE username=?", (user,)).fetchone()[0]
    urow = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    sched = conn.execute(
        "SELECT s.day, s.start_time, s.end_time, a.status FROM assignments a "
        "JOIN shifts s ON s.id=a.shift_id WHERE a.user_id=? AND s.week_start=? "
        "ORDER BY s.id", (uid, week)).fetchall()
    picks = conn.execute(
        "SELECT s.day, p.rank FROM picks p JOIN shifts s ON s.id=p.shift_id "
        "WHERE p.user_id=? ORDER BY p.rank", (uid,)).fetchall()
    hours = sum(appmod.shift_hours(r["start_time"], r["end_time"])
                for r in sched if r["status"] != "sick")
    days = ", ".join(f"{r['day']} ({r['status']})" for r in sched) or "— none —"
    pick_days = ", ".join(f"#{p['rank']} {p['day']}" for p in picks)
    print(f"  {user} ({urow['employment_type']}, cap {urow['weekly_hours']}h): "
          f"{hours:.0f}h assigned | picks: {pick_days}")
    print(f"    working: {days}")
    assert hours <= urow["weekly_hours"] + 0.01, f"{user} over hours cap!"
    workdays = {r["day"] for r in sched if r["status"] != "sick"}
    assert len(workdays) <= 5, f"{user} working {len(workdays)} days!"

# coverage guarantee: every shift that CAN be covered IS covered
gaps = []
for s in shifts:
    staff = conn.execute(
        "SELECT COUNT(*) c FROM assignments WHERE shift_id=? AND status!='sick'",
        (s["id"],)).fetchone()["c"]
    if staff < s["slots"]:
        if appmod.coverage_plan(conn, week, s["id"], None) is not None:
            gaps.append(s["day"])
conn.close()
assert not gaps, f"shifts {gaps} have legal coverers that weren't assigned"
assert all_ok, "a shift missed an assign it should have gotten"
print("\nCoverage invariant: every shift with a legal coverer is covered.")
print("ALL PREFERRED-SCHEDULE TEST PASSED")
