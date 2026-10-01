"""Shift scheduling engine and automatic round-based assignment."""
from collections import defaultdict
from datetime import date, timedelta

from shiftwise.db import db
from shiftwise.domain.constants import DAYS, MIN_DAYS_OFF
from shiftwise.domain.rules import (
    assignment_block_reason,
    priority_key,
    shift_hours,
    unavailable_uids,
)
from shiftwise.notify import notify
from shiftwise.scheduler.coverage import coverage_plan


def _vacated_by_pending_vacation(conn, shift):
    """True when a pending vacation is holding a slot on this shift open.

    Only these gaps may be backfilled by someone who would otherwise exceed
    their weekly hours or days-off limit: the vacation created the gap, and
    the manager already reviews that request knowing the cover may go over.
    Gaps from anything else (sick calls, unfilled picks) stay inside the caps.
    """
    shift_date = (date.fromisoformat(shift["week_start"]) +
                  timedelta(days=DAYS.index(shift["day"]))).isoformat()
    return conn.execute(
        "SELECT 1 FROM requests r JOIN users u ON u.id=r.user_id "
        "WHERE r.kind='vacation' AND r.status='approved' "
        "AND r.vacation_start<=? AND r.vacation_end>=? "
        "AND u.role='employee' AND u.station=? LIMIT 1",
        (shift_date, shift_date, shift["area"])).fetchone() is not None


class _SchedulerState:
    """Per-run mutable state shared by the run_scheduler phases.

    Created fresh inside run_scheduler() for each call and passed explicitly
    to the phase helpers below — never stored at module level or shared
    across runs.
    """

    def __init__(self, conn, week_start, actor):
        self.conn = conn
        self.week_start = week_start
        self.actor = actor
        # Filled in by the phases, in call order:
        self.notification_start = 0
        self.shifts = []
        self.shift_ids = []
        self.ph = ""
        self.shift_by_id = {}
        self.vacation_gap = {}
        self.unavailable = {}
        self.previous = defaultdict(set)
        self.previous_staffed = defaultdict(int)
        self.picks = []
        self.users = {}
        self.prior_conflicts = set()
        self.capacity = {}
        self.assigned = defaultdict(int)          # shift_id -> count
        self.user_hours = defaultdict(float)      # user_id -> assigned hours this week
        self.user_assignments = defaultdict(list)  # user_id -> [(shift_id, marker)]
        # marker: True = fixed confirmed,
        #         False = new, 'sick' = out sick
        self.shift_hours_map = {}
        self.area_of = {}
        self.user_prefs = defaultdict(lambda: defaultdict(list))


def _begin_week(state):
    """Begin the scheduling run: open the write transaction, snapshot the
    notification watermark, and load the week's shifts.

    Returns False when the week has no shifts (nothing to schedule).

    Reads: state.conn, state.week_start.
    Mutates: state.notification_start, state.shifts, state.shift_ids.
    """
    conn = state.conn
    conn.execute("BEGIN IMMEDIATE")
    state.notification_start = conn.execute(
        "SELECT COALESCE(MAX(id), 0) FROM notifications").fetchone()[0]
    state.shifts = conn.execute(
        "SELECT * FROM shifts WHERE week_start=? ORDER BY id", (state.week_start,)
    ).fetchall()
    state.shift_ids = [s["id"] for s in state.shifts]
    return bool(state.shift_ids)


def _collect_inputs(state):
    """Load the data the assignment rounds need: vacation-held gaps,
    per-shift unavailable sets, the previous assignment snapshot, picks,
    users, and prior conflict notifications. Also evicts holders whose
    pending swap keeps them listed as unavailable for their own shift.

    Reads: state.conn, state.shifts, state.shift_ids, state.notification_start.
    Mutates: state.vacation_gap, state.ph, state.shift_by_id, state.unavailable,
        state.previous, state.previous_staffed, state.picks, state.users,
        state.prior_conflicts; DB (deletes swap-evicted assignment rows).
    """
    conn = state.conn
    shifts = state.shifts
    shift_ids = state.shift_ids
    # Slots a pending vacation holds open are the only ones that may be
    # backfilled over the hours/days limits — see the helper above.
    state.vacation_gap = {s["id"]: _vacated_by_pending_vacation(conn, s)
                          for s in shifts}
    state.ph = ",".join("?" * len(shift_ids))
    ph = state.ph
    state.shift_by_id = {s["id"]: s for s in shifts}
    state.unavailable = {s["id"]: unavailable_uids(conn, s) for s in shifts}
    for r in conn.execute(
            f"SELECT shift_id, user_id FROM assignments WHERE shift_id IN ({ph}) "
            "AND status NOT IN ('sick','swap_requested')", shift_ids):
        state.previous[r["user_id"]].add(r["shift_id"])
        state.previous_staffed[r["shift_id"]] += 1
    # 'swap_invited' rows are exempt too: the pending swap keeps the
    # holder on the shift, so the requester's own swap must not evict them.
    for r in conn.execute(
            f"SELECT shift_id, user_id FROM assignments WHERE shift_id IN ({ph}) "
            "AND status NOT IN ('sick','swap_requested','swap_invited')",
            shift_ids).fetchall():
        if r["user_id"] in state.unavailable[r["shift_id"]]:
            conn.execute("DELETE FROM assignments WHERE shift_id=? AND user_id=?",
                         (r["shift_id"], r["user_id"]))
    state.picks = conn.execute(
        f"SELECT * FROM picks WHERE shift_id IN ({ph})", shift_ids).fetchall()
    state.users = {u["id"]: u for u in conn.execute(
        "SELECT * FROM users WHERE role='employee'")}
    state.prior_conflicts = {r["user_id"] for r in conn.execute(
        "SELECT DISTINCT user_id FROM notifications WHERE id<=? AND kind='conflict'",
        (state.notification_start,))}


