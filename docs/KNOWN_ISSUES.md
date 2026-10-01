# Known Issues — Chaos & Load Test Findings

> Status: active · Created: 2026-09-30 · Source: liveweek gauntlets (seeds 42, 9999) + scenario_random

This document catalogues real application issues surfaced by the chaos and load
testing harnesses. Each finding includes reproduction steps, root-cause
analysis, and pointers to the code that needs fixing. **Work items one per
PR**, branched from current `master`.

## How to use this backlog

1. Pick the highest-severity unfixed item.
2. Read the linked liveweek issue files in `tools/liveweek-artifacts/` for
   full evidence (tracebacks, event logs, actor timelines).
3. Write a regression test in `tests/` that triggers the bug deterministically.
4. Fix the code, run `./tools/run_all.sh`, and open a PR.
5. Mark the item as **FIXED** with the PR number.

---

## CRITICAL

### C1 — `database is locked` causes HTTP 500 under concurrent load

**Severity:** CRITICAL · **Reproducible:** Yes (every liveweek run)  
**Seeds:** 42 (7 occurrences), 9999 (5 occurrences)

**Symptom.** Under concurrent traffic (15+ employees + 2 managers), multiple
routes return HTTP 500 with `sqlite3.OperationalError: database is locked`.
The 15-second `busy_timeout` is exceeded, causing unhandled exceptions.

**Affected routes (observed tracebacks):**
- `POST /pick` → `employee.py:217` (`DELETE FROM picks`) and `:236` (`run_scheduler`)
- `GET /` → `employee.py:151` (`UPDATE notifications SET read=1`)
- `POST /login` → `auth.py:97` (`DELETE FROM login_attempts`)
- `POST /request/switch/<id>` → `employee.py` (dashboard redirect after write)
- `POST /manager/unassign/<sid>/<uid>` → `conflicts.py:108` (`DELETE FROM assignments`)
- `POST /pick` → `engine.py:57` (`BEGIN IMMEDIATE` in `run_scheduler`)

**Root cause.** Every route opens a fresh SQLite connection
(`shiftwise/db.py:db()`) and many routes trigger `run_scheduler()` which uses
`BEGIN IMMEDIATE` — a serializing lock. Under concurrent traffic, multiple
`BEGIN IMMEDIATE` transactions queue up, and those that cannot acquire the lock
within `busy_timeout` (15s) raise `OperationalError`. No route has a retry or
graceful-degradation path.

**Fix approach.**
1. Wrap write-path calls in a retry loop (1–3 retries with short backoff) for
   `OperationalError("database is locked")`.
