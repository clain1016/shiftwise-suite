"""Full scenario simulation on the mock roster (8 employees, Mon-Sun).

Every employee submits their preferred schedule through the real HTTP
routes, then conflicts of every kind are injected one at a time so each
reaction is visible:

  Phase 1:  all 8 submit preferences -> auto-assignment (contention on Fri/Sat)
  Phase 2:  sick call        -> instant auto-coverage
  Phase 3:  day-off request  -> drops + rebuild
  Phase 4:  vacation request -> in-range drops + rebuild
  Phase 5:  switch request into a FULL shift -> manager approves -> auto-denied
  Phase 6:  swap request     -> instant auto-coverage, requester fully released
  Phase 7:  hours-cap stress (riley picks 8h days despite 16h cap)
  Phase 8:  days-off stress  (sam ranks 6 days)
  Phase 9:  manager unassigns an at-cap employee -> someone else backfills
  Phase 10: two simultaneous sick calls on the same shift -> two coverers

Run against the MOCK DB (scheduler-mock), never the real one.
Prints a schedule snapshot after every phase. The mock is left in the
final state so you can click through it on port 5001.
"""
import sys, os
MOCKDIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, MOCKDIR)
os.chdir(MOCKDIR)
import sqlite3
from pathlib import Path
import app as appmod
import mock_seed

DAYS = appmod.DAYS
week = appmod.monday_of(appmod.date.today()).isoformat()

# ---------------- fresh mock DB
for suffix in ("", "-wal", "-shm"):
    p = Path(str(appmod.DB_PATH) + suffix)
    if p.exists():
        p.unlink()
appmod.init_db(seed_demo=True)
conn = sqlite3.connect(appmod.DB_PATH)
for t in ("users", "shifts", "picks", "assignments", "notifications", "requests"):
    conn.execute(f"DELETE FROM {t}")
conn.commit(); conn.close()
mock_seed.seed(appmod)

client = appmod.app.test_client()

def login(u):
    r = client.post("/login", data={"username": u, "password": u}, follow_redirects=True)
    assert b"Log out" in r.data, f"login failed for {u}"

def full_form(day_to_id, preferred_days):
    order = list(preferred_days) + [d for d in appmod.DAYS if d not in preferred_days]
    return {f"rank_{day_to_id[day]}": str(rank)
            for rank, day in enumerate(order, 1)}

def snapshot(title):
    conn = appmod.db()
    print(f"\n--- {title} ---")
    for s in conn.execute(
            "SELECT * FROM shifts WHERE week_start=? ORDER BY id", (week,)):
        staff = conn.execute(
            "SELECT u.name, a.status FROM assignments a JOIN users u ON u.id=a.user_id "
            "WHERE a.shift_id=? AND a.status NOT IN ('sick','swap_requested') ORDER BY u.id", (s["id"],)).fetchall()
        sick = conn.execute(
            "SELECT u.name FROM assignments a JOIN users u ON u.id=a.user_id "
            "WHERE a.shift_id=? AND a.status='sick'", (s["id"],)).fetchall()
        flag = "OK" if len(staff) >= s["slots"] else "GAP"
        line = (f"  {s['day']:<4} {s['start_time']}-{s['end_time']} "
                f"{len(staff)}/{s['slots']} [{flag}] ")
        line += ", ".join(f"{r['name'].split()[0]}({r['status'][:4]})" for r in staff)
        if sick:
            line += f"   <<out sick: {', '.join(r['name'].split()[0] for r in sick)}>>"
        print(line)
    print("  per-employee:")
    for u in conn.execute("SELECT * FROM users WHERE role='employee' ORDER BY id"):
        rows = conn.execute(
            "SELECT s.day, a.status, s.start_time, s.end_time FROM assignments a "
            "JOIN shifts s ON s.id=a.shift_id "
            "WHERE a.user_id=? AND s.week_start=? ORDER BY s.id", (u["id"], week)).fetchall()
        hrs = sum(appmod.shift_hours(r["start_time"], r["end_time"])
                  for r in rows if r["status"] not in ("sick", "swap_requested"))
        wd = ", ".join(f"{r['day']}({r['status'][:4]})" for r in rows) or "none"
        print(f"    {u['name'].split()[0]:<7} cap {u['weekly_hours']}h -> {hrs:>4.0f}h | {wd}")
    conn.close()

