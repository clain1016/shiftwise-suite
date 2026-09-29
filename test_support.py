"""Shared database isolation for the script-style tests."""
import tempfile
from pathlib import Path


def isolate_database(appmod):
    temporary = tempfile.TemporaryDirectory(prefix="shiftwise-test-")
    appmod.DB_PATH = Path(temporary.name) / "scheduler.db"
    return temporary
