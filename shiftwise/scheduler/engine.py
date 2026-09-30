"""Shift scheduling engine and automatic round-based assignment."""
from collections import defaultdict

from shiftwise.db import db
from shiftwise.domain.constants import MIN_DAYS_OFF
from shiftwise.domain.rules import (
    assignment_block_reason,
    priority_key,
    shift_hours,
    unavailable_uids,
)
from shiftwise.notify import notify
from shiftwise.scheduler.coverage import coverage_plan


def run_scheduler(week_start, actor="system"):
    """Assign picks to shifts across the week.

    Round-based, with a priority lineup: employees are ordered by
    employment type (full-time before part-time), then by seniority
    (earliest hire date first). In round N every employee claims their
    rank-N pick; contested slots go to the highest-priority employee in
    line. Employees are never scheduled past their weekly hours cap
    (full-time default 40h, part-time as set on the roster). If a slot is
    full, the employee falls through to their next pick — the best
    available alternative on their ranked list — and gets a notification
    explaining the fallback. When their whole list is exhausted they get a
    'nothing had room' notification.

    Rebuild semantics: every call wipes the week's auto assignments and
    recomputes them. Confirmed, approved switch, and arranged coverage
    assignments stay fixed. Sick and pending swap rows leave capacity open.
    """
    conn = db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        notification_start = conn.execute(
            "SELECT COALESCE(MAX(id), 0) FROM notifications").fetchone()[0]
        shifts = conn.execute(
            "SELECT * FROM shifts WHERE week_start=? ORDER BY id", (week_start,)
        ).fetchall()
        shift_ids = [s["id"] for s in shifts]
        if not shift_ids:
            return 0
        ph = ",".join("?" * len(shift_ids))
        shift_by_id = {s["id"]: s for s in shifts}
        unavailable = {s["id"]: unavailable_uids(conn, s) for s in shifts}
        previous = defaultdict(set)
        previous_staffed = defaultdict(int)
        for r in conn.execute(
                f"SELECT shift_id, user_id FROM assignments WHERE shift_id IN ({ph}) "
                "AND status NOT IN ('sick','swap_requested')", shift_ids):
            previous[r["user_id"]].add(r["shift_id"])
            previous_staffed[r["shift_id"]] += 1
        for r in conn.execute(
                f"SELECT shift_id, user_id FROM assignments WHERE shift_id IN ({ph}) "
                "AND status NOT IN ('sick','swap_requested')", shift_ids).fetchall():
            if r["user_id"] in unavailable[r["shift_id"]]:
                conn.execute("DELETE FROM assignments WHERE shift_id=? AND user_id=?",
                             (r["shift_id"], r["user_id"]))
        picks = conn.execute(
            f"SELECT * FROM picks WHERE shift_id IN ({ph})", shift_ids).fetchall()
        users = {u["id"]: u for u in conn.execute(
            "SELECT * FROM users WHERE role='employee'")}
        prior_conflicts = {r["user_id"] for r in conn.execute(
            "SELECT DISTINCT user_id FROM notifications WHERE id<=? AND kind='conflict'",
            (notification_start,))}

        # confirmed / manager-approved-switch / manager-set
        # assignments are fixed: keep the rows and seed the in-memory state
        # with them before re-running the lineup. ('notified' rows are NOT
        # fixed — they're regular auto-assignments and must be recomputed so
        # a higher-priority pick can displace an earlier lower-priority one.)
        fixed = conn.execute(
            f"SELECT shift_id, user_id FROM assignments WHERE shift_id IN ({ph}) "
            "AND status IN ('confirmed', 'manager_fixed')",
            shift_ids).fetchall()
        # Manager-approved switches and arranged cover stay fixed on rebuild.
        fixed += conn.execute(
            f"SELECT shift_id, user_id FROM assignments WHERE shift_id IN ({ph}) "
            "AND status IN ('switch_fixed','coverage_fixed')", shift_ids).fetchall()
        conn.execute(
            f"DELETE FROM assignments WHERE shift_id IN ({ph}) AND "
            "status IN ('proposed', 'notified')", shift_ids)

        capacity = {s["id"]: s["slots"] for s in shifts}
        assigned = defaultdict(int)          # shift_id -> count
        user_hours = defaultdict(float)      # user_id -> assigned hours this week
        user_assignments = defaultdict(list) # user_id -> [(shift_id, marker)]
                                             # marker: True = fixed confirmed,
                                             # False = new, 'sick' = out sick
        shift_hours_map = {s["id"]: shift_hours(s["start_time"], s["end_time"])
                           for s in shifts}
        shift_by_id = {s["id"]: s for s in shifts}
        area_of = {s["id"]: s["area"] for s in shifts}

        for r in fixed:
            if r["user_id"] in users:
                assigned[r["shift_id"]] += 1
                user_hours[r["user_id"]] += shift_hours_map[r["shift_id"]]
                user_assignments[r["user_id"]].append((r["shift_id"], True))

        # Sick and unresolved swap rows prevent the same employee from being
        # reclaimed, while leaving the slot open for coverage.
        sick_rows = conn.execute(
            f"SELECT shift_id, user_id FROM assignments WHERE shift_id IN ({ph}) "
            "AND status IN ('sick','swap_requested')", shift_ids).fetchall()
        for r in sick_rows:
            if r["user_id"] in users:
                user_assignments[r["user_id"]].append((r["shift_id"], "absent"))

        # defensive: ignore picks belonging to non-employees (e.g. stale
        # rows created before the manager role check existed)
        picks = [p for p in picks
                 if p["user_id"] in users and p["shift_id"] in area_of]

        # user_id -> {rank: [shift_ids]} — tied ranks allowed, resolved in
        # submission order within the round
        user_prefs = defaultdict(lambda: defaultdict(list))
        for p in picks:
            user_prefs[p["user_id"]][p["rank"]].append(p["shift_id"])

        # Priority lineup: front-of-house before back-of-house, then
        # full-time before part-time, then earliest hire date first.
        # Employees missing a hire date rank last within their group
        # (treated as newest). Each employee is only scheduled into
        # shifts of their own area: a FOH employee's picks on BOH
        # shifts (and vice versa) are ignored — the two houses run as
        # two separate schedules.
        priority = lambda uid: priority_key(users[uid])
        my_area = {uid: users[uid]["station"] for uid in users}
        eligible = lambda uid, sid: (sid in area_of and
                                     my_area[uid] == area_of[sid])

        max_rank = max((max(prefs) for prefs in user_prefs.values()), default=0)
        for rnd in range(1, max_rank + 1):
            # who wants a slot this round, highest priority first
            # (contested slots go to the most senior / full-time employee)
            claimants = []
            for uid, prefs in user_prefs.items():
                for sid in prefs.get(rnd, []):
                    if sid in [a[0] for a in user_assignments[uid]]:
                        continue
                    if not eligible(uid, sid):
                        continue  # FOH pick on a BOH shift (or vice versa)
                    claimants.append((priority(uid), uid, sid))
            claimants.sort()
            for _, uid, sid in claimants:
                if sid in [a[0] for a in user_assignments[uid]]:
                    continue
                reason = assignment_block_reason(conn, uid, shift_by_id[sid])
                if reason == "hours":
                    if actor == "system":
                        notify(conn, uid, "conflict",
                               f"Passing on {shift_by_id[sid]['day']} — it would put "
                               f"you over your {users[uid]['weekly_hours']}h weekly cap.")
                    continue
                if reason == "days":
                    if actor == "system":
                        notify(conn, uid, "conflict",
                               f"Passing on {shift_by_id[sid]['day']} — you need "
                               f"at least {MIN_DAYS_OFF} days off this week.")
                    continue
                if reason:
                    continue
                if assigned[sid] < capacity[sid]:
                    assigned[sid] += 1
                    user_hours[uid] += shift_hours_map[sid]
                    user_assignments[uid].append((sid, False))
                    conn.execute(
                        "INSERT OR IGNORE INTO assignments (shift_id, user_id) VALUES (?,?)",
                        (sid, uid))
                elif actor == "system":
                    shift = shift_by_id[sid]
                    notify(conn, uid, "conflict",
                           f"Your #{rnd} pick ({shift['day']} "
                           f"{shift['start_time']}-{shift['end_time']}) was full — "
                           "trying your next choice.")

        # coverage backfill: after the lineup, any shift that still has
        # empty slots (typical cause: a sick row vacated a taken slot)
        # is auto-covered by the best available employee — those who
        # picked it in lineup order first, then least-loaded with room.
        # Capacity math: assigned[] counts real workers only (sick rows
        # were never counted), so a shift with 2 slots + 1 sick row + 0
        # covers shows 1 open slot here and gets exactly one coverer.
        if actor == "system":
            for s in shifts:
                open_n = capacity[s["id"]] - assigned[s["id"]]
                if open_n <= 0:
                    continue
                for _ in range(open_n):
                    cover_uid = coverage_plan(conn, week_start, s["id"], None)
                    if cover_uid is None:
                        mgr = conn.execute(
                            "SELECT id FROM users WHERE role='manager'").fetchone()
                        if mgr:
                            prior_alert = conn.execute(
                                "SELECT 1 FROM notifications WHERE user_id=? "
                                "AND shift_id=? AND kind='conflict' AND "
                                "message LIKE 'No cover available%' LIMIT 1",
                                (mgr["id"], s["id"])).fetchone()
                            if previous_staffed[s["id"]] >= capacity[s["id"]] \
                                    or not prior_alert:
                                notify(conn, mgr["id"], "conflict",
                                       f"No cover available for {s['day']} "
                                       f"{s['start_time']}-{s['end_time']} — "
                                       "needs manual coverage.", s["id"])
                        break
                    # record + notify (hours/days tracking stays DB-accurate
                    # via the next build; insert directly to assignments)
                    conn.execute(
                        "INSERT OR IGNORE INTO assignments (shift_id, user_id) "
                        "VALUES (?,?)", (s["id"], cover_uid))
                    conn.execute(
                        "UPDATE assignments SET status='notified' "
                        "WHERE shift_id=? AND user_id=? AND status='proposed'",
                        (s["id"], cover_uid))
                    assigned[s["id"]] += 1
                    user_hours[cover_uid] += shift_hours_map[s["id"]]
                    user_assignments[cover_uid].append((s["id"], False))
                    notify(conn, cover_uid, "assignment",
                           f"Coverage: you're now on {s['day']} "
                           f"{s['start_time']}-{s['end_time']} (covering an "
                           "open slot).")

        # Once a replacement fills an unresolved swap's slot, clear the
        # original holder and close the request.
        for s in shifts:
            if assigned[s["id"]] < capacity[s["id"]]:
                continue
            for row in conn.execute(
                    "SELECT user_id FROM assignments WHERE shift_id=? "
                    "AND status='swap_requested'", (s["id"],)).fetchall():
                conn.execute("DELETE FROM assignments WHERE shift_id=? AND user_id=?",
                             (s["id"], row["user_id"]))
                conn.execute("UPDATE requests SET status='approved_ok' WHERE "
                             "kind='swap' AND status='approved' AND shift_id=? "
                             "AND user_id=?", (s["id"], row["user_id"]))
                notify(conn, row["user_id"], "assignment",
                       f"Swap covered for {s['day']} {s['start_time']}-{s['end_time']}.")

        # wrap-up notifications — only for employees whose schedule changed
        # (an auto-rebuild shouldn't spam unchanged schedules)
        for uid, prefs in user_prefs.items():
            u = users.get(uid)
            if not u:
                continue
            got = {r["shift_id"] for r in conn.execute(
                f"SELECT shift_id FROM assignments WHERE user_id=? AND shift_id IN ({ph}) "
                "AND status NOT IN ('sick','swap_requested')", (uid, *shift_ids))}
            if got == previous[uid]:
                continue
            if got:
                days = ", ".join(shift_by_id[s]["day"] for s in sorted(got))
                notify(conn, uid, "assignment",
                       f"Schedule posted: you're on {days} this week.")
            else:
                notify(conn, uid, "conflict",
                       "None of your picks had room this week — check open shifts "
                       "or message the manager.")

        # mark fresh proposed assignments as 'notified' so the next rebuild
        # can tell whether anything changed for this employee
        conn.execute(f"""
            UPDATE assignments SET status='notified'
            WHERE shift_id IN ({ph}) AND status='proposed'""", shift_ids)
        for uid in users:
            current = {r["shift_id"] for r in conn.execute(
                f"SELECT shift_id FROM assignments WHERE user_id=? "
                f"AND shift_id IN ({ph}) "
                "AND status NOT IN ('sick','swap_requested')",
                (uid, *shift_ids))}
            if current == previous[uid] and (current or uid in prior_conflicts):
                conn.execute("DELETE FROM notifications WHERE user_id=? AND id>?",
                             (uid, notification_start))
        conn.commit()
        return len(picks)
    finally:
        conn.close()
