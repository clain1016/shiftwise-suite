"""Randomized house-chaos simulation on the real app (own scenario DB).

Every run is different — seeded from system entropy, seed printed so a run
can be replayed:  .venv/bin/python tools/scenario_random.py <seed>

Phase 1: coin-flip the roster — each of the 10 employees randomly lands in
         front-of-house or back-of-house (kept balanced 5/5) with a random
         weekly-hours jitter, applied through the REAL /manager/roster route.
Phase 2: everyone submits a RANDOM full Mon-Sun ranking through the real
         /pick route (auto-scheduler runs). One random FOH employee also gets
         a planted cross-house pick on a BOH shift — the scheduler must
         ignore it (house separation invariant).
Phases 3+: chaos — all ten event kinds fire once each in random order:
         sick call, swap, day off, vacation, switch request, manager
         unassign, mid-week station flip, add shift, delete shift,
         weekly-cap change. Snapshot after each.
Then:    manager resolves pending requests.
Final:   audit — understaffed slots, over-cap employees, cross-house leaks
         (stale sick/swap rows after a flip are labeled, anything else is a
         LEAK), and whether the planted cross-house pick ever assigned.

The scenario DB is left in its final state for inspection.
"""
import random
import os
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

import app as appmod
import mock_seed
from shiftwise.security import use_csrf_aware_test_client

# Isolated database: never touches the working database unless
# SHIFTWISE_SCENARIO_DB_PATH points at it explicitly.
appmod.DB_PATH = Path(os.environ.get("SHIFTWISE_SCENARIO_DB_PATH",
                     os.environ.get("SHIFTWISE_DB_PATH",
                     ROOT_DIR / "tools" / "scenario_random.db")))

seed = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else \
    random.SystemRandom().randrange(2 ** 32)
rng = random.Random(seed)
print(f"\nRANDOM SEED = {seed}   (replay: python "
      f"{Path(__file__).name} {seed})\n")

# ---------- fresh mock DB
for suf in ("", "-wal", "-shm"):
    p = Path(str(appmod.DB_PATH) + suf)
    if p.exists():
        p.unlink()
appmod.init_db(seed_demo=True)
n_emp, n_days, n_picks = mock_seed.seed(appmod, force=True)
print(f"mock DB reseeded: {n_emp} employees, {n_days} days/house, "
      f"{n_picks} pre-seeded picks (wiped by phase 2)")

use_csrf_aware_test_client(appmod.app)
client = appmod.app.test_client()
WEEK = appmod.monday_of(date.today()).isoformat()
DAYS = appmod.DAYS
OK_STATUSES = ("notified", "proposed", "confirmed",
               "switch_fixed", "manager_fixed", "swap_invited")


def q(sql, args=()):
    conn = appmod.db()
    rows = conn.execute(sql, args).fetchall()
    conn.close()
    return rows


def login(u):
    client.get("/logout")
    r = client.post("/login", data={"username": u, "password": u},
                    follow_redirects=True)
    assert b"Log out" in r.data, f"login failed: {u}"


def uid_of(u):
    return q("SELECT id FROM users WHERE username=?", (u,))[0]["id"]


def employees():
    return [r["username"] for r in q(
        "SELECT username FROM users WHERE role='employee' ORDER BY id")]


def house_of(u):
    return q("SELECT station FROM users WHERE username=?", (u,))[0]["station"]


def shift_id(area, day):
    rows = q("SELECT id FROM shifts WHERE week_start=? AND day=? AND area=?",
             (WEEK, day, area))
    return rows[0]["id"] if rows else None


def holding(u, ok=None):
    rows = q(
        "SELECT s.id, s.day, s.area, s.start_time, a.status FROM assignments a "
        "JOIN shifts s ON s.id=a.shift_id WHERE a.user_id=? AND s.week_start=? "
        "ORDER BY s.id", (uid_of(u), WEEK))
    return [r for r in rows if r["status"] in ok] if ok else rows


def roster_post(changes):
    """POST /manager/roster with the FULL form — the route overwrites every
    employee's type/hired/cap/station from the form, so omitting a field
    for one employee wipes it. changes: {username: {station=?, cap=?}}"""
    form = {}
    for e in q("SELECT * FROM users WHERE role='employee' ORDER BY id"):
        ch = changes.get(e["username"], {})
        form[f"type_{e['id']}"] = e["employment_type"]
        form[f"hired_{e['id']}"] = e["hired_on"] or ""
        form[f"cap_{e['id']}"] = str(ch.get("cap", e["weekly_hours"]))
        form[f"station_{e['id']}"] = ch.get("station", e["station"])
    login("manager")
    client.post("/manager/roster", data=form, follow_redirects=True)


