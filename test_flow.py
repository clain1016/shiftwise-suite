"""End-to-end test of the scheduler flow using Flask's test client."""
import re
from collections import defaultdict
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))
import app as appmod

# fresh DB for the test
DB = appmod.DB_PATH
if DB.exists():
    DB.unlink()

appmod.init_db()
client = appmod.app.test_client()

def login(user, pw):
    r = client.post("/login", data={"username": user, "password": pw}, follow_redirects=True)
    assert b"Log out" in r.data, f"login failed for {user}"
    return r

# --- 1. manager login, look at week
login("manager", "manager")
r = client.get("/manager")
assert r.status_code == 200
print("manager dashboard OK,", r.data.decode().count("<tr>"), "table rows")

# --- 2. three employees each pick Mon rank1; Sat has 3 slots
picks = {
    "alex":   [("Mon", 1), ("Wed", 2), ("Thu", 3), ("Fri", 4), ("Sat", 5), ("Sun", 6), ("Tue", 7)],
    "sam":    [("Mon", 1), ("Fri", 2), ("Sat", 3), ("Tue", 4), ("Wed", 5), ("Sun", 6), ("Thu", 7)],
    "jordan": [("Mon", 1), ("Tue", 2), ("Sat", 3), ("Wed", 4), ("Thu", 5), ("Fri", 6), ("Sun", 7)],
}
for user, plist in picks.items():
    client.post("/logout")
    login(user, user)
    r = client.get("/")
    html = r.data.decode()
    # find shift ids by day
    import re
    day_to_id = {}
    for m in re.finditer(r'<td>(\w{3})[ <].*?</td>\s*<td>(\d\d:\d\d)&ndash;(\d\d:\d\d)</td>', html, re.S):
        pass
    # simpler: query db
    conn = appmod.db()
    rows = conn.execute("SELECT id, day FROM shifts").fetchall()
    conn.close()
    form = {}
    for day, rank in plist:
        sid = next(x["id"] for x in rows if x["day"] == day)
        form[f"rank_{sid}"] = str(rank)
    r = client.post("/pick", data=form, follow_redirects=True)
    assert b"Preferences saved" in r.data
print("all employees submitted picks")

# --- 3. manager runs scheduler
client.post("/logout")
login("manager", "manager")
r = client.post("/manager/run", follow_redirects=True)
assert b"Auto-assign" in r.data or b"Scheduler ran" in r.data
print(r.data.decode().split('class="flash">')[1].split("</div>")[0])

# --- 4. verify assignment outcome in DB
conn = appmod.db()
rows = conn.execute(
    "SELECT s.day, u.name, a.status FROM assignments a "
    "JOIN shifts s ON s.id=a.shift_id JOIN users u ON u.id=a.user_id ORDER BY s.id, u.name").fetchall()
conn.close()
print("\nAssignments:")
for r_ in rows:
    print(f"  {r_['day']}: {r_['name']} ({r_['status']})")

# Mon has 2 slots, 3 people wanted it rank-1 -> the odd one out lands on backups
mon = [r_ for r_ in rows if r_["day"] == "Mon"]
assert len(mon) == 2, f"Mon should have exactly 2 assigned, got {len(mon)}"
per_emp = defaultdict(int)
for r_ in rows:
    per_emp[r_["name"]] += 1
for name, n in per_emp.items():
    assert n >= 2, f"{name} only got {n} shifts"

# the loser of Mon should have gotten a conflict notification with an alternative
conn = appmod.db()
conf = conn.execute("SELECT u.name, n.message FROM notifications n JOIN users u ON u.id=n.user_id "
                    "WHERE kind='conflict'").fetchall()
conn.close()
print("\nConflict notifications:")
for c in conf:
    print(f"  {c['name']}: {c['message']}")
assert conf, "expected at least one conflict/alternative notification"

# --- 5. employee sees assignment; no confirm step (auto-scheduled)
client.post("/logout")
login("alex", "alex")
r = client.get("/")
assert b"scheduled" in r.data, "employee dashboard should show 'scheduled' tag"
# the confirm route must be gone
r2 = client.post("/confirm/1", follow_redirects=True)
assert r2.status_code == 404 or b"Shift confirmed" not in r2.data, \
    "confirm route should no longer exist"
sid_row = [x for x in rows if x["name"] == "Alex Rivera"][0]
print(f"\nemployee dashboard OK; Alex sees {sid_row['day']} assignment, auto-scheduled")

sid = appmod.db().execute(
    "SELECT s.id FROM assignments a JOIN shifts s ON s.id=a.shift_id "
    "JOIN users u ON u.id=a.user_id WHERE u.name='Alex Rivera' LIMIT 1").fetchone()["id"]

# --- 6. swap request pings the manager
r = client.post(f"/swap/{sid}", follow_redirects=True)
assert b"Swap requested" in r.data
conn = appmod.db()
n = conn.execute("SELECT COUNT(*) c FROM notifications WHERE kind='swap_request'").fetchone()["c"]
conn.close()
assert n >= 1
print("swap-request flow OK")

print("\nALL TESTS PASSED")
