"""ShiftWise top-level application entry point and backward compatibility layer.

Provides the primary WSGI application object 'app' for Gunicorn/Caddy and
re-exports all domain functions, database helpers, and constants so existing
tests, scripts, and deployment configurations continue to work without modification.
"""
import os
import importlib
import smtplib
import sys
import types
import urllib.error
import urllib.parse
import urllib.request
import urllib
from datetime import date, datetime, timedelta
from pathlib import Path

from werkzeug.security import check_password_hash, generate_password_hash

_db_mod = importlib.import_module("shiftwise.db")
from shiftwise.auth import login_required
from shiftwise.db import (
    POSTGRES_SCHEMA,
    SCHEMA,
    database_engine,
    db,
    execute_sql,
    init_db,
    monday_of,
)
from shiftwise.domain.constants import DAYS, MIN_DAYS_OFF
from shiftwise.domain.rules import (
    assignment_block_reason,
    priority_key,
    shift_hours,
    unavailable_uids,
    valid_email,
    valid_phone,
)
from shiftwise.notify import notify, send_schedule_link
from shiftwise.routes.calendar import calendar_days
from shiftwise.scheduler.coverage import apply_sick, coverage_plan
from shiftwise.scheduler.engine import run_scheduler
from shiftwise import app, create_app


# Keep compatibility writes here; delivery code depends only on its own module.
_notification_patch_targets = {
    "smtplib": (importlib.import_module("shiftwise.notify"), "_smtplib"),
    "urllib": (importlib.import_module("shiftwise.notify"), "_urllib"),
    "send_schedule_link": (
        importlib.import_module("shiftwise.routes.roster"), "send_schedule_link"
    ),
}


class _AppModule(types.ModuleType):
    """Propagate legacy database and notification patches to their consumers."""

    def __getattribute__(self, name):
        if name == "DB_PATH":
            return _db_mod.DB_PATH
        return super().__getattribute__(name)

    def __setattr__(self, name, val):
        if name == "DB_PATH":
            _db_mod.DB_PATH = Path(val) if val is not None else val
            self.__dict__.pop("DB_PATH", None)
            return
        if name in _notification_patch_targets:
            module, attribute = _notification_patch_targets[name]
            setattr(module, attribute, val)
        super().__setattr__(name, val)


sys.modules[__name__].__class__ = _AppModule
if "DB_PATH" in sys.modules[__name__].__dict__:
    del sys.modules[__name__].__dict__["DB_PATH"]


__all__ = [
    "create_app",
    "app",
    "db",
    "database_engine",
    "execute_sql",
    "init_db",
    "monday_of",
    "DB_PATH",
    "SCHEMA",
    "POSTGRES_SCHEMA",
    "DAYS",
    "MIN_DAYS_OFF",
    "login_required",
    "notify",
    "send_schedule_link",
    "run_scheduler",
    "coverage_plan",
    "apply_sick",
    "shift_hours",
    "assignment_block_reason",
    "priority_key",
    "unavailable_uids",
    "valid_email",
    "valid_phone",
    "calendar_days",
    "check_password_hash",
    "generate_password_hash",
    "date",
    "datetime",
    "timedelta",
    # Re-exported for the monkeypatch facade contract (AGENTS.md §2):
    # legacy tests may patch app.smtplib.SMTP / app.urllib.request.urlopen.
    "smtplib",
    "urllib",
]


if __name__ == "__main__":
    init_db(seed_demo=os.environ.get("SHIFTWISE_DEMO_SEED") in ("1", "8", "mock"))
    app.run(
        host=os.environ.get("SHIFTWISE_HOST", "127.0.0.1"),
        port=int(os.environ.get("SHIFTWISE_PORT", "5000")),
        debug=False,
    )
