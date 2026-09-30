#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

PYTHON="${ROOT_DIR}/.venv/bin/python"
PYTEST="${ROOT_DIR}/.venv/bin/pytest"

if [[ ! -x "${PYTHON}" ]]; then
    PYTHON="python3"
fi

if [[ ! -x "${PYTEST}" ]]; then
    PYTEST="pytest"
fi

echo "=========================================================="
echo " ShiftWise Suite: Full Test & Verification Suite"
echo "=========================================================="

echo ""
echo "--> [1/5] Running Unit & Integration Test Suite (pytest)..."
"${PYTEST}" "${ROOT_DIR}/tests/"

echo ""
echo "--> [2/5] Running Conflict Gauntlet (tools/gauntlet.py)..."
"${PYTHON}" "${ROOT_DIR}/tools/gauntlet.py"

echo ""
echo "--> [3/5] Running Scenario Simulation (tools/scenario_demo.py)..."
"${PYTHON}" "${ROOT_DIR}/tools/scenario_demo.py"

echo ""
echo "--> [4/5] Running Live-Week Concurrency Gauntlet (tools/liveweek.py)..."
"${PYTHON}" "${ROOT_DIR}/tools/liveweek.py" --duration 60

echo ""
echo "--> [5/5] Validating Docker Compose configuration..."
if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    (cd "${ROOT_DIR}" && docker compose config >/dev/null)
    echo "    docker compose config: OK"
else
    echo "    SKIPPED: docker compose is not available in this environment"
fi

echo ""
echo "=========================================================="
echo " ALL TESTS & SIMULATIONS PASSED (100% GREEN)"
echo "=========================================================="
