"""One-shot conflict gauntlet: every feature + conflict in a single
in-process run (zero network/LLM cost). Reseed -> 10x full-week picks ->
conflict events incl. edge cases -> manager review -> audit."""
import sys
import os
from pathlib import Path
from datetime import date, timedelta

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

import app as appmod
from mock_seed import seed
from shiftwise.security import use_csrf_aware_test_client

if len(sys.argv) > 1 and sys.argv[1].endswith(".db"):
    db_target = Path(sys.argv[1]).resolve()
else:
    db_target = Path(os.environ.get("SHIFTWISE_SCENARIO_DB_PATH",
                     os.environ.get("SHIFTWISE_DB_PATH",
                     ROOT_DIR / "tools" / "gauntlet.db")))

appmod.DB_PATH = db_target
appmod.init_db(seed_demo=True)
seed(appmod, force=True)
appmod.init_db(seed_demo=True)

use_csrf_aware_test_client(appmod.app)
client = appmod.app.test_client()
WEEK = appmod.monday_of(date.today()).isoformat()
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
EMPS = None

def login(u, password=None):
    client.get("/logout")
    response = client.post("/login", data={"username": u, "password": password or u},
                           follow_redirects=True)
    assert b"Log out" in response.data, u

def uid(u):
    c = appmod.db()
    r = c.execute("SELECT id FROM users WHERE username=?", (u,)).fetchone()[0]
    c.close()
    return r

def all_emps():
    c = appmod.db()
    r = [x["username"] for x in c.execute("SELECT username FROM users WHERE role='employee'")]
    c.close()
    return r

def house_of(u):
    c = appmod.db()
    r = c.execute("SELECT station FROM users WHERE username=?", (u,)).fetchone()
    c.close()
    return r["station"] if r else "front"

def sid_of(day, area=None):
    c = appmod.db()
    if area:
        r = c.execute("SELECT id FROM shifts WHERE week_start=? AND day=? AND area=?",
                      (WEEK, day, area)).fetchone()
    else:
        r = c.execute("SELECT id FROM shifts WHERE week_start=? AND day=?",
                      (WEEK, day)).fetchone()
    c.close()
    return r["id"] if r else None

def shift_ids(area):
    c = appmod.db()
    rows = c.execute(
        "SELECT id FROM shifts WHERE week_start=? AND area=? ORDER BY id",
        (WEEK, area),
    ).fetchall()
    c.close()
    return [row["id"] for row in rows]

def my(u, status="notified"):
    c = appmod.db()
    r = c.execute("SELECT s.id, s.day, a.status FROM assignments a "
                  "JOIN shifts s ON s.id=a.shift_id WHERE a.user_id=? AND "
                  "a.status=? AND s.week_start=?", (uid(u), status, WEEK)).fetchall()
    c.close()
    return r

def schedule():
    c = appmod.db()
    rows = c.execute(
        "SELECT s.day, s.slots, COUNT(a.id) n, GROUP_CONCAT(u.name, ', ') names "
        "FROM shifts s LEFT JOIN assignments a ON a.shift_id=s.id "
        "AND a.status NOT IN ('sick','swap_requested') "
        "LEFT JOIN users u ON u.id=a.user_id "
        "WHERE s.week_start=? GROUP BY s.id", (WEEK,)).fetchall()
    c.close()
    return [(r["day"], r["n"], r["slots"], r["names"] or "-") for r in rows]

def ph(n, t):
    print(f"\n===== PHASE {n}: {t} =====")

