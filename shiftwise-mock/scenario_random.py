"""Shim delegating to tools/scenario_random.py."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import scenario_random

if __name__ == "__main__":
    scenario_random.run_random()
