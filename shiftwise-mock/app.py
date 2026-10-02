"""ShiftWise Mock Launcher & Compatibility Shim.

Runs the ShiftWise application configured for LAN testing with mock data.
Listens on 0.0.0.0:5001 with an isolated mock database.
Also re-exports root app symbols for backward compatibility with existing scripts.
"""

import os
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import app as shiftwise_app

# Re-export all public attributes for full backward compatibility
for _attr in dir(shiftwise_app):
    if not _attr.startswith("__"):
        globals()[_attr] = getattr(shiftwise_app, _attr)

DEFAULT_DB = str(Path(__file__).resolve().parent / "mock.db")

if __name__ == "__main__":
    os.environ.setdefault("SHIFTWISE_HOST", "0.0.0.0")
    os.environ.setdefault("SHIFTWISE_PORT", "5001")
    os.environ.setdefault("SHIFTWISE_DB_PATH", DEFAULT_DB)
    os.environ.setdefault("SHIFTWISE_BOOTSTRAP_MANAGER_PASSWORD", "testbootstrap123")
    shiftwise_app.DB_PATH = Path(os.environ["SHIFTWISE_DB_PATH"])
    shiftwise_app.init_db(seed_demo=True)
    shiftwise_app.app.run(
        host=os.environ.get("SHIFTWISE_HOST", "0.0.0.0"),
        port=int(os.environ.get("SHIFTWISE_PORT", "5001")),
        debug=False,
    )
