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

## 3. Workspace Hygiene & Artifact Management
- **No Stale Test Artifacts:**
  - Tests and simulation harnesses may generate SQLite databases (`app.db`, `test_*.db`, `shiftwise-mock/mock.db`, `tools/*.db`) and cache directories (`__pycache__`, `.pytest_cache`).
  - Always clean up temporary databases and caches before committing, creating PRs, or proceeding to deployment.
  - `git status` must be completely clean (`nothing to commit, working tree clean`) with no untracked artifacts.

## 4. Verification Standard
- Before marking any phase complete, opening a PR, or deploying:
  - Run the full verification suite: `./tools/run_all.sh`
-  Ensure the full collected pytest unit & integration suite passes (currently 66 tests, 100% green).
  - Ensure the 11-phase stress gauntlet and scenario simulations complete without error.
  - Validate Docker Compose configuration with `docker compose config`.
