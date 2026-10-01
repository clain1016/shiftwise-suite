#!/usr/bin/env bash
# Remove the transient artifacts listed in AGENTS.md section 3.
#
#   ./tools/clean_artifacts.sh                 databases, __pycache__, *.pyc,
#                                              .pytest_cache
#   ./tools/clean_artifacts.sh --liveweek-artifacts
#                                              also remove tools/liveweek-artifacts/
#                                              (kept by default: AGENTS.md section 4
#                                              treats those findings as reports for
#                                              fixing agents)
#   ./tools/clean_artifacts.sh --quiet         suppress output
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

QUIET=0
REMOVE_FINDINGS=0
for arg in "$@"; do
    case "${arg}" in
        --liveweek-artifacts) REMOVE_FINDINGS=1 ;;
        --quiet) QUIET=1 ;;
        -h|--help) sed -n '2,10p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) echo "clean_artifacts: unknown option: ${arg}" >&2; exit 2 ;;
    esac
done

say() { [ "${QUIET}" -eq 1 ] || printf '%s\n' "$*"; }

count=0

# Databases and the pytest cache.  nullglob drops unmatched *globs*, but the
# literal names below always reach the loop, so existence is tested explicitly
# to keep the report (and the count) honest.
shopt -s nullglob
for path in app.db test_*.db shiftwise-mock/mock.db tools/*.db tools/*.db-journal .pytest_cache; do
    [ -e "${path}" ] || [ -L "${path}" ] || continue
    rm -rf -- "${path}"
    say "removed ${path}"
    count=$((count + 1))
done

# __pycache__ trees and stray bytecode, never inside .venv/.
pycache=$(find . -path ./.venv -prune -o -type d -name '__pycache__' -print | wc -l)
find . -path ./.venv -prune -o -type d -name '__pycache__' -exec rm -rf {} + 2>/dev/null || true
find . -path ./.venv -prune -o -type f -name '*.pyc' -exec rm -f {} + 2>/dev/null || true
if [ "${pycache}" -gt 0 ]; then
    say "removed ${pycache} __pycache__ tree(s)"
    count=$((count + pycache))
fi

if [ "${REMOVE_FINDINGS}" -eq 1 ] && [ -e tools/liveweek-artifacts ]; then
    rm -rf -- tools/liveweek-artifacts
    say "removed tools/liveweek-artifacts"
    count=$((count + 1))
fi

say "clean_artifacts: done (${count} artifact path(s) removed)"
