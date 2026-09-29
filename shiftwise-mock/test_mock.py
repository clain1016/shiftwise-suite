"""Mock smoke test: 8 employees, all pick Mon #1 -> verify FT priority lineup."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))
import app as appmod
import mock_seed

DB = appmod.DB_PATH
if DB.exists():
    DB.unlink()
appmod.init_db()
mock_seed.seed(appmod)

conn = appmod.db()
mon_id = conn.execute("SELECT id FROM shifts WHERE day='Mon'").fetchone()["id"]
conn.close()

# every employee ranks Mon #1 (rank now repeatable across days)
for u in ("maria", "devon", "priya", "alex", "sam", "jordan", "taylor", "riley"):
    r = appmod.app.test_client()
    r.post("/login", data={"username": u, "password": u})
    r.post("/pick", data={f"rank_{mon_id}": "1"})

appmod.run_scheduler(appmod.monday_of(__import__("datetime").date.today()).isoformat())

conn = appmod.db()
rows = conn.execute(
    "SELECT u.username, s.day FROM assignments a "
    "JOIN users u ON u.id=a.user_id JOIN shifts s ON s.id=a.shift_id").fetchall()
conn.close()
on_mon = [r["username"] for r in rows if r["day"] == "Mon"]
expected = ["maria", "devon", "priya"]   # 3 slots: FT, most senior first
assert on_mon == expected, f"Mon should be {expected}, got {on_mon}"
print("Mock smoke test: Mon (3 slots) went to FT top-3 by seniority:", on_mon)
print("MOCK SMOKE TEST PASSED")