def emp_uid(username):
    conn = appmod.db()
    uid = conn.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()[0]
    conn.close()
    return uid

def active_staff(shift_id):
    conn = appmod.db()
    rows = conn.execute(
        "SELECT a.user_id, u.name FROM assignments a JOIN users u ON u.id=a.user_id "
        "WHERE a.shift_id=? AND a.status NOT IN ('sick','swap_requested')",
        (shift_id,)).fetchall()
    conn.close()
    return {row["user_id"]: row["name"] for row in rows}

def holding(username):
    conn = appmod.db()
    rows = conn.execute(
        "SELECT s.id, s.day, a.status FROM assignments a JOIN shifts s ON s.id=a.shift_id "
        "WHERE a.user_id=? AND s.week_start=? ORDER BY s.id",
        (emp_uid(username), week)).fetchall()
    conn.close()
    return rows

def last_conflicts(username, n=3):
    conn = appmod.db()
    msgs = [r["message"] for r in conn.execute(
        "SELECT message FROM notifications WHERE user_id=? AND kind='conflict' "
        "ORDER BY id DESC LIMIT ?", (emp_uid(username), n))]
    conn.close()
    return msgs

# ================================================= PHASE 1
print("=" * 64)
print("PHASE 1 — every employee submits their preferred schedule")
print("=" * 64)
PREFS = {
    "maria":  {"Mon": 1, "Tue": 2, "Fri": 3, "Sat": 4},
    "devon":  {"Fri": 1, "Sat": 2, "Mon": 3},
    "priya":  {"Fri": 1, "Sat": 2, "Wed": 3, "Sun": 4},
    "alex":   {"Sat": 1, "Fri": 2, "Tue": 3},
    "sam":    {"Fri": 1, "Mon": 2, "Sun": 3},
    "jordan": {"Sat": 1, "Fri": 2, "Thu": 3},
    "taylor": {"Fri": 1, "Sat": 2, "Wed": 3, "Mon": 4},
    "riley":  {"Sat": 1, "Sun": 2, "Fri": 3},
}
for user, prefs in PREFS.items():
    login(user)
    client.get("/")
    conn = appmod.db()
    day_to_id = {r["day"]: r["id"] for r in conn.execute("SELECT id, day FROM shifts")}
    conn.close()
    form = full_form(day_to_id, [day for day, _ in sorted(
        prefs.items(), key=lambda item: item[1])])
    response = client.post("/pick", data=form, follow_redirects=True)
    assert b"Preferences saved" in response.data, user
snapshot("after all 8 submit preferences (auto-assigned, no confirmations)")

# ================================================= PHASE 2: sick call
print("=" * 64)
print("PHASE 2 — maria calls out sick on her Monday shift")
print("=" * 64)
login("maria")
mon = next(s for s in holding("maria") if s["day"] == "Mon")
before = active_staff(mon["id"])
client.post(f"/request/sick/{mon['id']}", follow_redirects=True)
new_staff = set(active_staff(mon["id"]).items()) - set(before.items())
print(f"  maria marked 'out sick'; instant coverer: "
      f"{', '.join(name for _, name in new_staff) if new_staff else 'NOBODY — manager alerted'}")
snapshot("after maria's sick call")

# ================================================= PHASE 3: day off
print("=" * 64)
print("PHASE 3 — taylor requests Tuesday off")
print("=" * 64)
login("taylor")
client.post("/request/day_off", data={"day": "Tue"}, follow_redirects=True)
snapshot("after taylor's day-off (Tue)")

# ================================================= PHASE 4: vacation
print("=" * 64)
print("PHASE 4 — sam requests vacation from today through next week")
print("=" * 64)
login("sam")
today = appmod.date.today()
vend = (appmod.monday_of(today) + appmod.timedelta(days=13)).isoformat()
client.post("/request/vacation",
            data={"vac_start": today.isoformat(), "vac_end": vend},
            follow_redirects=True)
conn = appmod.db()
sam_left = [r2["day"] for r2 in conn.execute(
    "SELECT s.day FROM assignments a JOIN shifts s ON s.id=a.shift_id "
    "WHERE a.user_id=? AND s.week_start=? AND a.status NOT IN ('sick','swap_requested')",
    (emp_uid("sam"), week))]
