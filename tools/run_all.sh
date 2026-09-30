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
echo "--> [1/3] Running Unit & Integration Test Suite (pytest)..."
"${PYTEST}" "${ROOT_DIR}/tests/"

echo ""
echo "--> [2/3] Running Conflict Gauntlet (tools/gauntlet.py)..."
"${PYTHON}" "${ROOT_DIR}/tools/gauntlet.py"

echo ""
echo "--> [3/3] Running Scenario Simulation (tools/scenario_demo.py)..."
"${PYTHON}" "${ROOT_DIR}/tools/scenario_demo.py"

echo ""
echo "=========================================================="
echo " ALL TESTS & SIMULATIONS PASSED (100% GREEN)"
echo "=========================================================="
