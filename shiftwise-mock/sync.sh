#!/usr/bin/env bash
# Sync the mock ShiftWise twin from the real app after every feature addition.
# Copies app.py + templates, fixes the port to 5001, and (with --reseed)
# force-reseeds the mock DB with the fake FOH/BOH roster and Mon-Sun demo week.
set -euo pipefail

cd "$(dirname "$0")"
if [[ $# -gt 1 || ( $# -eq 1 && "$1" != "--reseed" ) ]]; then
    echo "usage: $0 [--reseed]" >&2
    exit 2
fi

python_bin="$(cd .. && pwd)/.venv/bin/python"
if [[ ! -x "$python_bin" ]]; then
    echo "Missing suite virtualenv; run python3 -m venv .venv and install requirements.txt" >&2
    exit 1
fi

cp ../app.py app.py
cp ../templates/*.html templates/

# The mock runs on its own port and database. Keep these settings local to
# the copied entry point so the scheduler logic stays identical.
"$python_bin" - <<'PY'
from pathlib import Path

path = Path("app.py")
source = path.read_text()
source = source.replace('os.environ.get("SHIFTWISE_PORT", "5000")',
                        'os.environ.get("SHIFTWISE_PORT", "5001")')
path.write_text(source)
PY

if [[ "${1:-}" == "--reseed" ]]; then
    rm -f scheduler.db scheduler.db-wal scheduler.db-shm
    SHIFTWISE_DB_PATH="$PWD/scheduler.db" "$python_bin" - <<'PY'
import app
import mock_seed

app.init_db(seed_demo=True)
employees, days, picks = mock_seed.seed(app, force=True)
print(f"mock database seeded: {employees} fake employees (FOH+BOH), "
      f"{days} days/house, {picks} pre-seeded picks")
PY
fi

echo "mock source synced; restart the mock server to load it"
