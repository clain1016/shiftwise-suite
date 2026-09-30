"""ShiftWise Mock Seeding (Re-exported from canonical mock_seed.py)."""
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from mock_seed import *
from mock_seed import ROSTER, DEMO_SHIFTS, PREFS, monday_of, seed
