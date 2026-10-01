# ShiftWise Suite: Development & Operation Guidelines

## 1. Branching & PR Discipline
- **Never perform large refactors or multi-phase migrations directly on `master`:**
  - When undertaking non-trivial features, refactors, or reorganizations, create a dedicated feature branch (`git checkout -b <branch-name>`) or a git worktree.
  - Deliver changes through GitHub Pull Requests (`gh pr create`), review the diff, and merge cleanly into `master` once verified.
  - After merging, fast-forward local `master`, prune merged feature branches (both local and remote), and verify that `master` is in sync with `origin/master`.

## 2. Backward Compatibility & Entrypoint Facades
- **Preserve `app.py` as the WSGI and API Facade:**
  - The core domain logic, routes, and database access reside in the modular `shiftwise/` package.
  - Root `app.py` must remain intact for WSGI servers (`app:app`), CLI scripts, and existing test suites.
  - Any module-level monkeypatching expected by legacy tests (such as `app.DB_PATH`, `app.smtplib.SMTP`, `app.urllib.request.urlopen`, and helper re-exports) must dynamically propagate to `shiftwise.db` and related modules. Avoid static descriptor bindings that fail under Python 3.14 module caching.
  - **Re-export Database & Domain Helpers:** Any new public utility or helper added to `shiftwise/db.py` (e.g. `execute_sql`) or domain subpackages must be explicitly imported, re-exported in root `app.py`, and included in `__all__`.

## 3. Workspace Hygiene & Artifact Management
- **No Stale Test Artifacts:**
  - Tests and simulation harnesses may generate SQLite databases (`app.db`, `test_*.db`, `shiftwise-mock/mock.db`, `tools/*.db`) and cache directories (`__pycache__`, `.pytest_cache`).
  - Always clean up temporary databases and caches before committing, creating PRs, or proceeding to deployment.
  - Simulation tools like `tools/liveweek.py` and `tools/scenario_demo.py` write local databases (`tools/*.db`); remove these before pushing or checking status.
  - `git status` must be completely clean (`nothing to commit, working tree clean`) with no untracked artifacts.
- The canonical cleanup is `./tools/clean_artifacts.sh` (databases, `__pycache__`, `*.pyc`, `.pytest_cache`); add `--liveweek-artifacts` to also drop the gauntlet findings kept for fixing agents. `tools/run_all.sh` runs it on entry and on exit, so a normal or aborted gate leaves the tree clean; a `SIGKILL`ed run still needs the manual command.

## 4. Verification Standard

**Tiered regimen — never run the full gate on every small change.**

| When | Command | Cost |
| --- | --- | --- |
| While writing code | `pytest tests/<file>::<test>` (targeted) | seconds |
| Wider check while writing code | `./tools/run_all.sh --fast` (pytest suite only) | ~3 min |
| Before opening a PR or merging | `./tools/run_all.sh` (full gate) | ~6 min |
| Full gate without the load check | `./tools/run_all.sh --no-liveweek` | ~5 min |

- Before marking any phase complete, opening a PR, or deploying, the full gate (`./tools/run_all.sh`) must pass:
  - the full collected pytest unit & integration suite passes 100% green;
  - the 11-phase stress gauntlet (`tools/gauntlet.py`) and `tools/scenario_demo.py` complete without error;
  - the live-week concurrency gauntlet (`tools/liveweek.py`) completes without harness error; its findings are recorded as artifacts in `tools/liveweek-artifacts/` (gitignored) for fixing agents — they are reports, not suite failures (`--strict` enforces them in CI). Duration is configurable via `--liveweek-seconds N` or `SHIFTWISE_LIVEWEEK_SECONDS`;
  - `docker compose config` validates.
- `./tools/run_all.sh --fast` runs the pytest suite only (for the inner development loop) and prints the full-gate reminder.
- Dev extras (`requirements-dev.txt`: `pytest-timeout`, `pglast`) are required for a fully green suite: without `pglast` the PostgreSQL DDL parser test skips, and `pytest.ini` caps each test at 180 s so a lock wait or dialect error cannot hang a run.
- `tools/check_entrypoints.py` (run as step `[0]` of `tools/run_all.sh`) asserts the tracked shell entrypoints (`tools/run_all.sh`, `tools/clean_artifacts.sh`, `docker-entrypoint.sh`, `shiftwise-mock/sync.sh`) keep mode `100755` via `git ls-files -s`. It catches the regression class PR #18 shipped: a dropped executable bit silently breaks the documented `./tools/run_all.sh` while every test still passes.
- `.github/workflows/ci.yml` runs only that guard on pushes to `master` and on pull requests (no pytest, docker, or venv). It is not a required status check — blocking merges needs branch protection, which needs repo admin.

## 5. Simulation Harness & Mock Roster Coupling
- **Dynamic Staffing Expectations:**
  - Simulation harnesses (`tools/liveweek.py`, `tools/gauntlet.py`, `tools/scenario_demo.py`) must compute expected roster size and shift counts dynamically from `mock_seed.PREFS` / `DEMO_SHIFTS` rather than hardcoding constants.
- **Transient Entity Namespace Isolation:**
  - Test harnesses that simulate transient hires, deletions, or dynamic users (e.g. `TRANSIENT_HIRES` in `liveweek.py`) must never reuse usernames present in `mock_seed.py` to prevent unique constraint collisions and state contamination.

## 6. Known Issues & Concurrency Backlog
- **Consult `docs/KNOWN_ISSUES.md`** before working on any concurrency, database locking, or data-integrity fix.
  - The document catalogues real findings from `tools/liveweek.py` chaos/load testing, with root-cause analysis, affected code locations, and fix approaches.
  - Items are severity-ranked (CRITICAL → MEDIUM). Work them one per PR, highest severity first.
  - Each item includes a liveweek seed for reproducibility — re-run with `--seed N` to verify a fix.
- **`docs/REFACTORING_BACKLOG.md`** lists structural refactors (code cleanliness, not bugs). Do not confuse these with known issues — they are separate work streams.
- **`docs/POSTGRES_MIGRATION_PLAN.md`** contains the approved 5-phase engineering handoff for migrating from SQLite to PostgreSQL to resolve concurrency, locking, and multi-store scalability requirements.

## 7. Database Adapter & Connection Pool Lifecycle Standards
- **`sqlite3.Row` Emulation Semantics:**
  - Any custom row wrapper emulating `sqlite3.Row` (such as `_PostgresRow`) must behave as a sequence whose `__iter__` yields column **values**, not keys. Iterating over a `dict` yields keys, which breaks sequence unpacking (`id, username = row`), `tuple(row)`, and `list(row)`.
  - Description column names must be extracted safely (`column.name if hasattr(column, "name") else column[0]`) to avoid eager evaluation errors when description objects do not support indexing.
- **Connection Pool Lease Isolation:**
  - Connection facades wrapping physical pooled connections (`_PostgresConnection`) must clear their underlying connection reference on `close()` and raise `RuntimeError` on any subsequent method invocation. Never allow a closed facade to execute queries or commit transactions on a physical connection returned to the pool.
- **Context Manager Facades:**
  - Facades wrapping connections and cursors must implement `__enter__` returning `self` (the facade), ensuring that parameter translations (e.g. `?` to `%s`) remain active within `with conn:` and `with cur:` blocks.

