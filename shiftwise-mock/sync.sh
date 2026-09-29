#!/bin/bash
# Sync the mock scheduler from the real app after every feature addition.
# Copies app.py + templates, fixes the port to 5001, then force-reseeds the
# mock DB with 8 fake employees and a full Mon-Sun demo week — independent
# of whatever demo data the real app seeds.
set -e
cd "$(dirname "$0")"
cp ../scheduler/app.py app.py
cp ../scheduler/templates/*.html templates/
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
n, d, p = mock_seed.seed(a)      # force the 8-employee mock roster + picks
print(f"mock DB reseeded: {n} employees, Mon-Sun demo week ({d} days), "
      f"{p} pre-seeded picks")
EOF
echo "sync complete — restart the mock server to pick up changes"
