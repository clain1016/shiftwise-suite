#!/bin/bash
# Sync the mock ShiftWise twin from the real app after every feature addition.
# Copies app.py + templates, fixes the port to 5001, then force-reseeds the
# mock DB with 10 fake employees (5 FOH / 5... 4 BOH) and a full Mon-Sun demo
# week split into front-of-house and back-of-house schedules.
set -e
cd "$(dirname "$0")"
cp ../app.py app.py
cp ../templates/*.html templates/
/home/cody/scheduler/.venv/bin/python - <<'EOF'
import pathlib
p = pathlib.Path("app.py")
src = p.read_text()
src = src.replace('app.run(host="0.0.0.0", port=5000, debug=True)',
                  'app.run(host="0.0.0.0", port=5001, debug=True)')
p.write_text(src)
print("port set to 5001")
EOF
rm -f scheduler.db*
/home/cody/scheduler/.venv/bin/python - <<'EOF'
import sys
sys.path.insert(0, '.')
import app as a
import mock_seed
a.init_db()                      # create tables (runs real seed, gets wiped next)
n, d, p = mock_seed.seed(a)      # force the fake mock roster + picks
week = a.monday_of(a.date.today()).isoformat()
n_assign = a.run_scheduler(week)  # pre-seeded picks -> filled demo schedule
print(f"mock DB reseeded: {n} fake employees (FOH+BOH), Mon-Sun demo week "
      f"({d} days per house), {p} pre-seeded picks, {n_assign} picks scheduled")
EOF
echo "sync complete — restart the mock server to pick up changes"
