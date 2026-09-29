"""Tests for the per-user calendar view."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))
import app as appmod

WEEK = "2026-09-28"   # a Monday
DB = appmod.DB_PATH
if DB.exists():
    DB.unlink()
appmod.init_db()
conn = appmod.db()
conn.execute("DELETE FROM shifts")
rows = [(WEEK, d, "09:00", "17:00", 2) for d in ("Mon", "Wed", "Fri")]
conn.executemany(
    "INSERT INTO shifts (week_start, day, start_time, end_time, slots) VALUES (?,?,?,?,?)", rows)
conn.commit()

# assign Mon to alex, Wed to sam so the calendar has content
for day, user in (("Mon", "alex"), ("Wed", "sam")):
    sid = conn.execute("SELECT id FROM shifts WHERE day=?", (day,)).fetchone()[0]
    uid = conn.execute("SELECT id FROM users WHERE username=?", (user,)).fetchone()[0]
    conn.execute("INSERT INTO assignments (shift_id, user_id) VALUES (?,?)", (sid, uid))
conn.commit()
conn.close()

client = appmod.app.test_client()

# --- 1. employee sees their own week: Mon = mine, Wed/Fri empty for them
client.post("/login", data={"username": "alex", "password": "alex"})
r = client.get("/calendar")
html = r.data.decode()
assert r.status_code == 200
assert "Alex Rivera" in html
assert "cal-mine" in html
mine_count = html.count('class="cal-shift cal-mine"')
assert mine_count == 1, f"Alex should see exactly 1 of their shifts, got {mine_count}"
assert "scheduled" in html
assert "Sep 28" in html and "Oct 04" in html, "day labels should render"
print("1. Employee calendar renders own week with correct assignment: OK")

# --- 2. prev/next week navigation links present and work
assert "/calendar?week=2026-09-21" in html, "prev-week link missing"
assert "/calendar?week=2026-10-05" in html, "next-week link missing"
r2 = client.get("/calendar?week=2026-10-05")
assert r2.status_code == 200
assert "Oct 05" in r2.data.decode() and 'class="cal-shift cal-mine"' not in r2.data.decode()
print("2. Week navigation works, next week is empty: OK")

# --- 3. invalid week param falls back to current week, not a crash
r3 = client.get("/calendar?week=garbage")
assert r3.status_code == 200 and "Alex Rivera" in r3.data.decode()
print("3. Invalid week param handled gracefully: OK")

# --- 4. employee does NOT get the manager employee-picker
assert "— view employee —" not in html
print("4. No employee picker for employees: OK")

# --- 5. manager can view another employee's calendar
client.post("/logout")
client.post("/login", data={"username": "manager", "password": "manager"})
r5 = client.get("/calendar?user_id=3")   # sam
html5 = r5.data.decode()
assert r5.status_code == 200
assert "Sam Chen" in html5 and "— view employee —" in html5
assert html5.count('class="cal-shift cal-mine"') == 1
assert "Alex Rivera" in html5  # manager sees who else is staffed (Fri shift empty though)
print("5. Manager views Sam's calendar via picker: OK")

# --- 6. no assignments -> all days empty but page still renders
client.post("/logout")
client.post("/login", data={"username": "jordan", "password": "jordan"})
r6 = client.get("/calendar")
assert r6.status_code == 200 and "Jordan Diaz" in r6.data.decode()
assert 'class="cal-shift cal-mine"' not in r6.data.decode()
print("6. Unassigned employee gets a clean empty calendar: OK")

print("\nALL CALENDAR TESTS PASSED")
