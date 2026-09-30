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

_db_mod = importlib.import_module("shiftwise.db")
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
    valid_email,
    valid_phone,
)
from shiftwise.notify import notify, send_schedule_link
from shiftwise.routes.calendar import calendar_days
from shiftwise.scheduler.coverage import apply_sick, coverage_plan
from shiftwise.scheduler.engine import run_scheduler
from shiftwise import app, create_app


class _AppModule(types.ModuleType):
    """Custom module wrapper to synchronize DB_PATH mutations across packages."""

    def __getattribute__(self, name):
        if name == "DB_PATH":
            return _db_mod.DB_PATH
        return super().__getattribute__(name)

    def __setattr__(self, name, val):
        if name == "DB_PATH":
            _db_mod.DB_PATH = Path(val) if val is not None else val
            self.__dict__.pop("DB_PATH", None)
            return
        super().__setattr__(name, val)


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
]


if __name__ == "__main__":
    init_db(seed_demo=os.environ.get("SHIFTWISE_DEMO_SEED") in ("1", "8", "mock"))
    app.run(
        host=os.environ.get("SHIFTWISE_HOST", "127.0.0.1"),
        port=int(os.environ.get("SHIFTWISE_PORT", "5000")),
        debug=False,
    )
