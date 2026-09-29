"""Regression test for the KeyError: manager picks crashed the auto-rebuild."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))
import app as appmod

DB = appmod.DB_PATH
if DB.exists():
    DB.unlink()
appmod.init_db()

client = appmod.app.test_client()

# --- 1. manager submitting picks is rejected with a friendly flash, no 500
client.post("/login", data={"username": "manager", "password": "manager"})
r = client.post("/pick", data={"rank_1": "1", "rank_2": "2"}, follow_redirects=True)
assert r.status_code == 200
assert b"Picks are for employees" in r.data
conn = appmod.db()
n = conn.execute("SELECT COUNT(*) c FROM picks WHERE user_id=1").fetchone()["c"]
conn.close()
assert n == 0, "manager must not create pick rows"
print("1. Manager pick attempt blocked with explanation, no rows: OK")

# --- 2. defensive: stale manager picks in the DB don't crash the scheduler
conn = appmod.db()
conn.execute("INSERT INTO picks (user_id, shift_id, rank) VALUES (1, 1, 1)")
conn.commit()
conn.close()
try:
    n = appmod.run_scheduler(appmod.monday_of(appmod.date.today()).isoformat())
    print(f"2. Scheduler tolerates stale non-employee picks (processed {n}): OK")
except KeyError as e:
    raise AssertionError(f"KeyError {e} — scheduler must skip non-employee picks")

# --- 3. manager dashboard shows the view-only note, no pick form
r = client.get("/")
assert b"Save my picks" not in r.data
assert b"Managers view the schedule here" in r.data
print("3. Manager dashboard is view-only (no pick form): OK")

# --- 4. employee picks still work fine
client.post("/logout")
client.post("/login", data={"username": "alex", "password": "alex"})
conn = appmod.db()
all_shifts = [r["id"] for r in conn.execute("SELECT id FROM shifts").fetchall()]
conn.close()
form = {f"rank_{sid}": str(i + 1) for i, sid in enumerate(all_shifts)}
r = client.post("/pick", data=form, follow_redirects=True)
assert b"Preferences saved" in r.data
conn = appmod.db()
status = conn.execute(
    "SELECT a.status FROM assignments a JOIN users u ON u.id=a.user_id "
    "WHERE u.username='alex' AND a.shift_id=1").fetchone()
conn.close()
assert status and status["status"] in ("proposed", "notified"), status
print("4. Employee pick flow unaffected: OK")

print("\nALL KEYERROR-REGRESSION TESTS PASSED")