def snapshot(title):
    print(f"\n--- {title} ---")
    for area in ("front", "back"):
        print(f"  [{'FRONT OF HOUSE' if area == 'front' else 'BACK OF HOUSE'}]")
        for s in q("SELECT * FROM shifts WHERE week_start=? AND area=? "
                   "ORDER BY id", (WEEK, area)):
            staff = q(
                "SELECT u.name, a.status FROM assignments a "
                "JOIN users u ON u.id=a.user_id WHERE a.shift_id=? AND "
                "a.status!='sick' ORDER BY u.id", (s["id"],))
            sick = q(
                "SELECT u.name FROM assignments a JOIN users u ON u.id=a.user_id "
                "WHERE a.shift_id=? AND a.status='sick'", (s["id"],))
            flag = "OK " if len(staff) >= s["slots"] else "GAP"
            line = (f"    {s['day']:<4} {s['start_time']}-{s['end_time']} "
                    f"{len(staff)}/{s['slots']} [{flag}] "
                    + ", ".join(f"{r['name'].split()[0]}({r['status'][:4]})"
                                for r in staff))
            if sick:
                line += ("  <<sick: "
                         + ", ".join(r["name"].split()[0] for r in sick) + ">>")
            print(line)
    print("  per-employee:")
    for u in q("SELECT * FROM users WHERE role='employee' ORDER BY id"):
        rows = q(
            "SELECT s.day, s.start_time, s.end_time, a.status, s.area "
            "FROM assignments a JOIN shifts s ON s.id=a.shift_id "
            "WHERE a.user_id=? AND s.week_start=? ORDER BY s.id",
            (u["id"], WEEK))
        real = [r for r in rows if r["status"] != "sick"]
        hrs = sum(appmod.shift_hours(r["start_time"], r["end_time"])
                  for r in real)
        wd = ", ".join(f"{r['day']}/{r['area'][:1]}({r['status'][:4]})"
                       for r in rows) or "none"
        over = "  <<OVER CAP>>" if hrs > (u["weekly_hours"] or 40) else ""
        print(f"    {u['name'].split()[0]:<7} {u['station'][:1].upper()}FH "
              f"cap {u['weekly_hours']}h -> {hrs:>4.0f}h | {wd}{over}")

def phase(n, title):
    print("\n" + "=" * 64)
    print(f"PHASE {n}: {title}")
    print("=" * 64)


def coverer_of(shift_row, not_uid):
    rows = q(
        "SELECT u.username FROM assignments a JOIN users u ON u.id=a.user_id "
        "WHERE a.shift_id=? AND a.user_id!=? AND a.status='notified'",
        (shift_row["id"], not_uid))
    return rows[0]["username"] if rows else None


