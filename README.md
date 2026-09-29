# ShiftWise Suite

ShiftWise — auto-scheduling staff scheduler (Flask + SQLite): priority lineup, self-service day-off/vacation/sick/swap requests, automatic coverage backfill — plus its mock demo twin in one repository.

## Layout

- `/` — the real app (ShiftWise). Run it on port 5000:
  `python app.py`
- `/shiftwise-mock/` — mock demo twin (8 fake employees, Mon-Sun demo week, pre-seeded picks) for instant click-testing with no manual data entry. Runs on port 5001:
  `cd shiftwise-mock && python app.py`

## Mock workflow

After every feature added to the real app, run:

    ./shiftwise-mock/sync.sh

which copies app.py + templates from the real app into the mock, ports it to 5001, and reseeds via `mock_seed.py`.

## Tests

- Real app: `test_priority.py`, `test_flow.py`, `test_calendar.py`, `test_conflicts.py`, `test_requests.py`, `test_days_off.py`, `test_manager_pick.py`, `test_preferred_schedule.py`, `test_rebuild_recompute.py` (repo root)
- Mock: `shiftwise-mock/test_mock.py`