conn.close()
print(f"  RESOLVED: sam's in-range shifts dropped; he still holds: {sam_left or 'none'}")
snapshot("after sam's vacation request")

# ================================================= PHASE 5: switch into full
print("=" * 64)
print("PHASE 5 — jordan requests a switch into a FULL shift; manager approves")
print("=" * 64)
login("jordan")
conn = appmod.db()
j_shift = conn.execute(
    "SELECT s.id, s.day FROM assignments a JOIN shifts s ON s.id=a.shift_id "
    "WHERE a.user_id=? AND s.week_start=? AND a.status='notified' LIMIT 1",
    (emp_uid("jordan"), week)).fetchone()
full_target = conn.execute(
    "SELECT s.id, s.day FROM shifts s WHERE s.week_start=? AND s.id NOT IN "
    "(SELECT a.shift_id FROM assignments a WHERE a.user_id=?) AND "
    "(SELECT COUNT(*) FROM assignments a WHERE a.shift_id=s.id AND a.status NOT IN ('sick','swap_requested')) >= s.slots "
    "ORDER BY s.id LIMIT 1", (week, emp_uid("jordan"))).fetchone()
conn.close()
if j_shift and full_target:
    client.post(f"/request/switch/{j_shift['id']}",
                data={"target_shift": str(full_target["id"])}, follow_redirects=True)
    client.post("/logout")
    login("manager")
    conn = appmod.db()
    rid = conn.execute(
        "SELECT id FROM requests WHERE kind='switch' ORDER BY id DESC LIMIT 1").fetchone()[0]
    conn.close()
    client.post(f"/manager/requests/{rid}/approve", follow_redirects=True)
    conn = appmod.db()
    still = conn.execute(
        "SELECT s.day FROM assignments a JOIN shifts s ON s.id=a.shift_id "
        "WHERE a.user_id=? AND a.shift_id=?", (emp_uid("jordan"), j_shift["id"])).fetchone()
    st = conn.execute("SELECT status FROM requests WHERE id=?", (rid,)).fetchone()["status"]
    conn.close()
    print(f"  RESOLVED: switch {j_shift['day']} -> {full_target['day']} "
          f"auto-DENIED ({st}) — target full; jordan keeps {j_shift['day']}: "
          f"{'yes' if still else 'no'}")
else:
    print("  no full-shift target available — skipping")
snapshot("after the denied switch (capacity guard)")

# ================================================= PHASE 6: swap
print("=" * 64)
print("PHASE 6 — devon requests a swap on one of his shifts")
print("=" * 64)
login("devon")
d_shifts = [s for s in holding("devon") if s["status"] == "notified"]
if d_shifts:
    tgt = d_shifts[0]
    before = active_staff(tgt["id"])
    client.post(f"/swap/{tgt['id']}", follow_redirects=True)
    conn = appmod.db()
    gone = conn.execute(
        "SELECT 1 FROM assignments WHERE shift_id=? AND user_id=?",
        (tgt["id"], emp_uid("devon"))).fetchone()
    conn.close()
    new_staff = set(active_staff(tgt["id"]).items()) - set(before.items())
    print(f"  devon requested a swap on {tgt['day']} (row removed: {'yes' if not gone else 'no'})"
          f"; auto-coverer: {', '.join(name for _, name in new_staff) if new_staff else 'NOBODY — manager alerted'}")
else:
    print("  devon holds no swappable shift — skipping")
snapshot("after devon's swap request")

# ================================================= PHASE 7: hours cap
print("=" * 64)
print("PHASE 7 — riley (16h cap) greedily ranks five 8h days")
print("=" * 64)
login("riley")
client.get("/")
conn = appmod.db()
day_to_id = {r["day"]: r["id"] for r in conn.execute("SELECT id, day FROM shifts")}
conn.close()
form = full_form(day_to_id, ["Mon", "Tue", "Wed", "Fri", "Sat"])
assert b"Preferences saved" in client.post(
    "/pick", data=form, follow_redirects=True).data
conn = appmod.db()
notifs = [n["message"] for n in conn.execute(
    "SELECT message FROM notifications WHERE user_id=? AND kind='conflict' "
    "ORDER BY id DESC LIMIT 3", (emp_uid("riley"),))]
