#!/usr/bin/env python3
"""Assert the tracked shell entrypoints stay executable.

Regression class: PR #18 dropped run_all.sh's 100755 mode, so the documented
``./tools/run_all.sh`` failed with "Permission denied" while every test still
passed.

Reads metadata from the git index (``git ls-files -s``) -- that is what a
checkout materialises -- so there are no filesystem or platform assumptions.
Add any newly tracked, directly-invoked shell entrypoint to ENTRYPOINTS.
"""

from __future__ import annotations

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENTRYPOINTS = (
    "docker-entrypoint.sh",
    "shiftwise-mock/sync.sh",
    "tools/run_all.sh",
)
EXEC_MODE = "100755"


def main() -> int:
    problems: list[str] = []
    for path in ENTRYPOINTS:
        result = subprocess.run(
            ("git", "-C", ROOT, "ls-files", "-s", "--", path),
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            sys.exit(f"check_entrypoints: git ls-files failed:\n{result.stderr}")
        entry = result.stdout.strip()
        if not entry:
            problems.append(f"not tracked by git: {path}")
            continue
        mode = entry.split(None, 1)[0]
        if mode != EXEC_MODE:
            problems.append(f"{path} is mode {mode}, expected {EXEC_MODE}")

    if problems:
        print("check_entrypoints: FAILED", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    print(f"check_entrypoints: OK ({len(ENTRYPOINTS)} entrypoints executable)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