2. Move the `UPDATE notifications SET read=1` in the dashboard GET to a
   deferred / non-blocking path (it's a write inside a read-heavy page load).
3. Consider connection pooling or WAL mode optimizations in `shiftwise/db.py`.
4. Long-term: migrate to PostgreSQL for production concurrency (the Docker
   Compose config already provisions a pg container).

**Code locations:** `shiftwise/db.py`, `shiftwise/scheduler/engine.py:57`,
`shiftwise/routes/employee.py:151,217,236`, `shiftwise/auth.py:97`,
`shiftwise/routes/conflicts.py:108`

---

### C2 — Capacity violation: shifts over-staffed beyond `slots`

**Severity:** CRITICAL · **Reproducible:** Yes (seed 9999)  
**Seeds:** 9999 (confirmed in audit)

**Symptom.** The auditor found shifts where the count of active assignments
exceeds `shifts.slots`. Example: shift #27 staffed above its slot capacity.

**Root cause.** Concurrent `INSERT INTO assignments` from multiple paths
(coverage plan, manager assign, swap accept) don't recheck capacity under a
serialized lock. A read-then-insert pattern allows two writers to both see
"1 slot free" and both insert.

**Fix approach.**
1. Add a capacity recheck inside the `BEGIN IMMEDIATE` transaction in every
   assignment-creating path: `coverage_plan`, `manager_assign`, `approve_request`
   (swap branch), `respond_to_swap` (accept branch).
2. Consider a CHECK constraint or trigger in the schema (though SQLite triggers
   add complexity).

**Code locations:** `shiftwise/scheduler/coverage.py:coverage_plan`,
`shiftwise/routes/conflicts.py:manager_assign`,
`shiftwise/routes/manager.py:approve_request`,
`shiftwise/routes/employee.py:respond_to_swap`

---

### C3 — Pick storm: multiple 5xx during all-employee concurrent submission

**Severity:** CRITICAL · **Reproducible:** Yes (both seeds)  
**Seeds:** 42 (4 requests → 5xx), 9999 (1 request → 5xx)

**Symptom.** When all 19 employees submit picks simultaneously, several return
HTTP 500. The slowest submission took 21.7 seconds (over the 15s busy_timeout).

**Root cause.** Same as C1 — each `/pick` POST triggers `run_scheduler(week)`
which acquires `BEGIN IMMEDIATE`. With 19 concurrent submissions, the lock
queue serializes all rebuilds, and late arrivals time out.

**Fix approach.** Debounce or coalesce scheduler rebuilds: instead of running
`run_scheduler` inline in every pick POST, queue a single rebuild that fires
after a short delay (e.g., 500ms) and coalesces all pending pick changes.

---

## HIGH

### H1 — Orphan rows: deleted shifts leave dangling references

**Severity:** HIGH · **Reproducible:** Yes (every liveweek run)  
**Seeds:** 42 (50+ orphan findings), 9999 (33+ orphan findings)

**Symptom.** After `POST /manager/shift/delete/<id>`, the `picks`,
`coverage_preferences`, and `requests` tables retain rows referencing the
deleted shift ID. The auditor flags these as `orphan_row` for `picks.shift_id`,
`coverage_preferences.shift_id`, `requests.shift_id`, and
`requests.target_shift_id`.

**Root cause.** `shiftwise/routes/manager.py:delete_shift` cascades to
`assignments` and `notifications` but does NOT clean up `picks`,
`coverage_preferences`, or `requests` rows that reference the deleted shift.
The schema has no `FOREIGN KEY` declarations, so SQLite's FK enforcement
doesn't trigger cascading deletes.

**Fix approach.**
1. In `delete_shift`, add `DELETE FROM picks WHERE shift_id=?`,
   `DELETE FROM coverage_preferences WHERE shift_id=?`, and update `requests`
   rows where `shift_id` or `target_shift_id` equals the deleted shift
   (supersede them or null out the reference).
2. Long-term: add proper `FOREIGN KEY` declarations to `shiftwise/db.py:SCHEMA`
   with `ON DELETE CASCADE` where appropriate.

**Code locations:** `shiftwise/routes/manager.py:delete_shift`,
`shiftwise/db.py:SCHEMA`

---

### H2 — Hours cap violated under concurrent assignment

**Severity:** HIGH · **Reproducible:** Yes (both seeds)

**Symptom.** Employees scheduled beyond their `weekly_hours` cap by more than
the 4-hour tolerance. Example: user scheduled 48h against a 40h cap.

**Root cause.** Coverage backfill and concurrent manager assigns don't recheck
the employee's total hours inside the write transaction. A backfill assigns
an employee who is already near their cap, and a concurrent second backfill
does the same before either commits.

**Code locations:** `shiftwise/scheduler/engine.py:run_scheduler`,
`shiftwise/scheduler/coverage.py:coverage_plan`

---

### H3 — Days-off rule violated (> 5 working days per week)

**Severity:** HIGH · **Reproducible:** Yes (seed 42)

**Symptom.** Some employees work 6+ days in a week, violating the 2-day-off
minimum.

**Root cause.** Same concurrency issue as H2 — the scheduler's days-off guard
is checked but not enforced under a serialized transaction that prevents
concurrent writers from both assigning the 6th day.

**Code locations:** `shiftwise/scheduler/engine.py:run_scheduler`

---

### H4 — Authorization bypass under concurrent load (transient)

**Severity:** HIGH · **Reproducible:** Transient (seed 42, ISSUE-122/123)

**Symptom.** Employee `erin` was able to `POST /manager/run` and receive
HTTP 200, and `GET /manager` rendered the full manager dashboard.

**Root cause.** Under heavy concurrent load with the threaded WSGI server,
Flask's session handling may exhibit race conditions. When a manager password
reset changes `erin`'s session state concurrently, or when session cookies
from a previous manager login are reused due to shared cookie jars in the
harness, the authorization check passes. This needs investigation to determine
if it's a real app bug or a harness artifact (the harness uses separate
`WSession` objects but concurrent password resets modify the backing store).

