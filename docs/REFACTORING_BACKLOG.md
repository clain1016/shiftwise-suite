# Refactoring Backlog

> Status: all six items merged (R1–R6) · Last reviewed: 2026-10-02

Bigger, judgment-call refactors left out of the mechanical cleanup in PR #13.
Each item is specified so a future agent (or human) can pick it up and implement
it independently. Work them **one item per PR**, branched from current `master`.

## How to work this backlog

- Suggested order: R1 → R6 → R5 → R3 → R2 → R4 (small and safe first; R4 last
  because it rewrites many lines — coordinate with whoever is actively pushing).
- Verify every item with: `./tools/run_all.sh` (as mandated by `AGENTS.md`), or
  the targeted subset during fast iteration: `pytest tests/ -q`,
  `python -m pyflakes shiftwise/ tests/ tools/`, and `tools/gauntlet.py` for
  scheduler-adjacent changes.
- Never mix a refactor with behavior changes in one PR. Keep flash messages,
  status codes, and route URLs byte-identical unless the item says otherwise —
  `tools/liveweek.py` asserts on several of them.

---

## R1 — Untangle the hidden test seam (`notify.py` / `app.py`)

**Status:** implemented in [PR #23](https://github.com/clain1016/shiftwise-suite/pull/23).

Notification delivery uses module-level `_smtplib` and `_urllib` handles. The
roster route uses its imported `send_schedule_link`. Neither module looks up
`app` in `sys.modules`.

Tests patch these names directly. The `app.py` facade also forwards replacements
of `app.smtplib`, `app.urllib`, and `app.send_schedule_link` to their consumers.
Patching `app.smtplib.SMTP` or `app.urllib.request.urlopen` still works through
the shared library objects. `tests/test_notification_facade.py` covers legacy
patches and restoration without external delivery calls.

**Verification.** Run `pytest tests/test_employee_schedule_links.py
tests/test_notification_facade.py -v` and the full `./tools/run_all.sh` gate.
Pyflakes must report no new findings in the touched files; the existing dynamic
`DB_PATH` export warning remains tracked under R5. The `shiftwise/db.py` lookup
of `app` for demo seeding is outside this notification refactor.

## R2 — Split `run_scheduler` into phases

**Problem.** `shiftwise/scheduler/engine.py:35` is ~280 lines with nesting down to
~45 spaces. It's the core algorithm — the most important code in the repo — and
it's one giant function.

**Approach.** Extract phase helpers (e.g. `_collect_week`, `_assign`, `_notify`,
`_finalize` — name them by what the code actually does when you read it). Keep
`run_scheduler(week_start, actor="system")` as the orchestrator with the same
signature and return value. Move code only; change no logic.

**Acceptance.** Full pytest suite green; `tools/gauntlet.py` and
`tools/scenario_demo.py` complete without error (diff their printed summaries
before/after if feasible).

**Risk.** This is the highest-blast-radius item here. One PR, no other changes
mixed in, and a careful re-read of the final diff.

**Status:** implemented in [PR #28](https://github.com/clain1016/shiftwise-suite/pull/28).

`run_scheduler()` is now a thin orchestrator over eight phase helpers
(`_begin_week`, `_collect_inputs`, `_prepare_rebuild`,
`_run_assignment_rounds`, `_backfill_coverage`, `_resolve_swaps`,
`_notify_schedule_changes`, `_finalize_week`) sharing an explicit
per-run `_SchedulerState`. Move-only: full pytest suite green (115 passed),
and the gauntlet / scenario_demo printed summaries are byte-identical
before/after.

## R3 — `approve_request` dispatch dict

**Status:** implemented in [PR #26](https://github.com/clain1016/shiftwise-suite/pull/26).

**Problem.** `shiftwise/routes/manager.py:186` is ~127 lines of `if/elif` over the
request kinds. Every new request type makes it longer.

**Approach.** Extract per-kind handlers (`_approve_day_off`, `_approve_vacation`,
`_approve_sick`, `_approve_swap`, `_approve_switch` — confirm the exact kinds in
the code) and dispatch via a dict keyed by request kind; unknown kind → 400.
Keep every flash message and status code identical.

**Acceptance.** `pytest tests/test_requests.py tests/test_manager_override.py`
green; each request kind exercised (the test files cover them).

## R4 — Adopt ruff (lint + format)

**Status:** implemented in [PR #27](https://github.com/clain1016/shiftwise-suite/pull/27) (merged 2026-10-02).

**Problem.** No formatter/linter config exists; style is enforced by hand
(PR #13 hand-wrapped ~15 long lines).

**Approach.**
1. Create `pyproject.toml` with `[tool.ruff]`: `line-length = 100`,
   `target-version = "py312"`, and a small starting rule set (`F`, `E4`, `E7`,
   `E9`, `I` for import sorting).
2. Run `ruff check --fix` then `ruff format`; review the diff (expect mostly the
   already-wrapped lines plus import sorting).
3. Add ruff to `requirements-dev.txt` and document in `AGENTS.md`: run
   `ruff check` and `ruff format --check` before opening PRs.

**Acceptance.** `ruff check` clean, `ruff format --check` clean, pytest green.

**Risk.** Large diff — do this when `master` is quiet and nobody has big branches
open.

## R5 — `app.py` `__all__` / `DB_PATH` proxy

**Status:** implemented in [PR #25](https://github.com/clain1016/shiftwise-suite/pull/25).

**Problem (resolved).** `app.py` listed `"DB_PATH"` in `__all__`, but nothing bound
that name at module level — it only resolved via the `_AppModule.__getattribute__`
proxy. Works at runtime; invisible to static tools and confusing to new contributors.

**Approach & Resolution (approach (a) per `AGENTS.md` §2).** Kept the `_AppModule`
proxy and documented the facade contract in detail:
- Explains why `DB_PATH` is a dynamic proxy rather than a static binding.
- Documents the `AGENTS.md` §2 requirement that per-test database monkeypatches
  (`app.DB_PATH = ...`) and legacy notification patches (`smtplib`, `urllib`,
  `send_schedule_link`) must propagate to the underlying implementation modules.
- Explains why static descriptors fail under Python 3.14 module caching.
- Explains why pyflakes' `undefined name 'DB_PATH' in __all__` is expected and
  must not be "fixed" by a static assignment (which would break test isolation).
- Added an inline annotation on `"DB_PATH"` in `__all__` directing readers to
  the `_AppModule` docstring.

**Acceptance.** `pyflakes app.py` clean (only expected undefined-name note); full suite green.

## R6 — Restore the `mock_seed` force guard

**Status:** implemented in [PR #24](https://github.com/clain1016/shiftwise-suite/pull/24).

**Problem (resolved).** `mock_seed.seed()` used to refuse wiping a non-empty DB without
`force=True`; the version before this change wiped unconditionally. All present callers are
test/tooling/demo paths, but the rail existed for a reason.

**Resolution.** `mock_seed.seed(appmod, force=False)` refuses to wipe a non-empty database
unless `force=True` (raises `RuntimeError`). The connection is closed before
raising to avoid leaking pool leases on PostgreSQL. All callers intending to
wipe now pass `force=True`.

**Approach.** Restore the guard (raise unless the users table is empty or
`force=True`), and update every call site to pass `force=True` where wiping is
intended. Grep for all `seed(` call sites first (`mock_seed.seed(`, `seed(appmod)`,
the docker entrypoint, `sync.sh`).

**Acceptance.** Calling `seed()` on a non-empty DB without `force=True` raises;
all callers updated; tests green.

**Risk.** Low, but a missed call site breaks a demo/test path — the grep is the
important step.
