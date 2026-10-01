#!/usr/bin/env python3
"""Guard against non-runnable entrypoints.

Regression class this catches: a shell script loses its executable bit
(100755 -> 100644) in a commit, so the documented invocation
(``./tools/run_all.sh``) fails with "Permission denied" even though every
test still passes.  PR #18 shipped exactly that.

Checks (metadata comes from the git index, which is what CI checks out):

1. every tracked ``*.sh`` file has mode 100755 and a ``#!`` shebang;
2. every ``./<path>.sh`` referenced by tracked markdown resolves to a
   tracked file and is executable in the working tree (the form a user
   actually types).

Stdlib only, no dev extras, sub-second.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOC_REF = re.compile(r"(?<![\w/])\./([A-Za-z0-9_][A-Za-z0-9_./-]*\.sh)")

EXEC_MODE = "100755"


def git(*args: str) -> str:
    result = subprocess.run(
        ("git", *args), cwd=ROOT, capture_output=True, text=True
    )
    if result.returncode != 0:
        sys.exit(f"check_entrypoints: git {' '.join(args)} failed:\n{result.stderr}")
    return result.stdout


def index_entries() -> dict[str, str]:
    """Map tracked path -> index mode."""
    entries: dict[str, str] = {}
    for line in git("ls-files", "-s", "-z").split("\0"):
        if not line:
            continue
        meta, path = line.split("\t", 1)
        entries[path] = meta.split()[0]
    return entries


def shebang_ok(path: str) -> bool:
    try:
        with open(os.path.join(ROOT, path), "rb") as handle:
            return handle.readline().startswith(b"#!")
    except OSError:
        return False


def main() -> int:
    modes = index_entries()
    problems: list[str] = []

    scripts = sorted(p for p in modes if p.endswith(".sh"))
    for path in scripts:
        if modes[path] != EXEC_MODE:
            problems.append(
                f"tracked shell script is mode {modes[path]}, expected {EXEC_MODE}: {path}"
            )
        elif not shebang_ok(path):
            problems.append(f"tracked shell script has no '#!' shebang: {path}")

    references: dict[str, set[str]] = {}
    for doc in sorted(p for p in modes if p.endswith(".md")):
        try:
            with open(os.path.join(ROOT, doc), encoding="utf-8") as handle:
                text = handle.read()
        except OSError:
            continue
        for match in DOC_REF.finditer(text):
            references.setdefault(match.group(1), set()).add(doc)

    for ref, docs in sorted(references.items()):
        where = ", ".join(sorted(docs))
        if ref not in modes:
            problems.append(f"documented entrypoint does not exist: ./{ref} (in {where})")
        elif not os.access(os.path.join(ROOT, ref), os.X_OK):
            problems.append(f"documented entrypoint is not executable: ./{ref} (in {where})")

    if problems:
        print("check_entrypoints: FAILED", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    print(
        f"check_entrypoints: OK ({len(scripts)} tracked shell script(s), "
        f"{len(references)} documented reference(s))"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
