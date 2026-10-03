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
1. **Debounce / Decouple Scheduler Rebuilds**: Move `run_scheduler()` out of the
   inline `POST /pick` request path. Submitting picks is an ultra-fast (<5ms)
   `DELETE` + batch `INSERT`. Coalesce rebuilds via a debounced background worker
   or queue so 19 concurrent submissions trigger 1 schedule rebuild instead of 19
   serialized runs.
2. **Eliminate Write-on-Read**: Move `UPDATE notifications SET read=1` in `GET /`
   to a deferred/asynchronous endpoint or user dismissal action so page loads
   remain purely read-only and never compete for write locks.
3. **Application Retry Loop**: Wrap write transactions with exponential backoff
   and jitter (1–3 retries, 50ms–300ms) for transient `OperationalError: database is locked`.
4. **Architectural Review**: See the dedicated [Architectural Evaluation: SQLite Optimization vs. PostgreSQL Migration](#architectural-evaluation-sqlite-optimization-vs-postgresql-migration)
   section below for a full trade-off analysis and implementation blueprint.

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
**Status:** FIXED (PR #30, commit `bc50845`) — the rank form now covers all shifts per area (cf. `day_shift_ids` in `scenario_demo.py`); verified with the original seed `2356211046` and the pinned gate seed `42`.

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

## Architectural Evaluation: SQLite Optimization vs. PostgreSQL Migration

A key question arising from the chaos and load testing is whether SQLite itself is inadequate for ShiftWise's concurrent workload and whether the application must migrate to PostgreSQL (or another client-server RDBMS).

### 1. Is PostgreSQL Migration Strictly Necessary?

**Short answer:** Not for the current target deployment scale (single-store hospitality/service venue with 15–50 staff), but it **will be** necessary if the platform transitions to a multi-tenant cloud SaaS or multi-node clustered deployment.

#### Why SQLite Failed in the Load Tests
The test harness failures (`sqlite3.OperationalError: database is locked` causing 500s and latency spikes up to 21s) were **not** caused by SQLite's inability to store or retrieve 50 employees' shifts. In WAL mode, SQLite easily handles thousands of read queries per second alongside concurrent writes.

The failure was caused by an **application architectural antipattern**:
1. **Synchronous Full-Week Scheduler Inline with HTTP Requests**: Every `POST /pick` (as well as `/swap`, `/request/vacation`, etc.) runs `run_scheduler(week)` *inline* before responding to the user.
2. **Coarse-Grained Database Locks**: `run_scheduler()` calls `BEGIN IMMEDIATE`, locking the entire database for up to 1.5 seconds while performing iterative rounds of priority matching, allocations, and notification writes.
3. **Queue Overflow**: When 19 employees submit picks within a narrow window (e.g., during the pick storm drill), 19 exclusive transactions queue behind each other. The cumulative wait time (19 × ~1s = ~19s) exceeds SQLite's `busy_timeout` (15s), causing unhandled exceptions.
4. **Write-on-Read Contention**: `GET /` (dashboard) executes `UPDATE notifications SET read=1`, requiring an exclusive write lock on every page load and competing directly with scheduler runs.

**Critical Insight:** Migrating to PostgreSQL without altering this design would **not** solve the problem: 19 concurrent transactions attempting to recompute and write the entire week's schedule simultaneously would trigger PostgreSQL serialization failures (`could not serialize access due to concurrent update`, SQLSTATE 40001), transaction deadlocks, or connection pool exhaustion. The scheduling engine execution model must be optimized regardless of the database engine.

---

### 2. Comparison Matrix: SQLite Optimization vs. PostgreSQL Migration

| Dimension | Option A: Optimize SQLite Architecture | Option B: Migrate to PostgreSQL |
|---|---|---|
| **Primary Remediation** | Decouple scheduler from HTTP request (debounce/queue), remove write-on-read in `GET /`, add retry decorator with jitter. | Move to row-level locking RDBMS; rewrite DB abstraction layer; enforce foreign key cascades. |
| **Concurrency Ceiling** | ~50 concurrent users per store; single-writer bottleneck remains for overlapping writes, but write transactions become ultra-fast (<5ms). | Thousands of concurrent users across multiple stores; true multi-writer concurrency via MVCC and row-level locks. |
| **Operational Complexity** | **Zero**: Single file on disk (`scheduler.db`), no daemon, zero external dependencies, trivial backups (file copy/WAL snapshot). | **Medium**: Requires running, configuring, securing, and backing up a PostgreSQL container/service; managing connection strings, pg_hba, and volumes. |
| **Implementation Effort** | **Low (1–2 PRs)**: Change `/pick` to debounce scheduler runs; remove dashboard write; add a 3-line `@retry_on_locked` decorator. | **High (3–5 PRs)**: Refactor all raw SQL syntax (`?` → `%s`), replace `sqlite3.Row` cursors, configure connection pooling (`psycopg_pool`), adapt schema migrations, update test harnesses. |
| **Test Suite Velocity** | **Fast**: Pytest suite runs in-process with ephemeral SQLite databases in seconds without external network/container dependencies. | **Slower**: Test suites require spinning up test databases, managing schema fixtures, or running dedicated CI service containers. |
| **Multi-Node Deployment** | **Unsupported**: SQLite cannot be shared across multiple Docker containers/servers over network filesystems (NFS/CIFS lock hazards). | **Native**: Any number of web workers across multiple nodes can connect to the shared database instance. |
| **Data Integrity Rails** | Pragmas (`PRAGMA foreign_keys=ON`) required; manual cascade deletes currently implemented. | Native `FOREIGN KEY ... ON DELETE CASCADE` eliminates orphan rows (H1) permanently at the database engine level. |

---

### 3. How to Implement PostgreSQL Migration (Blueprint)

If the roadmap demands PostgreSQL (e.g. for multi-tenant cloud hosting or multi-node clustering), the migration should be executed in disciplined phases:

#### Step 1: Database Abstraction & Driver Layer
- Replace raw `sqlite3` calls in `shiftwise/db.py` with an abstraction layer (or SQLAlchemy Core / `psycopg3`).
- Create a unified query wrapper supporting configurable dialect drivers:
  ```python
  # Abstracted connection and parameter mapping
  DB_ENGINE = os.environ.get("SHIFTWISE_DB_ENGINE", "sqlite")  # "sqlite" | "postgres"
  ```
- Standardize parameter placeholders: SQLite uses `?`, whereas psycopg/PostgreSQL uses `%s` or `$1`. A query adapter function or migration to a unified syntax is required across all route files.

#### Step 2: Schema Translation & Constraints
- Replace SQLite-specific types:
  - `INTEGER PRIMARY KEY` → `BIGSERIAL PRIMARY KEY` or `GENERATED ALWAYS AS IDENTITY`
  - `TEXT` dates → `DATE` / `TIMESTAMP WITH TIME ZONE`
  - `INTEGER` flags → `BOOLEAN`
- Enforce relational constraints directly in DDL:
  ```sql
  ALTER TABLE picks ADD CONSTRAINT fk_picks_shift
    FOREIGN KEY (shift_id) REFERENCES shifts(id) ON DELETE CASCADE;
  ALTER TABLE picks ADD CONSTRAINT fk_picks_user
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE;
  ALTER TABLE assignments ADD CONSTRAINT fk_assignments_shift
    FOREIGN KEY (shift_id) REFERENCES shifts(id) ON DELETE CASCADE;
  ```
- Implement `CHECK (slots >= 1)` and use database-level uniqueness constraints for active assignments to resolve C2.

#### Step 3: Granular Locking for the Scheduler
- Replace `BEGIN IMMEDIATE` with PostgreSQL advisory locks:
  ```sql
  -- Advisory lock scoped to the specific week being scheduled:
  SELECT pg_try_advisory_xact_lock(hashtext('scheduler:' || :week_start));
  ```
  This allows scheduling for Week A and Week B to run concurrently without blocking each other, while cleanly discarding or queuing redundant concurrent triggers for the same week.

#### Step 4: Infrastructure & Container Orchestration
- Update `docker-compose.yml`:
  ```yaml
  services:
    postgres:
      image: postgres:16-alpine
      restart: unless-stopped
      environment:
        POSTGRES_DB: shiftwise
        POSTGRES_USER: shiftwise
        POSTGRES_PASSWORD_FILE: /run/secrets/pg_password
      volumes:
        - pg-data:/var/lib/postgresql/data
      networks:
        - app-backend
  ```
- Add database healthcheck and dependency ordering in `app`.

#### Step 5: Test Harness & CI Compatibility
- Maintain dual-backend test capability: developers and unit tests run fast in-process SQLite; CI integration runs against PostgreSQL.

---

### 4. Recommendation & Roadmap

For full implementation details, task checklists, and forward engineering handoffs across all 5 phases, see [**`docs/POSTGRES_MIGRATION_PLAN.md`**](POSTGRES_MIGRATION_PLAN.md).

1. **Phase 1: Database Adapter Layer & Driver (PR 1)**
   - Add `psycopg[binary,pool]` to `requirements.txt`.
   - Update `shiftwise/db.py` to support `DATABASE_URL` with connection pooling, while maintaining SQLite fallback for lightweight development/testing.
   - Abstract parameter placeholders (`?` vs `%s`).
2. **Phase 2: PostgreSQL Schema, DDL & Relational Constraints (PR 2)**
   - Define PostgreSQL DDL with `BIGSERIAL`, `TIMESTAMPTZ`, `BOOLEAN`, and explicit `ON DELETE CASCADE` foreign keys.
   - Eliminate manual cascade deletes in `routes/manager.py` and `routes/roster.py`.
3. **Phase 3: Route & Engine Query Standardization (PR 3)**
   - Standardize `INSERT ... ON CONFLICT` upserts and date/time functions across all routes and scheduler algorithms.
4. **Phase 4: Scheduler Concurrency, Advisory Locks & Debounce (PR 4)**
   - Replace `BEGIN IMMEDIATE` with PostgreSQL advisory locks (`pg_try_advisory_xact_lock`).
   - Debounce pick submission rebuilds and remove write-on-read from `GET /`.
   - Add row-level locking (`SELECT ... FOR UPDATE`) in `coverage_plan` and `manager_assign` to prevent capacity over-staffing (C2).
5. **Phase 5: Container Orchestration, Deployment & CI (PR 5)**
   - Add `postgres:16-alpine` to `docker-compose.yml` with healthchecks, persistent volumes, and secret management.
   - Verify 100% green pass on `tools/liveweek.py --seed 42 --strict`.

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
| T1 | TEST | scenario_random station-flip crash | FIXED (PR #30) |
