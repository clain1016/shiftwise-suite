"""Shim delegating to tools/gauntlet.py."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import gauntlet

if __name__ == "__main__":
    gauntlet.run_gauntlet()
