#!/usr/bin/env bash
# Reseed utility for the ShiftWise mock environment.
# Note: Source code and templates are now shared directly with the root
# application; this script provides an optional database reseed utility.
set -euo pipefail

cd "$(dirname "$0")"
if [[ $# -gt 1 || ( $# -eq 1 && "$1" != "--reseed" ) ]]; then
    echo "usage: $0 [--reseed]" >&2
    exit 2
fi

python_bin="$(cd .. && pwd)/.venv/bin/python"
if [[ ! -x "$python_bin" ]]; then
    python_bin="python3"
fi

if [[ "${1:-}" == "--reseed" ]]; then
    rm -f mock.db mock.db-wal mock.db-shm
    SHIFTWISE_DB_PATH="$PWD/mock.db" "$python_bin" - <<'PY'
import sys
from pathlib import Path
ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))
import app
import mock_seed
from datetime import date

app.init_db(seed_demo=True)
employees, days, picks = mock_seed.seed(app, force=True)
week = mock_seed.monday_of(date.today()).isoformat()
app.run_scheduler(week)
print(f"mock database seeded: {employees} employees (FOH+BOH), "
      f"{days} days/house, {picks} pre-seeded picks")
PY
    echo "mock database reseeded successfully"
else
    echo "Mock environment directly uses root app and templates (no file copying required)."
    echo "To reseed the mock database, run: ./sync.sh --reseed"
fi
