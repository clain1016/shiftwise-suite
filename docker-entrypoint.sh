#!/usr/bin/env bash
set -euo pipefail

# Ensure persistent database directory exists
if [[ -n "${SHIFTWISE_DB_PATH:-}" ]]; then
  mkdir -p "$(dirname "${SHIFTWISE_DB_PATH}")"
fi

# Fail fast on a missing or placeholder session secret: without a stable
# SHIFTWISE_SECRET_KEY every container restart invalidates all sessions.
if [[ -z "${SHIFTWISE_SECRET_KEY:-}" || "${SHIFTWISE_SECRET_KEY}" == "replace_with_32_byte_hex_token" ]]; then
  echo "ERROR: set a stable SHIFTWISE_SECRET_KEY (32-byte hex) in your .env" >&2
  exit 1
fi

# Run database schema initialization and migrations prior to starting workers.
# Demo seeding (8-employee mock roster) is strictly opt-in: set
# SHIFTWISE_DEMO_SEED=1 for a demo; production boots with an empty database
# and requires SHIFTWISE_BOOTSTRAP_MANAGER_PASSWORD on first start.
if [[ "${SHIFTWISE_DEMO_SEED:-0}" == "1" || "${SHIFTWISE_DEMO_SEED:-0}" == "8" || "${SHIFTWISE_DEMO_SEED:-0}" == "mock" ]]; then
  python3 -c "import app, mock_seed; app.init_db(mock_roster=True); app.run_scheduler(app.monday_of(app.date.today()).isoformat())"
else
  python3 -c "import app; app.init_db()"
fi


exec "$@"
