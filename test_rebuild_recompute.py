"""Regression: rebuilds must recompute 'notified' assignments (only
confirmed/swap_requested are fixed), so a higher-priority pick displaces
an earlier lower-priority one."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))
import app as appmod

WEEK = "2026-09-28"
DB = appmod.DB_PATH
if DB.exists():
    DB.unlink()
appmod.init_db()
conn = appmod.db()
conn.execute("DELETE FROM shifts")
conn.execute("DELETE FROM picks")
# boost hours caps so the hours cap doesn't interfere
for u in ("alex", "sam", "jordan"):
    conn.execute("UPDATE users SET weekly_hours=80 WHERE username=?", (u,))
mon = conn.execute(
    "INSERT INTO shifts (week_start, day, start_time, end_time, slots) VALUES (?,?,?,?,?)",
    (WEEK, "Mon", "09:00", "17:00", 1)).lastrowid
conn.commit()
conn.close()

client = appmod.app.test_client()

# sam (PT, newest-ish) picks first — rebuild assigns sam to Mon (notified)
client.post("/login", data={"username": "sam", "password": "sam"})
conn0 = appmod.db()
_all_shifts = [r["id"] for r in conn0.execute("SELECT id FROM shifts").fetchall()]
conn0.close()
_full_form = {f"rank_{sid}": str(i + 1) for i, sid in enumerate(
    [mon] + [x for x in _all_shifts if x != mon])}
client.post("/pick", data=_full_form)
conn = appmod.db()
who = conn.execute(
    "SELECT u.username FROM assignments a JOIN users u ON u.id=a.user_id "
    "WHERE a.shift_id=?", (mon,)).fetchall()
conn.close()
assert [w["username"] for w in who] == ["sam"], who
print("initial: sam (PT) holds the only Mon slot after his pick: OK")

# now alex (FT, senior) submits the same pick — the rebuild must give Mon
# to alex and bump sam
client.post("/logout")
client.post("/login", data={"username": "alex", "password": "alex"})
_full_form_alex = {f"rank_{sid}": str(i + 1) for i, sid in enumerate(
    [mon] + [x for x in _all_shifts if x != mon])}
client.post("/pick", data=_full_form_alex)
conn = appmod.db()
who = conn.execute(
    "SELECT u.username FROM assignments a JOIN users u ON u.id=a.user_id "
    "WHERE a.shift_id=?", (mon,)).fetchall()
conn.close()
assert [w["username"] for w in who] == ["alex"], \
    f"FT must displace PT on rebuild, got {who}"
print("after Alex (FT) picks: Mon reassigned to Alex, Sam displaced: OK")

# confirmed rows STILL survive: confirm alex, then jordan picks Mon
client.post("/logout")
client.post("/login", data={"username": "manager", "password": "manager"})
conn = appmod.db()
conn.execute(
    "UPDATE assignments SET status='confirmed' WHERE shift_id=? AND user_id="
    "(SELECT id FROM users WHERE username='alex')", (mon,))
conn.commit()
conn.close()
client.post("/logout")
client.post("/login", data={"username": "jordan", "password": "jordan"})
_full_form_jordan = {f"rank_{sid}": str(i + 1) for i, sid in enumerate(
    [mon] + [x for x in _all_shifts if x != mon])}
client.post("/pick", data=_full_form_jordan)
conn = appmod.db()
who = conn.execute(
    "SELECT u.username FROM assignments a JOIN users u ON u.id=a.user_id "
    "WHERE a.shift_id=?", (mon,)).fetchall()
conn.close()
assert [w["username"] for w in who] == ["alex"], \
    f"confirmed must be immutable even for later PT pickers, got {who}"
print("confirmed assignment survives later pickers: OK")

print("\nREBUILD-RECOMPUTE TESTS PASSED")
