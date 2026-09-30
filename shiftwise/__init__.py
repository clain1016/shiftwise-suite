"""ShiftWise scheduling application package."""
from pathlib import Path

from flask import Flask

from shiftwise.auth import login_required
from shiftwise.config import Config
from shiftwise.db import DB_PATH, SCHEMA, init_db, monday_of
from shiftwise.domain.constants import DAYS, MIN_DAYS_OFF
from shiftwise.domain.rules import (
    assignment_block_reason,
    priority_key,
    shift_hours,
    unavailable_uids,
)
from shiftwise.notify import notify
from shiftwise.routes import register_blueprints
from shiftwise.scheduler.coverage import apply_sick, coverage_plan
from shiftwise.security import init_app as init_csrf
from shiftwise.scheduler.engine import run_scheduler

ROOT_DIR = Path(__file__).resolve().parent.parent


def create_app(config_class=Config):
    """Application factory for ShiftWise."""
    flask_app = Flask(
        __name__,
        template_folder=str(ROOT_DIR / "templates"),
        static_folder=str(ROOT_DIR / "static"),
    )
    flask_app.secret_key = config_class.SECRET_KEY

    if config_class.BEHIND_PROXY:
        from werkzeug.middleware.proxy_fix import ProxyFix
        flask_app.wsgi_app = ProxyFix(
            flask_app.wsgi_app,
            x_for=1,
            x_proto=1,
            x_host=1,
            x_prefix=1,
        )

    flask_app.config["SESSION_COOKIE_HTTPONLY"] = config_class.SESSION_COOKIE_HTTPONLY
    flask_app.config["SESSION_COOKIE_SAMESITE"] = config_class.SESSION_COOKIE_SAMESITE
    if config_class.SESSION_COOKIE_SECURE:
        flask_app.config["SESSION_COOKIE_SECURE"] = True

    init_csrf(flask_app)
    register_blueprints(flask_app)
    return flask_app


# Default application instance for WSGI servers and tests
app = create_app()

__all__ = [
    "create_app",
    "app",
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
]