def _prepare_rebuild(state):
    """Wipe the week's auto assignments and seed the in-memory tracking from
    the rows that stay fixed, then build the preference lists used by the
    assignment rounds.

    Reads: state.conn, state.shifts, state.shift_ids, state.ph, state.users,
        state.picks.
    Mutates: DB (deletes 'proposed'/'notified' assignment rows);
        state.capacity, state.shift_hours_map, state.shift_by_id,
        state.area_of, state.assigned, state.user_hours,
        state.user_assignments, state.picks (filtered), state.user_prefs.
    """
    conn = state.conn
    shift_ids = state.shift_ids
    ph = state.ph
    shifts = state.shifts
    users = state.users
    # confirmed / manager-approved-switch / manager-set
    # assignments are fixed: keep the rows and seed the in-memory state
    # with them before re-running the lineup. ('notified' rows are NOT
    # fixed — they're regular auto-assignments and must be recomputed so
    # a higher-priority pick can displace an earlier lower-priority one.)
    # 'swap_invited' stays fixed: a pending coworker swap keeps the holder
    # on the shift (staffed, counted toward hours) until the invite resolves.
    fixed = conn.execute(
        f"SELECT shift_id, user_id FROM assignments WHERE shift_id IN ({ph}) "
        "AND status IN ('confirmed', 'manager_fixed', 'swap_invited')",
        shift_ids).fetchall()
    # Manager-approved switches and arranged cover stay fixed on rebuild.
    fixed += conn.execute(
        f"SELECT shift_id, user_id FROM assignments WHERE shift_id IN ({ph}) "
        "AND status IN ('switch_fixed','coverage_fixed')", shift_ids).fetchall()
    conn.execute(
        f"DELETE FROM assignments WHERE shift_id IN ({ph}) AND "
        "status IN ('proposed', 'notified')", shift_ids)

    state.capacity = {s["id"]: s["slots"] for s in shifts}
    state.shift_hours_map = {s["id"]: shift_hours(s["start_time"], s["end_time"])
                             for s in shifts}
    state.shift_by_id = {s["id"]: s for s in shifts}
    state.area_of = {s["id"]: s["area"] for s in shifts}

    for r in fixed:
        if r["user_id"] in users:
            state.assigned[r["shift_id"]] += 1
            state.user_hours[r["user_id"]] += state.shift_hours_map[r["shift_id"]]
            state.user_assignments[r["user_id"]].append((r["shift_id"], True))

    # Sick and unresolved swap rows prevent the same employee from being
    # reclaimed, while leaving the slot open for coverage.
    sick_rows = conn.execute(
        f"SELECT shift_id, user_id FROM assignments WHERE shift_id IN ({ph}) "
        "AND status IN ('sick','swap_requested')", shift_ids).fetchall()
    for r in sick_rows:
        if r["user_id"] in users:
            state.user_assignments[r["user_id"]].append((r["shift_id"], "absent"))

    # defensive: ignore picks belonging to non-employees (e.g. stale
    # rows created before the manager role check existed)
    area_of = state.area_of
    state.picks = [p for p in state.picks
                   if p["user_id"] in users and p["shift_id"] in area_of]

    # user_id -> {rank: [shift_ids]} — tied ranks allowed, resolved in
    # submission order within the round
    user_prefs = state.user_prefs
    for p in state.picks:
        user_prefs[p["user_id"]][p["rank"]].append(p["shift_id"])


def _run_assignment_rounds(state):
    """Round-based lineup: in round N every employee claims their rank-N
    pick; contested slots go to the highest-priority employee in line.

    Reads: state.conn, state.actor, state.users, state.area_of,
        state.shift_by_id, state.user_prefs, state.assigned, state.user_hours,
        state.user_assignments, state.capacity, state.shift_hours_map.
    Mutates: DB (inserts assignments, writes conflict notifications);
        state.assigned, state.user_hours, state.user_assignments.
    """
    conn = state.conn
    actor = state.actor
    users = state.users
    area_of = state.area_of
    shift_by_id = state.shift_by_id
    user_prefs = state.user_prefs
    assigned = state.assigned
    user_hours = state.user_hours
    user_assignments = state.user_assignments
    capacity = state.capacity
    shift_hours_map = state.shift_hours_map

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