def run_gauntlet():
    global EMPS
    # ---------- 1. full-week picks, everyone ----------
    EMPS = all_emps()
    ph(1, "all " + str(len(EMPS)) + " employees rank all 7 days (required)")
    for u in EMPS:
        login(u)
        order = DAYS[::-1] if u in ("riley", "priya") else DAYS
        form = {f"rank_day_{day}": str(i + 1) for i, day in enumerate(order)}
        r = client.post("/pick", data=form, follow_redirects=True)
        assert b"Preferences saved" in r.data, u
    print("  all saved; schedule:")
    for d, n, s, names in schedule():
        print(f"    {d}: {n}/{s}  [{names}]")

    # ---------- 2. partial pick rejection ----------
    ph(2, "partial day-rank form REJECTED (only 6 days ranked)")
    login("maria")
    form = {f"rank_day_{day}": str(i + 1) for i, day in enumerate(DAYS[:6])}
    r = client.post("/pick", data=form, follow_redirects=True)
    print("  rejected:", b"Rank all seven days" in r.data)

    # ---------- 3. sick call, two on same day ----------
    ph(3, "sick calls: maria + taylor both out on Monday")
    for u in ("maria", "taylor"):
        login(u)
        m = [x for x in my(u) if x["day"] == "Mon"]
        if m:
            client.post(f"/request/sick/{m[0]['id']}", follow_redirects=True)
            print(f"  {u} sick on Mon -> covered: {my(u, 'notified') and 'yes'}")
    print("  Mon now:", [f"{n}/{s}" for d, n, s, _ in schedule() if d == "Mon"])

    # ---------- 4. swap ----------
    ph(4, "swap: devon gives up a shift (auto-covered)")
    login("devon")
    d_mine = my("devon")
    if d_mine:
        tgt = d_mine[0]
        before = f"{tgt['day']}"
        client.post(f"/swap/{tgt['id']}", follow_redirects=True)
        still = appmod.db().execute(
            "SELECT 1 FROM assignments WHERE shift_id=? AND user_id=?",
            (tgt["id"], uid("devon"))).fetchone()
        print(f"  devon released {before}; row removed: {still is None}")

    # ---------- 5. day off ----------
    ph(5, "day off: priya drops Wednesday")
    login("priya")
    client.post("/request/day_off", data={"day": "Wed"}, follow_redirects=True)
    print("  Wed now:", [f"{n}/{s}" for d, n, s, _ in schedule() if d == "Wed"])

    # ---------- 6. vacation ----------
    ph(6, "vacation: alex out Tue->next Mon")
    login("alex")
    today = date.today()
    vs = today + timedelta(days=(1 if today.weekday() < 1 else 0))
    ve = vs + timedelta(days=6)
    r = client.post("/request/vacation",
                    data={"vac_start": vs.isoformat(), "vac_end": ve.isoformat()},
                    follow_redirects=True)
    print(f"  alex vacation {vs}..{ve}: saved={b'Vacation requested' in r.data}")
    print("  alex now holds:", [x["day"] for x in my("alex")] or "nothing (all in range)")

    # ---------- 7. vacation, past-dated (rejected) ----------
    ph(7, "vacation with past start -> rejected")
    login("jordan")
    r = client.post("/request/vacation",
                    data={"vac_start": "2026-09-01", "vac_end": "2026-09-05"},
                    follow_redirects=True)
    print("  rejected:", b"past" in r.data)

    # ---------- 8. switch + manager deny ----------
    ph(8, "switch request into a FULL shift -> manager approval auto-denies")
    login("sam")
    s_mine = my("sam")
    full = appmod.db().execute(
        "SELECT s.id, s.day FROM shifts s WHERE s.week_start=? AND (SELECT "
        "COUNT(*) FROM assignments a WHERE a.shift_id=s.id AND a.status NOT IN ('sick','swap_requested')) "
        ">= s.slots ORDER BY s.id LIMIT 1", (WEEK,)).fetchone()
    mine_ids = {x["id"] for x in s_mine}
    if full and full["id"] not in mine_ids:
        client.post(f"/request/switch/{s_mine[0]['id']}",
                    data={"target_shift": full["id"]}, follow_redirects=True)
        login("manager")
        c = appmod.db()
        req = c.execute("SELECT id FROM requests WHERE kind='switch' AND "
                        "status='approved' ORDER BY id DESC LIMIT 1").fetchone()
        if req:
            client.post(f"/manager/requests/{req['id']}/approve", follow_redirects=True)
            row = c.execute("SELECT status FROM requests WHERE id=?", (req["id"],)).fetchone()
            print(f"  switch into {full['day']} (full): result={row['status']}")
        else:
            print("  switch route blocked it up front")
        c.close()
    else:
        print("  no full shift to test against")

    # ---------- 9. manager unassign + instant backfill ----------
    ph(9, "manager unassigns jordan -> slot backfills instantly")
    login("manager")
    j_mine = my("jordan")
    if j_mine:
        tgt = j_mine[0]
        client.post(f"/manager/unassign/{tgt['id']}/{uid('jordan')}", follow_redirects=True)
        c = appmod.db()
        who = c.execute("SELECT u.name FROM assignments a JOIN users u ON u.id=a.user_id "
                        "WHERE a.shift_id=? AND a.user_id!=? AND a.status='notified' "
                        "ORDER BY a.id DESC LIMIT 1", (tgt["id"], uid("jordan"))).fetchone()
        c.close()
        print(f"  jordan pulled off {tgt['day']}; backfilled by {who['name'] if who else 'NOBODY'}")

    # ---------- 10. roster change ----------
    ph(10, "roster change: add new part-timer (avery), they pick all 21 shifts")
    login("manager")
    client.post("/manager/roster/add", data={
        "username": "avery", "password": "avery-private-passphrase", "name": "Avery Quinn",
        "weekly_hours": "20", "employment_type": "part_time",
        "hired_on": date.today().isoformat()}, follow_redirects=True)
    c = appmod.db()
    row = c.execute("SELECT id FROM users WHERE username='avery'").fetchone()
    assert row, "new employee was not added"
    c.close()
    login("avery", "avery-private-passphrase")
    avery_ids = shift_ids(house_of("avery"))
    form = {f"rank_{sid}": str(i + 1) for i, sid in enumerate(avery_ids)}
    r = client.post("/pick", data=form, follow_redirects=True)
    print("  avery picked all 21 shifts:", b"Preferences saved" in r.data)
    c = appmod.db()
    avery_holds = c.execute("SELECT COUNT(*) c FROM assignments a JOIN users u ON "
                            "u.id=a.user_id WHERE u.username='avery'").fetchone()["c"]
    c.close()
    print(f"  avery auto-assigned {avery_holds} shifts")

    # ---------- 11. final audit ----------
    ph(11, "final audit")
    for d, n, s, names in schedule():
        print(f"    {d}: {n}/{s}  [{names}]")
    c = appmod.db()
    sick = c.execute("SELECT u.username, s.day FROM assignments a JOIN users u ON "
                     "u.id=a.user_id JOIN shifts s ON s.id=a.shift_id WHERE "
                     "a.status='sick' AND s.week_start=?", (WEEK,)).fetchall()
    over = c.execute(
        "SELECT u.username, SUM(julianday(s2.end_time)-julianday(s2.start_time))*24 h "
        "FROM assignments a JOIN users u ON u.id=a.user_id JOIN shifts s2 ON "
        "s2.id=a.shift_id WHERE a.status NOT IN ('sick','swap_requested') AND s2.week_start=? GROUP BY a.user_id",
        (WEEK,)).fetchall()
    reqs = c.execute("SELECT kind, status, COUNT(*) n FROM requests GROUP BY kind, status").fetchall()
    c.close()
    print("  sick rows:", [(r["username"], r["day"]) for r in sick] or "none")
    print("  weekly hours:", {r["username"]: round(r["h"]) for r in over})
    print("  request log:", [(r["kind"], r["status"], r["n"]) for r in reqs])
    print("\nGAUNTLET DONE")

if __name__ == "__main__":
    run_gauntlet()
