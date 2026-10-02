# PostgreSQL Migration Plan & Development Handoff

> Status: Phase 1 complete (merged 2026-10-01) · Phase 2 complete (merged 2026-10-01) · Phases 3–5 pending · Target: Multi-worker concurrency, zero-lock contention, multi-store scalability · Last updated: 2026-10-01

This document serves as the complete, step-by-step engineering specification and forward handoff for migrating ShiftWise Suite from SQLite to PostgreSQL. Any future agent or engineer can pick up this roadmap and implement it phase-by-phase without ambiguity.

> **Current state (2026-10-01):** Phase 1 is merged to `master` as `59549352` (PR #16). Phase 2 (schema & constraints) is merged to `master` as `28792e34` (PR #17): PostgreSQL DDL with declarative `ON DELETE CASCADE` / `ON DELETE SET NULL` FKs, per-engine `init_db()` dispatch, and engine-conditional cascade deletions in the delete routes. Next up: Phase 3 (query & route migration).

---

## 1. Problem Statement & Motivation

During chaos and concurrency gauntlet testing (`tools/liveweek.py` on seeds 42 and 9999), the single-writer architecture of SQLite produced critical failures under 19-employee concurrent load:
- **C1 & C3:** `sqlite3.OperationalError: database is locked` causing HTTP 500s and up to 21-second latency spikes when write transactions queued behind `run_scheduler()`'s `BEGIN IMMEDIATE`.
- **C2:** Over-capacity shift staffing due to non-serialized read-then-insert paths.
- **H1:** Persistent orphan rows in `picks`, `coverage_preferences`, and `requests` after shift deletions because SQLite lacks declarative `ON DELETE CASCADE` foreign keys.
- **Write-on-Read Contention:** Routine page loads (`GET /`) executing `UPDATE notifications SET read=1` collided with background scheduler rebuilds.

Migrating to PostgreSQL establishes true multi-writer concurrency (MVCC), row-level locking, declarative relational integrity, and the capability to scale horizontally across multi-node container clusters or multi-tenant SaaS environments without dialect debt.

---

## 2. Target Architecture

```
       +------------------------------------------------+
       |                  Web Browser                   |
       |  (Employees & Managers via Responsive HTML/CSS)|
       +-----------------------+------------------------+
                               | HTTP
       +-----------------------v------------------------+
       |                     Caddy                      |
       |         (Reverse Proxy, TLS, Headers)          |
       +-----------------------+------------------------+
                               | Proxy (Port 5000)
       +-----------------------v------------------------+
       |                    Gunicorn                    |
       |            (WSGI Multi-worker 4-8)             |
       +-----------------------+------------------------+
                               | psycopg_pool ConnectionPool
       +-----------------------v------------------------+
       |                  Flask App                     |
       |     (shiftwise/db.py Database Interface)       |
       +-----------------------+------------------------+
                               | TCP (Port 5432)
       +-----------------------v------------------------+
       |               PostgreSQL 16 Alpine             |
       |   - Row-level locking (MVCC)                   |
       |   - Declarative ON DELETE CASCADE              |
       |   - Advisory Locks for Scheduler Coalescing    |
       |   - Persistent volume: postgres-data           |
       +------------------------------------------------+
```

---

## 3. Phased Implementation Roadmap

Per `AGENTS.md` guidelines, each phase is executed in a **dedicated feature branch and PR**, with verification before merging into `master` (targeted tests for the changed area by default — see the handoff checklist).

```mermaid
flowchart LR
    P1["Phase 1: DB Adapter & Driver<br/>(PR 1)"] --> P2["Phase 2: Schema & Constraints<br/>(PR 2)"]
    P2 --> P3["Phase 3: Query & Route Migration<br/>(PR 3)"]
    P3 --> P4["Phase 4: Scheduler Advisory Locks<br/>(PR 4)"]
    P4 --> P5["Phase 5: Docker & CI Deployment<br/>(PR 5)"]
```

---

### Phase 1: Database Adapter Layer & Driver — COMPLETE (PR #16, merged 2026-10-01 as `59549352`)

**Objective:** Introduce driver dependencies and build a unified connection manager in `shiftwise/db.py` that supports `DATABASE_URL` (PostgreSQL) while preserving SQLite compatibility for lightweight in-memory/in-process tests.

#### Tasks:
1. **Dependencies:** Add `psycopg[binary,pool]>=3.2.0` to `requirements.txt`.
2. **Connection Pooling & Factory in `shiftwise/db.py`:**
   - Detect engine from `DATABASE_URL` or `SHIFTWISE_DB_ENGINE` (`postgres` vs `sqlite`).
   - If PostgreSQL:
     - Initialize a module-level `ConnectionPool(conninfo=DATABASE_URL, min_size=4, max_size=20)`.
     - Implement `db()` returning a connection wrapper or pooled connection.
     - Configure row factory to return dict/attribute-accessible rows matching `sqlite3.Row` semantics: positional index (`row[0]`), key access (`row["id"]`), and sequence value-iteration (`__iter__` yielding values for `id, name = row` unpacking).
     - Invalidate connection references upon `close()` to protect against cross-thread operations on returned connections.
     - Ensure connection/cursor facades return `self` on `__enter__` to preserve placeholder translation in context managers.
   - If SQLite (fallback / test mode):
     - Maintain existing `sqlite3.connect` logic with `busy_timeout=15000` and `foreign_keys=ON`.
3. **Parameter Syntax Adapter:**
   - Provide a helper function `execute_sql(conn, query, params=())` that normalizes parameter placeholders:
     - SQLite uses `?`.
     - PostgreSQL uses `%s`.
     - Alternatively, standardize all queries on `%s` and adapt for SQLite, or use named parameters (`:name`).
4. **App Facade Compatibility (`app.py`):**
   - Ensure `app:app` exports and test monkeypatching continue to propagate cleanly per `AGENTS.md` §2.
   - Re-export `execute_sql` in `app.py` and `__all__`.

#### Acceptance Criteria:
- `requirements.txt` installs cleanly.
- `python -c "import psycopg"` succeeds in `.venv`.
- All existing pytest tests continue to pass in SQLite mode without regression (baseline 89 tests).
- Automated tests in `tests/test_db_adapter.py` verify backend selection, qmark translation, row semantics, connection lease lifecycle, and `app.py` facade exports.

**Delivered 2026-10-01** (PR #16, merge `59549352`): all tasks above, plus two review fixes —
literal `%` is escaped as `%%` in `_qmark_to_psycopg` (psycopg treats `%` as a placeholder
introducer even inside string literals, so the scheduler's `LIKE 'No cover available%'`
query would otherwise raise a placeholder-parsing error), and `_PostgresConnection.close()`
returns the physical connection inside try/finally so a `putconn` failure detaches the lease
instead of leaking it. Full suite green on the merged head: 89 passed.

---

### Phase 2: PostgreSQL Schema, DDL & Relational Constraints (PR 2)

**Objective:** Define production PostgreSQL DDL with declarative foreign keys and cascade rules to permanently resolve Issue H1 (orphan rows).

#### Tasks:
1. **Define PostgreSQL DDL in `shiftwise/db.py`:**
   ```sql
   CREATE TABLE IF NOT EXISTS users (
       id BIGSERIAL PRIMARY KEY,
       username TEXT UNIQUE NOT NULL,
       password TEXT NOT NULL,
       name TEXT NOT NULL,
       role TEXT NOT NULL DEFAULT 'employee',
       weekly_hours INTEGER DEFAULT 40,
       employment_type TEXT NOT NULL DEFAULT 'part_time',
       hired_on DATE,
       station TEXT NOT NULL DEFAULT 'front',
       email TEXT,
       phone TEXT,
       time_format TEXT NOT NULL DEFAULT '24h'
   );

   CREATE TABLE IF NOT EXISTS shifts (
       id BIGSERIAL PRIMARY KEY,
       week_start DATE NOT NULL,
       day TEXT NOT NULL,
       start_time TIME NOT NULL,
       end_time TIME NOT NULL,
       slots INTEGER NOT NULL DEFAULT 1 CHECK (slots >= 1),
       note TEXT,
       area TEXT NOT NULL DEFAULT 'front'
   );

   CREATE TABLE IF NOT EXISTS picks (
       id BIGSERIAL PRIMARY KEY,
       user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
       shift_id BIGINT NOT NULL REFERENCES shifts(id) ON DELETE CASCADE,
       rank INTEGER NOT NULL,
       UNIQUE(user_id, shift_id)
   );

   CREATE TABLE IF NOT EXISTS coverage_preferences (
       user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
       shift_id BIGINT NOT NULL REFERENCES shifts(id) ON DELETE CASCADE,
       willing BOOLEAN NOT NULL DEFAULT TRUE,
       PRIMARY KEY(user_id, shift_id)
   );

   CREATE TABLE IF NOT EXISTS assignments (
       id BIGSERIAL PRIMARY KEY,
       shift_id BIGINT NOT NULL REFERENCES shifts(id) ON DELETE CASCADE,
       user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
       status TEXT NOT NULL DEFAULT 'proposed',
       UNIQUE(shift_id, user_id)
   );

   CREATE TABLE IF NOT EXISTS requests (
       id BIGSERIAL PRIMARY KEY,
       user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
       kind TEXT NOT NULL,
       shift_id BIGINT REFERENCES shifts(id) ON DELETE SET NULL,
       day TEXT,
       target_shift_id BIGINT REFERENCES shifts(id) ON DELETE SET NULL,
       vacation_start DATE,
       vacation_end DATE,
       week_start DATE,
       target_user_id BIGINT REFERENCES users(id) ON DELETE SET NULL,
       reason TEXT,
       status TEXT NOT NULL DEFAULT 'approved',
       created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
   );

   CREATE TABLE IF NOT EXISTS notifications (
       id BIGSERIAL PRIMARY KEY,
       user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
       shift_id BIGINT REFERENCES shifts(id) ON DELETE SET NULL,
       kind TEXT NOT NULL,
       message TEXT NOT NULL,
       created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
       read BOOLEAN NOT NULL DEFAULT FALSE
   );

   CREATE TABLE IF NOT EXISTS login_attempts (
       key TEXT PRIMARY KEY,
       failures INTEGER NOT NULL DEFAULT 0,
       locked_until TIMESTAMPTZ
   );
   ```
2. **Schema Migration / Seeding:**
   - Update `init_db()` to run the appropriate DDL based on the configured engine.
   - Update `mock_seed.py` so demo seeding runs cleanly on PostgreSQL.
3. **Eliminate Manual Cascade Deletions:**
   - Remove fragile manual cascade deletions in `shiftwise/routes/manager.py:delete_shift` and `shiftwise/routes/roster.py:delete_employee`. PostgreSQL's `ON DELETE CASCADE` handles these atomically.

#### Acceptance Criteria:
- Schema initializes without errors on PostgreSQL.
- Deleting a shift automatically drops associated picks, coverage preferences, and assignments.
- Orphan row checks (`PRAGMA foreign_key_check` equivalent queries) find 0 violations.

**Delivered 2026-10-01** (PR #17, merge `28792e34`): all tasks above, plus fixes from a live-PostgreSQL-16 review — `conn.commit()` after the DDL before the `mock_seed` handoff (the pool rolled back the uncommitted schema otherwise), `HH:MM` normalization of `TIME` columns in `mock_seed.py`, try/finally pool-lease return in `_init_db_postgres`, defensive `statement_timeout`/`lock_timeout` on the pool, a quote-aware `_split_ddl` lexer, and per-FK test pins. One deliberate deviation from the task text: manual cascade deletions were made engine-conditional rather than removed outright, because the SQLite schema declares no foreign keys — removing them would regress SQLite. Targeted tests green on the merged head (27 passed).

---

### Phase 3: Route & Engine Query Standardization (PR 3)

**Objective:** Audit and adapt all SQL statements across route modules and scheduler algorithms to be fully compatible with PostgreSQL.

#### Areas of Focus:
1. **Conflict Handling (`INSERT ... ON CONFLICT`):**
   - Replace any SQLite `INSERT OR REPLACE` / `INSERT OR IGNORE` with ANSI standard `ON CONFLICT (...) DO UPDATE` or `ON CONFLICT (...) DO NOTHING`.
   - Update `picks`, `coverage_preferences`, and `assignments` upsert operations.
2. **Date & Time Calculations:**
   - Review Julian day math in `tools/gauntlet.py` and `shiftwise/`:
     - SQLite: `julianday(s.end_time) - julianday(s.start_time)`
     - PostgreSQL: standard time difference `EXTRACT(EPOCH FROM (end_time - start_time)) / 3600.0`.
   - Ensure `start_time` and `end_time` formatting matches `HH:MM:SS` or `HH:MM`.
3. **Boolean Handling:**
   - Normalize boolean values (`0/1` vs `TRUE/FALSE`) for `notifications.read` and `coverage_preferences.willing`.
4. **Files to Update:**
   - `shiftwise/routes/auth.py`
   - `shiftwise/routes/employee.py`
   - `shiftwise/routes/manager.py`
   - `shiftwise/routes/conflicts.py`
   - `shiftwise/routes/calendar.py`
   - `shiftwise/routes/roster.py`
   - `shiftwise/scheduler/engine.py`
   - `shiftwise/scheduler/coverage.py`
   - `shiftwise/notify.py`

#### Acceptance Criteria:
- Targeted pytest tests for every touched route/module pass 100% green against PostgreSQL (run the full suite only if the changes are cross-cutting or failures suggest wider impact).
- No `SyntaxError` or dialect mismatch errors in any route.

---

### Phase 4: Scheduler Concurrency, Advisory Locks & Debounce (PR 4)

**Objective:** Solve the core scheduling bottleneck (C1, C3) using PostgreSQL transaction advisory locks and asynchronous coalescing.

#### Tasks:
1. **Advisory Locking in `run_scheduler()`:**
   - Replace coarse `BEGIN IMMEDIATE` with a week-scoped PostgreSQL advisory lock:
     ```python
     # Hash the week string into a 64-bit integer key
     week_key = zlib.crc32(week_start.encode("utf-8"))
     conn.execute("SELECT pg_try_advisory_xact_lock(%s)", (week_key,))
     ```
   - If another worker is already computing that week's schedule, subsequent triggers cleanly skip or wait without table-level deadlocks.
2. **Debounce Pick Submission Rebuilds:**
   - In `shiftwise/routes/employee.py:pick`:
     - Save employee picks immediately (<3ms transaction).
     - Instead of running `run_scheduler(week)` synchronously inline, enqueue a debounced rebuild task (or set a `rebuild_pending = TRUE` timestamp in a control table).
   - This ensures that when 20 employees submit preferences simultaneously, the scheduler runs once after a settle window (e.g. 500ms) rather than 20 serialized consecutive runs.
3. **Eliminate Write-on-Read in Dashboard:**
   - In `shiftwise/routes/employee.py:dashboard`:
     - Remove `conn.execute("UPDATE notifications SET read=1 WHERE user_id=?", ...)` from the `GET /` path.
     - Move notification clearing to an explicit endpoint (`POST /notifications/read-all`) or an asynchronous fetch call.
4. **Capacity Validation Under Row Lock (Fix C2):**
   - In `coverage_plan` and `manager_assign`, acquire row-level locks on the target shift:
     ```sql
     SELECT slots, (SELECT COUNT(*) FROM assignments WHERE shift_id = s.id AND status NOT IN ('sick','swap_requested')) AS current_staff
     FROM shifts s WHERE s.id = %s FOR UPDATE;
     ```
   - If `current_staff >= slots`, reject assignment atomically to prevent over-capacity.

#### Acceptance Criteria:
- Run `tools/liveweek.py --seed 42 --strict` against PostgreSQL:
  - 0 HTTP 500 errors.
  - 0 `database is locked` exceptions.
  - 0 capacity violations (Issue C2 resolved).
  - P95 request latency drops below 200ms.

---

### Phase 5: Container Orchestration, Deployment & CI (PR 5)

**Objective:** Integrate PostgreSQL into the production container stack and CI test runners.

#### Tasks:
1. **Update `docker-compose.yml`:**
   ```yaml
   services:
     postgres:
       image: postgres:16-alpine
       restart: unless-stopped
       environment:
         POSTGRES_DB: ${POSTGRES_DB:-shiftwise}
         POSTGRES_USER: ${POSTGRES_USER:-shiftwise}
         POSTGRES_PASSWORD_FILE: /run/secrets/pg_password
       secrets:
         - pg_password
       volumes:
         - postgres-data:/var/lib/postgresql/data
       networks:
         - app-internal
       healthcheck:
         test: ["CMD-SHELL", "pg_isready -U ${POSTGRES_USER:-shiftwise} -d ${POSTGRES_DB:-shiftwise}"]
         interval: 10s
         timeout: 5s
         retries: 5

     app:
       # ...
       environment:
         DATABASE_URL: postgresql://${POSTGRES_USER:-shiftwise}@postgres:5432/${POSTGRES_DB:-shiftwise}
         PGPASSWORD_FILE: /run/secrets/pg_password
       depends_on:
         postgres:
           condition: service_healthy
       networks:
         - app-proxy
         - app-internal

   volumes:
     postgres-data: {}

   secrets:
     pg_password:
       file: ./secrets/pg_password.txt
   ```
2. **Update Environment & Deployment Docs:**
   - Update `docs/ENVIRONMENT.md` with `DATABASE_URL`, `POSTGRES_USER`, `POSTGRES_DB`.
   - Update `docs/DEPLOYMENT.md` with database backup/restore procedures (`pg_dump`, `pg_restore`).
3. **CI Pipeline Integration:**
   - Configure GitHub Actions workflow with a `services.postgres` container so `./tools/run_all.sh` executes against a live PostgreSQL instance in CI.

#### Acceptance Criteria:
- `docker compose config` validates without error.
- Full container stack boots cleanly with `docker compose up -d`.
- `./tools/run_all.sh` passes 100% green against the containerized PostgreSQL database.

---

## 4. Handoff Checklist for Implementing Agents

When an agent begins work on any phase:

1. **Branch Hygiene:** Create a branch prefixed with the phase name: `git checkout -b postgres-phase1-driver`.
2. **Local PostgreSQL Setup for Development:**
   ```sh
   # Start local PostgreSQL in Docker for testing
   docker run --name shiftwise-pg-dev -e POSTGRES_PASSWORD=devpass -e POSTGRES_DB=shiftwise_test -p 5432:5432 -d postgres:16-alpine
   export DATABASE_URL="postgresql://postgres:devpass@127.0.0.1:5432/shiftwise_test"
   ```
3. **Verify Targeted Tests Before Making Changes:**
   Run only the tests covering the area the phase touches — not the full suite:
   ```sh
   .venv/bin/pytest tests/test_<area>.py -q
   ```
4. **Execute the Phase Tasks** as specified in Section 3 above.
5. **Run Targeted Verification:**
   Re-run the targeted tests from step 3 plus any new tests the phase adds. Escalate to `./tools/run_all.sh --fast` (pytest suite only) or the full `./tools/run_all.sh` gate only when the phase touches shared/cross-cutting code, when targeted-test failures suggest wider impact, or when Matthew explicitly asks.
6. **Open PR & Review:**
   - Deliver via GitHub PR (`gh pr create`).
   - Cross-reference `docs/POSTGRES_MIGRATION_PLAN.md` and mark the phase complete.