conn.close()
for n in notifs:
    print(f"  riley notified: {n}")
snapshot("after riley tries to over-pick (cap enforcement)")

# ================================================= PHASE 8: days off
print("=" * 64)
print("PHASE 8 — sam prioritizes six days despite the 2-days-off rule")
print("=" * 64)
login("sam")
client.get("/")
conn = appmod.db()
day_to_id = {r["day"]: r["id"] for r in conn.execute("SELECT id, day FROM shifts")}
conn.close()
form = full_form(day_to_id, ["Mon", "Tue", "Wed", "Thu", "Fri", "Sun"])
assert b"Preferences saved" in client.post(
    "/pick", data=form, follow_redirects=True).data
conn = appmod.db()
notifs = [n["message"] for n in conn.execute(
    "SELECT message FROM notifications WHERE user_id=? AND kind='conflict' "
    "ORDER BY id DESC LIMIT 3", (emp_uid("sam"),))]
conn.close()
for n in notifs:
    print(f"  sam notified: {n}")
snapshot("after sam's 6-day over-pick (days-off enforcement)")

# ================================================= PHASE 9: unassign at cap
print("=" * 64)
print("PHASE 9 — manager unassigns riley (at 16h cap) from Monday")
print("=" * 64)
login("manager")
conn = appmod.db()
victim = conn.execute(
    "SELECT a.shift_id, a.user_id, s.day, u.name FROM assignments a "
    "JOIN shifts s ON s.id=a.shift_id JOIN users u ON u.id=a.user_id "
    "WHERE s.week_start=? AND s.day='Mon' AND u.username='riley' "
    "AND a.status='notified'", (week,)).fetchone()
conn.close()
if victim:
    client.post(f"/manager/unassign/{victim['shift_id']}/{victim['user_id']}",
                follow_redirects=True)
    conn = appmod.db()
    new = conn.execute(
        "SELECT u.name FROM assignments a JOIN users u ON u.id=a.user_id "
        "WHERE a.shift_id=? AND a.user_id!=? AND a.status NOT IN ('sick','swap_requested') "
        "ORDER BY a.id DESC LIMIT 1", (victim["shift_id"], victim["user_id"])).fetchone()
    conn.close()
    riley_now = [r["day"] for r in holding("riley")]
    print(f"  RESOLVED: riley pulled off Mon (now holds: {riley_now or 'nothing'}); "
          f"backfilled by: {new['name'] if new else 'NOBODY'}")
else:
    print("  riley not holding Mon — skipping")
snapshot("after manager unassign + auto-backfill")

# ================================================= PHASE 10: double sick
print("=" * 64)
print("PHASE 10 — two Monday holders call out sick at once")
print("=" * 64)
conn = appmod.db()
mon_holders = conn.execute(
    "SELECT a.user_id, a.shift_id, u.username FROM assignments a "
    "JOIN users u ON u.id=a.user_id JOIN shifts s ON s.id=a.shift_id "
    "WHERE s.week_start=? AND s.day='Mon' AND a.status='notified' "
    "ORDER BY a.id LIMIT 2", (week,)).fetchall()
conn.close()
for h in mon_holders:
    login(h["username"])
    client.post(f"/request/sick/{h['shift_id']}", follow_redirects=True)
if mon_holders:
    sid = mon_holders[0]["shift_id"]
    conn = appmod.db()
    sick_now = [r["name"].split()[0] for r in conn.execute(
        "SELECT u.name FROM assignments a JOIN users u ON u.id=a.user_id "
        "WHERE a.shift_id=? AND a.status='sick'", (sid,))]
    covers = [r["name"].split()[0] for r in conn.execute(
        "SELECT u.name FROM assignments a JOIN users u ON u.id=a.user_id "
        "WHERE a.shift_id=? AND a.status='notified' ORDER BY a.id", (sid,))]
    conn.close()
    print(f"  out sick on Mon: {sick_now}; covers assigned: {covers or 'none — manager alerted'}")
else:
    print("  no Monday holders to test with")
snapshot("final schedule after conflict scenarios")

print(f"\nScenario database: {appmod.DB_PATH}")
print("Manager login: manager/manager · employees: username = password.")
