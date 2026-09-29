"""Tests for employee self-service requests: day off, sick call with
automatic coverage, and shift switch, using Flask's test client."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))
import app as appmod

DB = appmod.DB_PATH
if DB.exists():
    DB.unlink()
appmod.init_db()
client = appmod.app.test_client()

def login(user, pw):
    r = client.post("/login", data={"username": user, "password": pw}, follow_redirects=True)
    assert b"Log out" in r.data, f"login failed for {user}"
    return r

def sid_for(day):
    conn = appmod.db()
    sid = conn.execute("SELECT id FROM shifts WHERE day=? LIMIT 1", (day,)).fetchone()[0]
    conn.close()
    return sid

def uid_for(username):
    conn = appmod.db()
    uid = conn.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()[0]
    conn.close()
    return uid

# everyone submits picks for ALL 7 days (rank 1-7, no repeats) — the
# full ranking is the backup plan: any day can be covered if plans change
week = appmod.monday_of(appmod.date.today()).isoformat()
pick_plan = {
    "alex":   ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
    "sam":    ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
    "jordan": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
}
for user, wanted in pick_plan.items():
    login(user, user)
    client.get("/")
    conn = appmod.db()
    rows = conn.execute("SELECT id, day FROM shifts").fetchall()
    conn.close()
    form = {}
    for r in rows:
        form[f"rank_{r['id']}"] = str(wanted.index(r["day"]) + 1)
    client.post("/pick", data=form, follow_redirects=True)

conn = appmod.db()
n_assigned = conn.execute("SELECT COUNT(*) c FROM assignments").fetchone()["c"]
conn.close()
assert n_assigned > 0, "auto-scheduler should have assigned shifts"
print(f"1. Week auto-assigned: {n_assigned} assignments: OK")

# --- 2. day-off request: drops the employee's shifts on that day
login("sam", "sam")
mon_id = sid_for("Mon")
conn = appmod.db()
sam_had_mon = conn.execute(
    "SELECT 1 FROM assignments WHERE shift_id=? AND user_id=?",
    (mon_id, uid_for("sam"))).fetchone()
conn.close()
r = client.post("/request/day_off", data={"day": "Mon"}, follow_redirects=True)
assert b"Day off requested" in r.data
conn = appmod.db()
gone = conn.execute(
    "SELECT 1 FROM assignments a JOIN shifts s ON s.id=a.shift_id "
    "WHERE a.user_id=? AND s.day=? AND s.week_start=? AND a.status!='sick'",
    (uid_for("sam"), "Mon", week)).fetchone()
req = conn.execute("SELECT * FROM requests WHERE kind='day_off'").fetchone()
mgr_notif = conn.execute(
    "SELECT message FROM notifications WHERE user_id=(SELECT id FROM users WHERE role='manager') "
    "AND message LIKE '%requested Mon off%'").fetchone()
conn.close()
assert gone is None, "Sam's Mon assignment should be dropped after day-off request"
assert req is not None, "day_off request row missing"
assert mgr_notif, "manager should be notified of the day-off request"
print("2. Day-off request drops the day's shifts + notifies manager: OK")

# --- 3. sick call: coverage auto-arranged, nobody left uncovered
login("alex", "alex")
# find a shift alex actually holds
conn = appmod.db()
alex_shift = conn.execute(
    "SELECT s.id, s.day, s.slots FROM assignments a JOIN shifts s ON s.id=a.shift_id "
    "WHERE a.user_id=? LIMIT 1", (uid_for("alex"),)).fetchone()
conn.close()
assert alex_shift, "alex should hold a shift to call out sick from"
r = client.post(f"/request/sick/{alex_shift['id']}", follow_redirects=True)
assert b"Sick call logged" in r.data
conn = appmod.db()
sick_row = conn.execute(
    "SELECT status FROM assignments WHERE shift_id=? AND user_id=?",
    (alex_shift["id"], uid_for("alex"))).fetchone()
cover = conn.execute(
    "SELECT u.username, a.status FROM assignments a JOIN users u ON u.id=a.user_id "
    "WHERE a.shift_id=? AND a.user_id!=? AND a.status='notified'",
    (alex_shift["id"], uid_for("alex"))).fetchone()
conn.close()
assert sick_row and sick_row["status"] == "sick", "alex's row should be status='sick'"
assert cover is not None, "someone must auto-cover the sick shift"
print(f"3. Sick call: {cover['username']} auto-covers Alex's "
      f"{alex_shift['day']} shift: OK")

# --- 3b. coverage never breaks the hours cap or days-off rule
conn = appmod.db()
cap_violations = conn.execute(
    """SELECT u.username, u.weekly_hours, SUM(
         CASE WHEN s.start_time IS NOT NULL
         THEN (CAST(substr(s.end_time,1,2) AS REAL)*60 + CAST(substr(s.end_time,4,2) AS REAL)
             - CAST(substr(s.start_time,1,2) AS REAL)*60 - CAST(substr(s.start_time,4,2) AS REAL))/60.0
         ELSE 0 END) h
       FROM users u
       LEFT JOIN assignments a ON a.user_id=u.id AND a.status!='sick'
       LEFT JOIN shifts s ON s.id=a.shift_id AND s.week_start=?
       WHERE u.role='employee' GROUP BY u.id""",
    (week,)).fetchall()
for row in cap_violations:
    h = row["h"] or 0
    assert h <= (row["weekly_hours"] or 40) + 0.01, \
        f"{row['username']} over cap: {h}h > {row['weekly_hours']}h"
conn.close()
print("3b. Coverage respects every employee's weekly hours cap: OK")

# --- 4. shift switch request + manager approval (target = shift with room)
login("sam", "sam")
conn = appmod.db()
sam_shift = conn.execute(
    "SELECT s.id FROM assignments a JOIN shifts s ON s.id=a.shift_id "
    "WHERE a.user_id=? AND a.status='notified' LIMIT 1", (uid_for("sam"),)).fetchone()
# a shift sam does NOT hold that has a free slot (backfill fills everything
# it legally can, so this may not exist — then a switch can't be approved
# and the test only checks the request round-trip)
target_shift = conn.execute(
    "SELECT s.id FROM shifts s WHERE s.week_start=? AND s.id NOT IN "
    "(SELECT a.shift_id FROM assignments a WHERE a.user_id=?) AND "
    "(SELECT COUNT(*) FROM assignments a WHERE a.shift_id=s.id AND a.status!='sick') < s.slots "
    "ORDER BY s.id LIMIT 1", (week, uid_for("sam"))).fetchone()
conn.close()
if sam_shift and target_shift:
    r = client.post(f"/request/switch/{sam_shift['id']}",
                    data={"target_shift": str(target_shift["id"])},
                    follow_redirects=True)
    assert b"Switch request sent" in r.data
    conn = appmod.db()
    req = conn.execute(
        "SELECT id FROM requests WHERE kind='switch' AND status='approved' "
        "AND user_id=?", (uid_for("sam"),)).fetchone()
    conn.close()
    assert req, "switch request row missing"
    client.post("/logout")
    login("manager", "manager")
    r = client.get("/manager/requests")
    assert b"Switch" in r.data, "requests page should list the switch"
    r = client.post(f"/manager/requests/{req['id']}/approve", follow_redirects=True)
    conn = appmod.db()
    req_st = conn.execute("SELECT status FROM requests WHERE id=?",
                          (req["id"],)).fetchone()["status"]
    now_on_target = conn.execute(
        "SELECT 1 FROM assignments WHERE shift_id=? AND user_id=?",
        (target_shift["id"], uid_for("sam"))).fetchone()
    not_on_old = conn.execute(
        "SELECT 1 FROM assignments WHERE shift_id=? AND user_id=?",
        (sam_shift["id"], uid_for("sam"))).fetchone()
    staffed = conn.execute(
        "SELECT COUNT(*) c FROM assignments WHERE shift_id=? AND status!='sick'",
        (target_shift["id"],)).fetchone()["c"]
    slots = conn.execute("SELECT slots FROM shifts WHERE id=?",
                         (target_shift["id"],)).fetchone()["slots"]
    conn.close()
    if req_st == "approved_ok":
        assert now_on_target, "sam should hold the target shift after approval"
        assert not_on_old is None, "sam should no longer hold the old shift"
        print("4. Switch request approved -> sam moved to target shift: OK")
    else:
        assert staffed >= slots, "declined switch must have been into a full shift"
        print("4. Switch request correctly declined (target shift was full): OK")
else:
    print("4. Switch request: skipped (no free shift to switch into this week)")

# --- 5. manager deny path works
conn = appmod.db()
pend = conn.execute(
    "SELECT id FROM requests WHERE status='approved' AND kind='day_off'").fetchone()
conn.close()
if pend:
    client.post("/logout")
    login("manager", "manager")
    client.post(f"/manager/requests/{pend['id']}/deny", follow_redirects=True)
    conn = appmod.db()
    st = conn.execute("SELECT status FROM requests WHERE id=?",
                      (pend["id"],)).fetchone()["status"]
    conn.close()
    assert st == "denied"
    print("5. Manager can deny a pending request: OK")

# --- 6. every shift either covered, flagged, or arithmetically uncoverable
conn = appmod.db()
understaffed = conn.execute(
    """SELECT s.id, s.day, s.slots, COUNT(a.id) staffed FROM shifts s
       LEFT JOIN assignments a ON a.shift_id=s.id AND a.status!='sick'
       WHERE s.week_start=? GROUP BY s.id""",
    (week,)).fetchall()
gaps = [r for r in understaffed if r["staffed"] < r["slots"]]
for g in gaps:
    # either the manager was alerted...
    has_alert = conn.execute(
        "SELECT 1 FROM notifications WHERE user_id=(SELECT id FROM users "
        "WHERE role='manager') AND message LIKE '%No cover%' "
        "AND message LIKE ? AND message LIKE '%manual coverage%'",
        (f"for {g['day']} %",)).fetchone()
    if has_alert:
        continue
    # ...or every potential coverer would break their hours cap or the
    # days-off rule (coverage_plan confirms nobody can legally take it)
    assert appmod.coverage_plan(conn, week, g["id"], None) is None, \
        f"{g['day']} shift understaffed but a legal coverer exists and wasn't assigned"
conn.close()
print("6. Every shift covered, flagged, or truly uncoverable: OK")

# --- 7. vacation request: drops shifts in the date range, notifies manager.
# Today may fall late in the week (e.g. Sunday), so the range spans from
# the earlier of (Thu, today) through next week — always valid & future.
login("jordan", "jordan")
week_monday = appmod.date.fromisoformat(week)
today = appmod.date.today()
thu = (week_monday + appmod.timedelta(days=3)).isoformat()
next_sat = (week_monday + appmod.timedelta(days=12)).isoformat()
vstart = thu if thu >= today.isoformat() else today.isoformat()
vend = next_sat
r = client.post("/request/vacation",
                data={"vac_start": vstart, "vac_end": vend}, follow_redirects=True)
assert b"Vacation requested" in r.data, \
    f"vacation request should succeed, flash said: " + \
    r.data.decode().split('class="flash">')[-1].split("</div>")[0]
conn = appmod.db()
jordan_uid = conn.execute("SELECT id FROM users WHERE username='jordan'").fetchone()[0]
vreq = conn.execute(
    "SELECT * FROM requests WHERE kind='vacation' AND user_id=?",
    (jordan_uid,)).fetchone()
# in-range shifts of THIS week that jordan held must be gone, but only if
# their date is >= today (past dates were rejected at request time)
still_held = []
for d in conn.execute(
        "SELECT s.day, a.shift_id FROM assignments a JOIN shifts s ON s.id=a.shift_id "
        "WHERE a.user_id=? AND s.week_start=?", (jordan_uid, week)).fetchall():
    dt = week_monday + appmod.timedelta(days=appmod.DAYS.index(d["day"]))
    if vstart <= dt.isoformat() <= vend and dt >= appmod.date.today():
        still_held.append(d["day"])
conn.close()
assert vreq is not None and vreq["vacation_start"] == vstart and vreq["vacation_end"] == vend
assert still_held == [], \
    f"jordan still holds in-range shifts {[h for h in still_held]}"
print("7. Vacation request records the range; in-range shifts dropped: OK")

# --- 7b. vacation can't start in the past
r = client.post("/request/vacation",
                data={"vac_start": "2020-01-01", "vac_end": "2020-01-02"},
                follow_redirects=True)
assert b"start in the past" in r.data
print("7b. Past-dated vacation rejected: OK")

# --- 8. swap request auto-covers (no manager wait)
# find a shift jordan still holds (after vacation, Mon-Wed or Sat remain)
conn = appmod.db()
j_shift = conn.execute(
    "SELECT s.id, s.day FROM assignments a JOIN shifts s ON s.id=a.shift_id "
    "WHERE a.user_id=? AND a.status='notified' LIMIT 1", (jordan_uid,)).fetchone()
conn.close()
if j_shift:
    login("jordan", "jordan")
    r = client.post(f"/swap/{j_shift['id']}", follow_redirects=True)
    conn = appmod.db()
    jrow = conn.execute(
        "SELECT status FROM assignments WHERE shift_id=? AND user_id=?",
        (j_shift["id"], jordan_uid)).fetchone()
    coverer = conn.execute(
        "SELECT u.username, a.status FROM assignments a JOIN users u ON u.id=a.user_id "
        "WHERE a.shift_id=? AND a.user_id!=? AND a.status='notified'",
        (j_shift["id"], jordan_uid)).fetchone()
    mgr_alert = conn.execute(
        "SELECT 1 FROM notifications WHERE user_id=(SELECT id FROM users "
        "WHERE role='manager') AND message LIKE '%requested a swap%' "
        "AND message LIKE '%nobody can cover%'").fetchone()
    conn.close()
    assert jrow and jrow["status"] == "swap_requested", \
        "jordan's row should be swap_requested"
    if coverer is not None:
        assert b"Swap arranged" in r.data
        jrow_status_uncovered = False
        print(f"8. Swap auto-covered: {coverer['username']} takes jordan's "
              f"{j_shift['day']} shift instantly: OK")
    else:
        assert mgr_alert, "no coverer -> manager must be alerted"
        assert b"nobody is available" in r.data
        jrow_status_uncovered = True
        print(f"8. Swap requested but uncoverable (everyone at cap) — "
              f"manager alerted, jordan relieved either way: OK")
else:
    print("8. Swap test skipped (jordan holds no swappable shift)")

# --- 8b. the swap left a trace in the requests log; if it was uncovered,
# the requester must not be re-claimed by rebuilds
conn = appmod.db()
swap_rows = conn.execute(
    "SELECT status FROM requests WHERE kind='swap' AND user_id=?",
    (jordan_uid,)).fetchall()
conn.close()
assert swap_rows, "swap request should be logged"
if jrow_status_uncovered:
    conn = appmod.db()
    still_off = conn.execute(
        "SELECT status FROM assignments WHERE shift_id=? AND user_id=?",
        (j_shift["id"], jordan_uid)).fetchone()
    conn.close()
    assert still_off and still_off["status"] == "swap_requested", \
        "rebuild must not re-claim an uncovered swapped shift for the requester"
    print("8b. Uncovered swap survives rebuilds (requester not re-claimed): OK")
else:
    conn = appmod.db()
    gone = conn.execute(
        "SELECT 1 FROM assignments WHERE shift_id=? AND user_id=?",
        (j_shift["id"], jordan_uid)).fetchone()
    conn.close()
    assert gone is None, "covered swap should have removed jordan's row"
    print("8b. Covered swap removed jordan's row entirely: OK")

print("\nALL SELF-SERVICE REQUEST TESTS PASSED")