def _backfill_coverage(state):
    """Coverage backfill: after the lineup, any shift that still has empty
    slots (typical cause: a sick row vacated a taken slot) is auto-covered
    by the best available employee — those who picked it in lineup order
    first, then least-loaded with room. With no coverer, the manager gets a
    'no cover available' notification.

    No-op unless the run actor is "system".

    Reads: state.actor, state.conn, state.week_start, state.shifts,
        state.capacity, state.assigned, state.user_hours,
        state.user_assignments, state.shift_hours_map, state.vacation_gap,
        state.previous_staffed.
    Mutates: DB (inserts/updates assignments, writes notifications);
        state.assigned, state.user_hours, state.user_assignments.
    """
    if state.actor != "system":
        return
    conn = state.conn
    week_start = state.week_start
    shifts = state.shifts
    capacity = state.capacity
    assigned = state.assigned
    user_hours = state.user_hours
    user_assignments = state.user_assignments
    shift_hours_map = state.shift_hours_map
    vacation_gap = state.vacation_gap
    previous_staffed = state.previous_staffed
    # Capacity math: assigned[] counts real workers only (sick rows
    # were never counted), so a shift with 2 slots + 1 sick row + 0
    # covers shows 1 open slot here and gets exactly one coverer.
    for s in shifts:
        open_n = capacity[s["id"]] - assigned[s["id"]]
        if open_n <= 0:
            continue
        for _ in range(open_n):
            cover_uid = coverage_plan(
                conn, week_start, s["id"], None,
                allow_over_limits=vacation_gap[s["id"]])
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
                   "open slot)." + (" This vacation option may exceed "
                   "your weekly hours or workday limit; check with your "
                   "manager before confirming." if vacation_gap[s["id"]] else ""))


def _resolve_swaps(state):
    """Once a replacement fills an unresolved swap's slot, clear the
    original holder and close the request.

    Reads: state.conn, state.capacity, state.assigned, state.shift_by_id,
        state.shifts.
    Mutates: DB (deletes filled swap_requested assignments, closes swap
        requests, writes notifications).
    """
    conn = state.conn
    capacity = state.capacity
    assigned = state.assigned
    shift_by_id = state.shift_by_id
    for s in state.shifts:
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


def _notify_schedule_changes(state):
    """Wrap-up notifications — only for employees whose schedule changed
    (an auto-rebuild shouldn't spam unchanged schedules).

    Reads: state.conn, state.ph, state.shift_ids, state.users, state.previous,
        state.shift_by_id, state.user_prefs.
    Mutates: DB (writes assignment/conflict notifications).
    """
    conn = state.conn
    ph = state.ph
    shift_ids = state.shift_ids
    users = state.users
    previous = state.previous
    shift_by_id = state.shift_by_id
    for uid, prefs in state.user_prefs.items():
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


def _finalize_week(state):
    """Mark fresh proposed assignments as 'notified' so the next rebuild
    can tell whether anything changed for this employee, drop notifications
    for unchanged schedules, and commit the week.

    Reads: state.conn, state.ph, state.shift_ids, state.users, state.previous,
        state.prior_conflicts, state.notification_start.
    Mutates: DB (updates assignments to 'notified', deletes notifications
        for unchanged schedules, commits).
    """
    conn = state.conn
    ph = state.ph
    shift_ids = state.shift_ids
    # mark fresh proposed assignments as 'notified' so the next rebuild
    # can tell whether anything changed for this employee
    conn.execute(f"""
            UPDATE assignments SET status='notified'
            WHERE shift_id IN ({ph}) AND status='proposed'""", shift_ids)
    for uid in state.users:
        current = {r["shift_id"] for r in conn.execute(
            f"SELECT shift_id FROM assignments WHERE user_id=? "
            f"AND shift_id IN ({ph}) "
            "AND status NOT IN ('sick','swap_requested')",
            (uid, *shift_ids))}
        if current == state.previous[uid] and (current or uid in state.prior_conflicts):
            conn.execute("DELETE FROM notifications WHERE user_id=? AND id>?",
                         (uid, state.notification_start))
    conn.commit()


def run_scheduler(week_start, actor="system"):
    """Assign picks to shifts across the week.

    Round-based, with a priority lineup: employees are ordered by
    employment type (full-time before part-time), then by seniority
    (earliest hire date first). In round N every employee claims their
    rank-N pick; contested slots go to the highest-priority employee in
    line. Employees are never scheduled past their weekly hours cap
    (full-time default 40h, part-time as set on the roster), with one
    exception: a slot a pending vacation is holding open may be offered to
    an over-cap coverer so the manager can review it. If a slot is
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
        state = _SchedulerState(conn, week_start, actor)
        if not _begin_week(state):
            return 0
        _collect_inputs(state)
        _prepare_rebuild(state)
        _run_assignment_rounds(state)
        _backfill_coverage(state)
        _resolve_swaps(state)
        _notify_schedule_changes(state)
        _finalize_week(state)
        return len(state.picks)
    finally:
        conn.close()
