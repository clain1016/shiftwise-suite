"""Pytest configuration and shared fixtures for ShiftWise test suite."""
import sys
from pathlib import Path
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import app as appmod
from test_support import isolate_database
from shiftwise.security import use_csrf_aware_test_client

# Test clients attach a valid CSRF token to unsafe requests, so the suite
# exercises the real check (see tests/test_csrf.py) instead of disabling it.
use_csrf_aware_test_client(appmod.app)


@pytest.fixture
def isolated_db():
    """Provides a fresh isolated database for testing."""
    temp_dir = isolate_database(appmod)
    yield appmod
    temp_dir.cleanup()
