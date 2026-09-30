import tempfile
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def isolate_database(appmod):
    temporary = tempfile.TemporaryDirectory(prefix="shiftwise-test-")
    appmod.DB_PATH = Path(temporary.name) / "scheduler.db"
    return temporary
