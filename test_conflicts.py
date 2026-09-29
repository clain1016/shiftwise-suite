"""Tests for the conflicts page: detection, priority sorting, manual assign."""
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
# Mon 1 slot, all 3 employees rank it #1 -> conflict with 3 claimants
mon = conn.execute(
    "INSERT INTO shifts (week_start, day, start_time, end_time, slots) VALUES (?,?,?,?,?)",
    (WEEK, "Mon", "09:00", "17:00", 1)).lastrowid
# Tue 1 slot, only jordan picks it -> no conflict
tue = conn.execute(
    "INSERT INTO shifts (week_start, day, start_time, end_time, slots) VALUES (?,?,?,?,?)",
    (WEEK, "Tue", "09:00", "17:00", 1)).lastrowid
for u in ("alex", "sam", "jordan"):
    uid = conn.execute("SELECT id FROM users WHERE username=?", (u,)).fetchone()[0]
    conn.execute("INSERT INTO picks (user_id, shift_id, rank) VALUES (?,?,1)", (uid, mon))
uid = conn.execute("SELECT id FROM users WHERE username='jordan'").fetchone()[0]
conn.execute("INSERT INTO picks (user_id, shift_id, rank) VALUES (?,?,2)", (uid, tue))
conn.commit()
conn.close()

client = appmod.app.test_client()
client.post("/login", data={"username": "manager", "password": "manager"})

# --- 1. conflicts page detects the contested shift, not the uncontested one
r = client.get("/manager/conflicts")
html = r.data.decode()
assert r.status_code == 200
assert "Conflicts" in html
assert "Mon" in html
assert "Tue" not in html.split("Conflicts —")[1].split("week of")[0]  # Tue never in a conflict card
# 3 claimants listed, sorted FT first then seniority: alex(FT 2021), sam(PT 2023), jordan(PT 2024)
assert html.index("Alex Rivera") < html.index("Sam Chen") < html.index("Jordan Diaz"), \
    "claimants must be sorted by priority lineup"
print("1. Conflict detected; claimants sorted FT-first then seniority: OK")

# --- 2. week navigation present
assert "/manager/conflicts?week=2026-09-21" in html
print("2. Week navigation on conflicts page: OK")

# --- 3. MANUAL ASSIGN REMOVED (review-only manager): the assign endpoint
# must no longer exist
uid_sam_row = appmod.db()
uid_sam = uid_sam_row.execute("SELECT id FROM users WHERE username='sam'").fetchone()[0]
uid_sam_row.close()
r = client.post("/manager/conflicts/assign",
                data={"shift_id": mon, "user_id": uid_sam}, follow_redirects=True)
assert r.status_code == 404, "conflict_assign endpoint should be gone"
print("3. Manual assign endpoint removed (review-only manager): OK")

# --- 4. assignments happen automatically WITHOUT the manager running anything:
# fresh employee pick -> assignments rebuilt instantly
conn2 = appmod.db()
conn2.execute("DELETE FROM assignments")
n_alex_before = conn2.execute(
    "SELECT COUNT(*) c FROM assignments a JOIN users u ON u.id=a.user_id "
    "WHERE u.username='alex'").fetchone()["c"]
conn2.commit()
conn2.close()
client.post("/logout")
client.post("/login", data={"username": "alex", "password": "alex"})
conn3 = appmod.db()
all_shifts = [r["id"] for r in conn3.execute("SELECT id FROM shifts").fetchall()]
conn3.close()
# re-rank all days: Mon #1, then Tue and the rest as backups (1..N)
picks_form = {f"rank_{sid}": str(i + 1) for i, sid in enumerate(
    [mon] + [x for x in all_shifts if x != mon])}
client.post("/pick", data=picks_form)
conn2 = appmod.db()
n_mon = conn2.execute("SELECT COUNT(*) c FROM assignments WHERE shift_id=?", (mon,)).fetchone()["c"]
status = conn2.execute(
    "SELECT a.status FROM assignments a JOIN users u ON u.id=a.user_id "
    "WHERE u.username='alex' AND a.shift_id=?", (mon,)).fetchone()
conn2.close()
assert n_mon == 1, f"alex saving a pick should auto-assign Mon, got {n_mon}"
assert status is not None and status["status"] == "notified", \
    f"auto assignments should be marked notified, got {status}"
print("4. Auto-assign fires on pick save (no manager action): OK (Mon auto-filled, status=notified)")

# --- 5. confirmed assignments survive a rebuild
client.post("/logout")
client.post("/login", data={"username": "sam", "password": "sam"})
client.post("/pick", data={f"rank_{mon}": "1"})   # sam PT loses contest but rebuild fires
client.post("/logout")
client.post("/login", data={"username": "manager", "password": "manager"})
conn2 = appmod.db()
sid_alex = conn2.execute(
    "SELECT a.shift_id FROM assignments a JOIN users u ON u.id=a.user_id "
    "WHERE u.username='alex' AND a.shift_id=?", (mon,)).fetchone()
conn2.execute("UPDATE assignments SET status='confirmed' WHERE user_id="
              "(SELECT id FROM users WHERE username='alex') AND shift_id=?", (mon,))
conn2.commit()
conn2.close()
# force a rebuild via unassign (the only manager mutation that still exists)
# -> confirmed row must survive
client.post(f"/manager/unassign/{mon}/9999")  # no-op unassign still triggers rebuild
conn2 = appmod.db()
survived = conn2.execute(
    "SELECT status FROM assignments a JOIN users u ON u.id=a.user_id "
    "WHERE u.username='alex' AND a.shift_id=?", (mon,)).fetchone()
conn2.close()
assert survived and survived["status"] == "confirmed", \
    f"confirmed assignment must survive rebuild, got {survived}"
print("5. Confirmed assignments survive auto-rebuild: OK")

# --- 6. employees cannot reach the conflicts page
client.post("/logout")
client.post("/login", data={"username": "alex", "password": "alex"})
r = client.get("/manager/conflicts")
assert r.status_code == 403
print("6. Conflicts page manager-only (403 for employees): OK")

print("\nALL CONFLICT TESTS PASSED")
