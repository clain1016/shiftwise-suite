"""Randomized conflict simulation: seeded RNG picks random employees and
conflict types each run. Same lifecycle as scenario_demo but chaos mode."""
import random
import sys
import pathlib
from datetime import date, timedelta
sys.path.insert(0, "/home/cody/scheduler-mock")
import app as appmod
from mock_seed import seed

appmod.DB_PATH = "/home/cody/scheduler-mock/mock.db"
if not (pathlib.Path("/home/cody/scheduler-mock/mock.db").exists()):
    appmod.init_db()
seed(appmod)
appmod.init_db()
rng = random.Random(7)

client = appmod.app.test_client()
WEEK = appmod.monday_of(date.today()).isoformat()

def login(u):
    client.get("/logout")
    client.post("/login", data={"username": u, "password": u})

def uid(u):
    c = appmod.db()
    r = c.execute("SELECT id FROM users WHERE username=?", (u,)).fetchone()[0]
    c.close()
    return r

def emp_names():
    c = appmod.db()
    r = [x["username"] for x in c.execute(
        "SELECT username FROM users WHERE role='employee'")]
    c.close()
    return r

EMPS = emp_names()

def shift_id(day):
    c = appmod.db()
    r = c.execute("SELECT id FROM shifts WHERE week_start=? AND day=?",
                  (WEEK, day)).fetchone()
    c.close()
    return r["id"] if r else None

def my_shifts(u, status="notified"):
    c = appmod.db()
    r = c.execute(
        "SELECT s.id, s.day, s.start_time, a.status FROM assignments a "
        "JOIN shifts s ON s.id=a.shift_id WHERE a.user_id=? AND a.status=? "
        "AND s.week_start=?", (uid(u), status, WEEK)).fetchall()
    c.close()
    return r

def staffed():
    c = appmod.db()
    r = c.execute(
        "SELECT s.day, s.slots, COUNT(a.id) n FROM shifts s "
        "LEFT JOIN assignments a ON a.shift_id=s.id AND a.status!='sick' "
        "WHERE s.week_start=? GROUP BY s.id", (WEEK,)).fetchall()
    c.close()
    return [(x["day"], f"{x['n']}/{x['slots']}") for x in r]

def phase(n, title):
    print(f"\n===== PHASE {n}: {title} =====")

# ---- everyone ranks all 7 days randomly (the backup-plan rule)
phase(1, "all 8 employees rank all 7 days (random order each)")
for u in EMPS:
    login(u)
    days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    rng.shuffle(days)
    form = {}
    for i, d in enumerate(days, 1):
        sid = shift_id(d)
        if sid:
            form[f"rank_{sid}"] = str(i)
    client.post("/pick", data=form, follow_redirects=True)
    print(f"  {u}: ranks {'>'.join(days)}")

print("\n  schedule after preferences:")
for d, f in staffed():
    print(f"    {d}: {f}")

# ---- chaos loop: 8 random events
KINDS = ["sick", "swap", "day_off", "vacation", "switch", "unassign",
         "sick", "swap"]
events = []
for i, kind in enumerate(KINDS, 2):
    phase(i, f"random {kind}")
    u = rng.choice(EMPS)
    login(u)
    if kind == "sick":
        mine = my_shifts(u)
        if not mine:
            print(f"  {u} holds no shifts — skipped"); continue
        s = rng.choice(mine)
        client.post(f"/request/sick/{s['id']}", follow_redirects=True)
        events.append((kind, u, s["day"]))
        print(f"  {u} called out sick for {s['day']} {s['start_time']}")
        login("manager")
    elif kind == "swap":
        mine = my_shifts(u)
        if not mine:
            print(f"  {u} holds no shifts — skipped"); continue
        s = rng.choice(mine)
        client.post(f"/swap/{s['id']}", follow_redirects=True)
        events.append((kind, u, s["day"]))
        print(f"  {u} requested swap on {s['day']} {s['start_time']}")
    elif kind == "day_off":
        d = rng.choice(["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"])
        client.post("/request/day_off", data={"day": d}, follow_redirects=True)
        events.append((kind, u, d))
        print(f"  {u} requested day off: {d}")
    elif kind == "vacation":
        start = date.today() + timedelta(days=rng.randint(0, 3))
        end = start + timedelta(days=rng.randint(2, 10))
        client.post("/request/vacation",
                    data={"vac_start": start.isoformat(),
                          "vac_end": end.isoformat()}, follow_redirects=True)
        events.append((kind, u, f"{start}..{end}"))
        print(f"  {u} requested vacation {start} -> {end}")
    elif kind == "switch":
        mine = my_shifts(u)
        all_s = [x for x in my_shifts(u, "notified")]
        held = {x["id"] for x in mine}
        targets = [x for x in all_s if x["id"] not in held]
        if not mine or not targets:
            print(f"  no switch possible for {u} — trying day_off instead")
            d = rng.choice(["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"])
            client.post("/request/day_off", data={"day": d}, follow_redirects=True)
            events.append(("day_off", u, d))
            print(f"  {u} requested day off: {d}")
            continue
        a = rng.choice(mine); b = rng.choice(targets)
        client.post(f"/request/switch/{a['id']}",
                    data={"target_shift": b["id"]}, follow_redirects=True)
        events.append((kind, u, f"{a['day']} for {b['day']}"))
        print(f"  {u} asked to switch {a['day']} for {b['day']} (manager must approve)")
    elif kind == "unassign":
        login("manager")
        mine = my_shifts(u)
        if not mine:
            print(f"  {u} holds no shifts — skipped"); continue
        s = rng.choice(mine)
        client.post(f"/manager/unassign/{s['id']}/{uid(u)}", follow_redirects=True)
        events.append((kind, u, s["day"]))
        print(f"  manager pulled {u} off {s['day']}")
print()

# ---- manager approves one pending switch if any
phase(10, "manager reviews pending requests")
login("manager")
c = appmod.db()
pending = c.execute(
    "SELECT r.id, r.kind, u.username FROM requests r JOIN users u ON u.id=r.user_id "
    "WHERE r.status='pending'").fetchall()
c.close()
if pending:
    for r in pending:
        resp = client.post(f"/manager/requests/{r['id']}/approve",
                           follow_redirects=True)
        print(f"  approved {r['kind']} for {r['username']}: "
              f"{'done' if resp.status_code == 200 else 'refused'}")
else:
    print("  no pending requests (all auto-resolved)")

# ---- final sweep: anyone sick without cover -> note it
phase(11, "final audit")
c = appmod.db()
sick_rows = c.execute(
    "SELECT s.day, u.username FROM assignments a JOIN shifts s ON s.id=a.shift_id "
    "JOIN users u ON u.id=a.user_id WHERE a.status='sick' AND s.week_start=?",
    (WEEK,)).fetchall()
c.close()
print(f"  sick rows (relieved, cover already assigned): "
      f"{[(r['username'], r['day']) for r in sick_rows] or 'none'}")
print("\n  FINAL SCHEDULE:")
for d, f in staffed():
    print(f"    {d}: {f}")
c = appmod.db()
under = c.execute(
    "SELECT s.day, s.slots, COUNT(a.id) n FROM shifts s "
    "LEFT JOIN assignments a ON a.shift_id=s.id AND a.status!='sick' "
    "WHERE s.week_start=? GROUP BY s.id HAVING n < s.slots", (WEEK,)).fetchall()
c.close()
under_str = ", ".join("{} {}/{}".format(x["day"], x["n"], x["slots"]) for x in under) or "none — fully staffed"
print("\n  understaffed: " + under_str)
print("\nCHAOS RUN DONE")