def run_random():
    # ================================================= PHASE 1
    phase(1, "coin-flip the roster: who is front of house, who is back?")
    emps = employees()
    houses = ["front"] * 5 + ["back"] * 5
    rng.shuffle(houses)
    changes = {}
    for u, h in zip(emps, houses):
        old = house_of(u)
        cap = q("SELECT weekly_hours FROM users WHERE username=?",
                (u,))[0]["weekly_hours"]
        new_cap = max(16, min(40, cap + rng.choice([-4, -2, 0, 0, 2, 4])))
        changes[u] = {"station": h, "cap": new_cap}
        arrow = (f"{old[:1].upper()}FH -> {h[:1].upper()}FH"
                 if old != h else f"stays {h[:1].upper()}FH")
        print(f"  {u:<7} {arrow:<15} cap {cap}h -> {new_cap}h")
    roster_post(changes)

    # ================================================= PHASE 2
    phase(2, "everyone submits a RANDOM full Mon-Sun ranking")
    for u in emps:
        h = house_of(u)
        login(u)
        days = list(DAYS)
        rng.shuffle(days)
        form = {}
        ranked = []
        for i, d in enumerate(days, 1):
            sid = shift_id(h, d)
            assert sid, f"missing {h} shift for {d}"
            form[f"rank_{sid}"] = str(i)
            ranked.append(f"{i}.{d}")
        r = client.post("/pick", data=form, follow_redirects=True)
        assert b"Preferences saved" in r.data, f"pick rejected for {u}"
        print(f"  {u:<7} ({h[:1]}FH): {' > '.join(ranked)}")

    # planted cross-house pick: a FOH employee holding a rank-1 pick on a BOH
    # shift (as if picked before a house flip) — must never be assigned
    noise_u = rng.choice([u for u in emps if house_of(u) == "front"])
    noise_shift = shift_id("back", rng.choice(DAYS))
    conn = appmod.db()
    conn.execute("INSERT OR IGNORE INTO picks (user_id, shift_id, rank) "
                 "VALUES (?,?,1)", (uid_of(noise_u), noise_shift))
    conn.commit()
    conn.close()
    appmod.run_scheduler(WEEK)
    print(f"  noise: planted a cross-house pick for {noise_u} on a BACK shift "
          f"(scheduler must ignore it)")
    snapshot("after random preferences + auto-schedule")

    # ================================================= CHAOS
    EVENTS = ["sick", "swap", "day_off", "vacation", "switch",
              "unassign", "station_flip", "add_shift", "delete_shift",
              "cap_change"]
    rng.shuffle(EVENTS)
    n = 2
    for kind in EVENTS:
        n += 1
        phase(n, f"chaos event: {kind}")
        if kind == "sick":
            cands = [u for u in emps if holding(u, OK_STATUSES)]
            if not cands:
                print("  nobody holds a shiftable shift — skipped")
                continue
            u = rng.choice(cands)
            s = rng.choice(holding(u, OK_STATUSES))
            login(u)
            client.post(f"/request/sick/{s['id']}", follow_redirects=True)
            print(f"  {u} called out sick for {s['day']} {s['start_time']} "
                  f"({s['area']} house)")
            c = coverer_of(s, uid_of(u))
            print(f"  auto-cover: {c if c else 'NONE — manager alerted'}")
        elif kind == "swap":
            cands = [u for u in emps if holding(u, OK_STATUSES)]
            if not cands:
                print("  nobody holds a shiftable shift — skipped")
                continue
            u = rng.choice(cands)
            s = rng.choice(holding(u, OK_STATUSES))
            login(u)
            client.post(f"/swap/{s['id']}", follow_redirects=True)
            row = q("SELECT status FROM assignments WHERE shift_id=? AND user_id=?",
                    (s["id"], uid_of(u)))
            if row and row[0]["status"] == "swap_requested":
                print(f"  {u} swap on {s['day']} ({s['area']}) -> nobody could "
                      f"cover: swap_requested + manager alert")
            else:
                c = coverer_of(s, uid_of(u))
                print(f"  {u} swap on {s['day']} ({s['area']}) -> covered by "
                      f"{c if c else '?'}, requester fully released")
        elif kind == "day_off":
            u = rng.choice(emps)
            d = rng.choice(DAYS)
            login(u)
            client.post("/request/day_off", data={"day": d}, follow_redirects=True)
            print(f"  {u} requested {d} off (that weekday's rows dropped, "
                  f"rebuild ran)")
        elif kind == "vacation":
            u = rng.choice(emps)
            start = date.today() + timedelta(days=rng.randint(0, 3))
            end = start + timedelta(days=rng.randint(2, 10))
            login(u)
            client.post("/request/vacation",
                        data={"vac_start": start.isoformat(),
                              "vac_end": end.isoformat()}, follow_redirects=True)
            print(f"  {u} vacation {start}..{end} (in-range shifts dropped)")
        elif kind == "switch":
            cands = [u for u in emps if holding(u, OK_STATUSES)]
            if not cands:
                print("  nobody holds a shiftable shift — skipped")
                continue
            u = rng.choice(cands)
            mine = holding(u, OK_STATUSES)
            h = house_of(u)
            held = {m["id"] for m in mine}
            targets = [r for r in q(
                "SELECT id, day, start_time FROM shifts WHERE week_start=? "
                "AND area=?", (WEEK, h)) if r["id"] not in held]
            if not mine or not targets:
                print(f"  no switch possible for {u} — skipped")
                continue
            a = rng.choice(mine)
            b = rng.choice(targets)
            login(u)
            client.post(f"/request/switch/{a['id']}",
                        data={"target_shift": b["id"]}, follow_redirects=True)
            print(f"  {u} asked to switch {a['day']} for {b['day']} "
                  f"{b['start_time']} (pending manager)")
        elif kind == "unassign":
            cands = [u for u in emps if holding(u, OK_STATUSES)]
            if not cands:
                print("  nobody holds a shiftable shift — skipped")
                continue
            u = rng.choice(cands)
            s = rng.choice(holding(u, OK_STATUSES))
            login("manager")
            client.post(f"/manager/unassign/{s['id']}/{uid_of(u)}",
                        follow_redirects=True)
            print(f"  manager pulled {u} off {s['day']} ({s['area']}) — slot "
                  f"reopens, rebuild refills")
        elif kind == "station_flip":
            u = rng.choice(emps)
            old = house_of(u)
            new = "back" if old == "front" else "front"
            roster_post({u: {"station": new}})
            print(f"  {u} FLIPPED {old} -> {new} mid-week (rebuild wipes their "
                  f"old-house rows; sick/swap rows linger as stale)")
        elif kind == "add_shift":
            login("manager")
            area = rng.choice(["front", "back"])
            d = rng.choice(DAYS)
            st, en = rng.choice([("06:00", "14:00"), ("09:00", "17:00"),
                                 ("11:00", "19:00"), ("14:00", "22:00")])
            slots = rng.randint(1, 3)
            client.post("/manager/shift/add",
                        data={"day": d, "start": st, "end": en,
                              "slots": str(slots), "note": "chaos extra",
                              "area": area}, follow_redirects=True)
            print(f"  manager added a {area} shift {d} {st}-{en} x{slots} "
                  f"({area} staff notified, rebuild may fill it)")
        elif kind == "delete_shift":
            rows = q("SELECT id, day, area FROM shifts WHERE week_start=?", (WEEK,))
            if len(rows) <= 4:
                print("  too few shifts left to delete — skipped")
                continue
            # Keep the required Monday BOH demo shift available after the chaos run.
            candidates = [r for r in rows if not (
                r["day"] == "Mon" and r["area"] == "back" and
                sum(1 for s in rows if s["day"] == "Mon" and s["area"] == "back") <= 1
            )]
            if not candidates:
                print("  only required Monday BOH shift remains — skipped")
                continue
            s = rng.choice(candidates)
            login("manager")
            client.post(f"/manager/shift/delete/{s['id']}", follow_redirects=True)
            print(f"  manager deleted the {s['area']} {s['day']} shift "
                  f"(picks+assignments gone, rebuild ran)")
        elif kind == "cap_change":
            u = rng.choice(emps)
            cur = q("SELECT weekly_hours FROM users WHERE username=?",
                    (u,))[0]["weekly_hours"]
            new_cap = max(8, min(40, cur + rng.choice([-12, -8, 8, 12])))
            roster_post({u: {"cap": new_cap}})
            print(f"  manager changed {u}'s weekly cap {cur}h -> {new_cap}h "
                  f"(rebuild re-fits under the new cap)")
        snapshot(f"after {kind}")

    # ================================================= manager resolution
    n += 1
    phase(n, "manager resolves pending requests")
    login("manager")
    pending = q("SELECT r.id, r.kind, u.username FROM requests r "
                "JOIN users u ON u.id=r.user_id WHERE r.status='pending'")
    if not pending:
        print("  none pending (all auto-resolved)")
    for r in pending:
        resp = client.post(f"/manager/requests/{r['id']}/approve",
                           follow_redirects=True)
        st = q("SELECT status FROM requests WHERE id=?", (r["id"],))[0]["status"]
        print(f"  {r['kind']} for {r['username']}: {st} (HTTP {resp.status_code})")

    # ================================================= final audit
    n += 1
    phase(n, "FINAL AUDIT")
    under = q(
        "SELECT s.day, s.area, s.slots, COUNT(a.id) n FROM shifts s "
        "LEFT JOIN assignments a ON a.shift_id=s.id AND a.status!='sick' "
        "WHERE s.week_start=? GROUP BY s.id HAVING n < s.slots", (WEEK,))
    print("  understaffed: " + (", ".join(
        f"{r['area']}/{r['day']} {r['n']}/{r['slots']}" for r in under)
        or "none — fully staffed"))
    over_cap = []
    for u in q("SELECT * FROM users WHERE role='employee'"):
        hrs = sum(appmod.shift_hours(r["start_time"], r["end_time"]) for r in q(
            "SELECT s.start_time, s.end_time FROM assignments a "
            "JOIN shifts s ON s.id=a.shift_id WHERE a.user_id=? AND "
            "s.week_start=? AND a.status!='sick'", (u["id"], WEEK)))
        if hrs > (u["weekly_hours"] or 40):
            over_cap.append(f"{u['username']} {hrs:.0f}h>{u['weekly_hours']}h")
    print("  over cap: " + (", ".join(over_cap) or "none"))
    leaks = q(
        "SELECT u.username, u.station, s.day, s.area, a.status "
        "FROM assignments a JOIN shifts s ON s.id=a.shift_id "
        "JOIN users u ON u.id=a.user_id "
        "WHERE s.week_start=? AND s.area!=u.station", (WEEK,))
    if leaks:
        for r in leaks:
            tag = ("stale by design (sick/swap row survives a house flip)"
                   if r["status"] in ("sick", "swap_requested", "swap_invited")
                   else "!! LEAK !!")
            print(f"  cross-house row: {r['username']} ({r['station']}) on "
                  f"{r['area']} {r['day']} [{r['status']}] — {tag}")
    else:
        print("  cross-house leak: none")
    noise_leak = q("SELECT id FROM assignments WHERE shift_id=? AND user_id=?",
                   (noise_shift, uid_of(noise_u)))
    print("  planted cross-house pick assigned: "
          + ("YES — BUG" if noise_leak else "no (correctly ignored)"))

    snapshot("FINAL STATE — click through on http://localhost:5001 "
             "(logins = username)")
    print("\nCHAOS RUN DONE")

if __name__ == "__main__":
    run_random()
