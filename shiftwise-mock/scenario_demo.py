"""Shim delegating to tools/scenario_demo.py."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import scenario_demo

if __name__ == "__main__":
    scenario_demo.run_demo()
