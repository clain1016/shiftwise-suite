"""ShiftWise top-level application entry point and backward compatibility layer.

Provides the primary WSGI application object 'app' for Gunicorn/Caddy and
re-exports all domain functions, database helpers, and constants so existing
tests, scripts, and deployment configurations continue to work without modification.
"""
import os
import sys
import types
from datetime import date, datetime, timedelta
from pathlib import Path

from flask import (
    Flask,
    abort,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

import shiftwise.db as _db_mod
from shiftwise.auth import login_required
from shiftwise.db import (
    SCHEMA,
    db,
    init_db,
    monday_of,
)
from shiftwise.domain.constants import DAYS, MIN_DAYS_OFF
from shiftwise.domain.rules import (
    assignment_block_reason,
    priority_key,
    shift_hours,
    unavailable_uids,
)
from shiftwise.notify import notify
from shiftwise.scheduler.coverage import apply_sick, coverage_plan
from shiftwise.scheduler.engine import run_scheduler
from shiftwise import app, create_app


class _AppModule(types.ModuleType):
    """Custom module wrapper to synchronize DB_PATH mutations across packages."""

    @property
    def DB_PATH(self):
        return _db_mod.DB_PATH

    @DB_PATH.setter
    def DB_PATH(self, val):
        p = Path(val) if val is not None else val
        _db_mod.DB_PATH = p


sys.modules[__name__].__class__ = _AppModule
if "DB_PATH" in sys.modules[__name__].__dict__:
    del sys.modules[__name__].__dict__["DB_PATH"]


__all__ = [
    "create_app",
    "app",
    "db",
    "init_db",
    "monday_of",
    "DB_PATH",
    "SCHEMA",
    "DAYS",
    "MIN_DAYS_OFF",
    "login_required",
    "notify",
    "run_scheduler",
    "coverage_plan",
    "apply_sick",
    "shift_hours",
    "assignment_block_reason",
    "priority_key",
    "unavailable_uids",
    "check_password_hash",
    "generate_password_hash",
    "date",
    "datetime",
    "timedelta",
]

if __name__ == "__main__":
    init_db(seed_demo=os.environ.get("SHIFTWISE_DEMO_SEED") in ("1", "8", "mock"))
    app.run(
        host=os.environ.get("SHIFTWISE_HOST", "127.0.0.1"),
        port=int(os.environ.get("SHIFTWISE_PORT", "5000")),
        debug=False,
    )
