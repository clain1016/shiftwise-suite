"""Mock smoke test (FOH/BOH split): each house's staff pick their own
house's Mon shift #1 -> verify FT priority lineup per house."""
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

week = appmod.monday_of(__import__("datetime").date.today()).isoformat()
conn = appmod.db()
ids = {(r["area"], r["day"]): r["id"]
       for r in conn.execute("SELECT id, day, area FROM shifts WHERE week_start=?", (week,))}
conn.close()
mon_front, mon_back = ids[("front", "Mon")], ids[("back", "Mon")]

# FOH staff pick the FOH Mon shift #1; BOH staff the BOH Mon shift #1.
# The full-week pick rule requires ALL 7 days of their own house ranked
# (unique 1..7) — Mon gets rank 1, the rest follow in day order.
def full_form(client_user, mon_id):
    conn = appmod.db()
    house = conn.execute(
        "SELECT station FROM users WHERE username=?", (client_user,)).fetchone()["station"]
    ids = [r["id"] for r in conn.execute(
        "SELECT id FROM shifts WHERE week_start=? AND area=? ORDER BY id",
        (week, house))]
    conn.close()
    ids.remove(mon_id)
    form = {f"rank_{mon_id}": "1"}
    for i, sid in enumerate(ids):
        form[f"rank_{sid}"] = str(i + 2)
    return form

for u in ("maria", "devon", "priya", "alex", "sam"):
    c = appmod.app.test_client()
    c.post("/login", data={"username": u, "password": u})
    c.post("/pick", data=full_form(u, mon_front))
for u in ("jordan", "taylor", "riley", "casey", "morgan"):
    c = appmod.app.test_client()
    c.post("/login", data={"username": u, "password": u})
    c.post("/pick", data=full_form(u, mon_back))

appmod.run_scheduler(week)

conn = appmod.db()
rows = conn.execute(
    "SELECT u.username, s.area, s.day FROM assignments a "
    "JOIN users u ON u.id=a.user_id JOIN shifts s ON s.id=a.shift_id "
    "WHERE s.day='Mon'").fetchall()
cross = conn.execute(
    "SELECT u.username FROM assignments a JOIN users u ON u.id=a.user_id "
    "JOIN shifts s ON s.id=a.shift_id WHERE s.area!=u.station").fetchall()
conn.close()

on_front = sorted(r["username"] for r in rows if r["area"] == "front")
on_back = sorted(r["username"] for r in rows if r["area"] == "back")
assert not cross, f"cross-house leak: {[r['username'] for r in cross]}"
# FOH Mon (3 slots): FT most-senior first -> maria, devon, priya
assert on_front == sorted(["maria", "devon", "priya"]), f"FOH Mon got {on_front}"
# BOH Mon (2 slots): morgan (FT 2021) then casey (PT 2023) — jordan/taylor/
# riley also pick Mon but lineup order decides; assert the 2 most senior
assert len(on_back) == 2 and set(on_back) == {"morgan", "casey"}, f"BOH Mon got {on_back}"
print("FOH Mon (3 slots):", on_front)
print("BOH Mon (2 slots):", on_back)
print("no cross-house assignments")
print("MOCK SMOKE TEST PASSED")