**Code locations:** `shiftwise/auth.py:login_required`,
`shiftwise/routes/manager.py`

---

### H5 — Sick-vs-swap drill: duplicate requests for same shift

**Severity:** HIGH · **Reproducible:** Yes (seed 42, ISSUE-035)

**Symptom.** When an employee simultaneously calls in sick AND requests a swap
on the same shift, both requests are created (2 rows in `requests` for the
same `user_id`/`shift_id`).

**Root cause.** Neither the sick-call path nor the swap path checks for a
concurrent in-flight request on the same shift. The read-then-insert pattern
allows both to proceed.

**Code locations:** `shiftwise/routes/employee.py:request_sick`,
`shiftwise/routes/employee.py:swap`

---

## MEDIUM

### M1 — Pervasive latency under load (3–15s per request)

**Symptom.** Nearly every route exceeds 3 seconds under 19-employee concurrent
traffic. Many exceed 10 seconds. Some hit the 15-second ceiling.

**Root cause.** SQLite's single-writer architecture combined with
`BEGIN IMMEDIATE` in `run_scheduler` serializes all write operations.

---

### M2 — Invite inconsistencies after concurrent modifications

**Symptom.** Swap invites become orphaned: the assignment has `swap_invited`
status but no matching pending request exists, or vice versa.

**Root cause.** Concurrent unassign/accept/delete operations modify the
assignment and request tables independently without checking each other.

---

### M3 — Duplicate vacation requests accepted (no overlap guard)

**Symptom.** drill `double_vacation` confirmed: 2 overlapping vacation
requests for the same employee were both accepted (seed 9999).

**Root cause.** `request_vacation` has no guard against overlapping date
ranges for the same employee. Two concurrent submissions both pass validation.

**Code locations:** `shiftwise/routes/employee.py:request_vacation`

---

## HARNESS

### T1 — `scenario_random.py` crashes after station flip (harness bug)

**Severity:** TEST HARNESS · **Reproducible:** Yes (seed 2356211046)

**Symptom.** `AssertionError: pick rejected for maria` when Maria is flipped
from front-of-house to back-of-house.

**Root cause.** The harness function `shift_id(area, day)` returns only the
*first* shift for a given (area, day) pair. After a station flip, the employee
needs to rank all 21 back-house shifts (3 per day × 7 days), but the form only
submits 7 ranks — one per day. The pick route correctly rejects the incomplete
form.

**Fix.** Replace `shift_id(area, day)` with a function that returns all shift
IDs per area, and build the rank form using all of them (like `day_shift_ids`
in `scenario_demo.py`).

**Code location:** `tools/scenario_random.py:94-97`

---

## Summary table

| ID | Sev | Category | Status |
|---|---|---|---|
| C1 | CRITICAL | `database is locked` → 500 | OPEN |
| C2 | CRITICAL | Capacity violation (over-staffing) | OPEN |
| C3 | CRITICAL | Pick storm 5xx | OPEN (subset of C1) |
| H1 | HIGH | Orphan rows after shift delete | OPEN |
| H2 | HIGH | Hours cap violated | OPEN |
| H3 | HIGH | Days-off rule violated | OPEN |
| H4 | HIGH | Authorization bypass (transient) | NEEDS INVESTIGATION |
| H5 | HIGH | Duplicate sick+swap requests | OPEN |
| M1 | MEDIUM | Pervasive latency | OPEN (root cause = C1) |
| M2 | MEDIUM | Invite inconsistency | OPEN |
| M3 | MEDIUM | Duplicate vacation accepted | OPEN |
| T1 | TEST | scenario_random station-flip crash | OPEN |
