#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

# Tiered verification runner.
#
#   ./tools/run_all.sh                       full gate (required before opening a PR
#                                            or merging: AGENTS.md section 4)
#   ./tools/run_all.sh --fast                 unit + integration suite only; use this
#                                            while writing code instead of the full gate
#   ./tools/run_all.sh --no-liveweek          full gate minus the live-week gauntlet
#   ./tools/run_all.sh --liveweek-seconds N   override the live-week duration (default 60)
#
# SHIFTWISE_LIVEWEEK_SECONDS also overrides the live-week duration.

MODE="full"
RUN_LIVEWEEK=1
LIVEWEEK_SECONDS="${SHIFTWISE_LIVEWEEK_SECONDS:-60}"
usage() {
    cat <<'EOF'
Usage: tools/run_all.sh [options]

  (no options)              full gate: pytest + gauntlet + scenario + chaos
                                + liveweek + compose
  --fast                    pytest suite only (inner development loop)
  --no-liveweek             full gate without the live-week concurrency gauntlet
  --liveweek-seconds N      override the live-week duration (default 60;
                            SHIFTWISE_LIVEWEEK_SECONDS also works)
  -h, --help                show this message
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --fast) MODE="fast" ;;
        --no-liveweek) RUN_LIVEWEEK=0 ;;
        --liveweek-seconds)
            LIVEWEEK_SECONDS="${2:?--liveweek-seconds requires a value}"
            shift
            ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

if ! [[ "${LIVEWEEK_SECONDS}" =~ ^[0-9]+$ ]]; then
    echo "Error: live-week duration must be a non-negative integer seconds (got '${LIVEWEEK_SECONDS}')" >&2
    exit 2
fi

CLEAN="${ROOT_DIR}/tools/clean_artifacts.sh"

# Start from a clean artifact baseline, and clean again on exit -- including
# aborted runs -- so a killed gate cannot leave test databases or caches behind.
# tools/liveweek-artifacts/ is preserved: AGENTS.md section 4 keeps those
# findings for fixing agents.
"${CLEAN}" --quiet
trap '"${CLEAN}" --quiet || true' EXIT

PYTHON="${ROOT_DIR}/.venv/bin/python"
PYTEST="${ROOT_DIR}/.venv/bin/pytest"

if [[ ! -x "${PYTHON}" ]]; then
    PYTHON="python3"
fi

if [[ ! -x "${PYTEST}" ]]; then
    PYTEST="pytest"
fi

echo "=========================================================="
echo " ShiftWise Suite: Test & Verification Suite (${MODE})"
echo "=========================================================="

echo "--> [0] Checking entrypoint executability (tools/check_entrypoints.py)..."
"${PYTHON}" "${ROOT_DIR}/tools/check_entrypoints.py"

echo ""
if [[ "${MODE}" == "fast" ]]; then
    echo "--> FAST MODE: unit & integration suite only."
    echo "    The full gate must still pass before a PR or merge: ./tools/run_all.sh"
    echo ""
    "${PYTEST}" "${ROOT_DIR}/tests/"
    echo ""
    echo "FAST MODE OK"
    exit 0
fi

if ! "${PYTHON}" -c "import pglast, pytest_timeout" >/dev/null 2>&1; then
    echo "    NOTE: dev extras missing (pglast/pytest-timeout)."
    echo "          Install with: .venv/bin/pip install -r requirements-dev.txt"
    echo "          Without pglast the PostgreSQL DDL parser test is skipped."
    echo ""
fi

STEP=0
TOTAL=$((5 + RUN_LIVEWEEK))

STEP=$((STEP + 1))
echo "--> [${STEP}/${TOTAL}] Running Unit & Integration Test Suite (pytest)..."
"${PYTEST}" "${ROOT_DIR}/tests/"

echo ""
STEP=$((STEP + 1))
echo "--> [${STEP}/${TOTAL}] Running Conflict Gauntlet (tools/gauntlet.py)..."
"${PYTHON}" "${ROOT_DIR}/tools/gauntlet.py"

echo ""
STEP=$((STEP + 1))
echo "--> [${STEP}/${TOTAL}] Running Scenario Simulation (tools/scenario_demo.py)..."
"${PYTHON}" "${ROOT_DIR}/tools/scenario_demo.py"

echo ""
STEP=$((STEP + 1))
echo "--> [${STEP}/${TOTAL}] Running Chaos Simulation (tools/scenario_random.py, pinned seed)..."
# Pinned seed keeps the gate deterministic; re-run manually with other seeds
# (tools/scenario_random.py <seed>) for wider exploration.
"${PYTHON}" "${ROOT_DIR}/tools/scenario_random.py" 42

if [[ "${RUN_LIVEWEEK}" -eq 1 ]]; then
    echo ""
    STEP=$((STEP + 1))
    echo "--> [${STEP}/${TOTAL}] Running Live-Week Concurrency Gauntlet (tools/liveweek.py, ${LIVEWEEK_SECONDS}s)..."
    "${PYTHON}" "${ROOT_DIR}/tools/liveweek.py" --duration "${LIVEWEEK_SECONDS}"
else
    echo ""
    echo "--> SKIPPED: live-week concurrency gauntlet (--no-liveweek)."
    echo "    Run the full gate before a PR or merge: ./tools/run_all.sh"
fi

echo ""
STEP=$((STEP + 1))
echo "--> [${STEP}/${TOTAL}] Validating Docker Compose configuration..."
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
