"""ShiftWise application configuration."""
import os
import secrets
import warnings
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent

DEFAULT_DB_PATH = ROOT_DIR / "scheduler.db"


class Config:
    """Base application configuration."""

    # SQLite Database Path
    DB_PATH = Path(os.environ.get("SHIFTWISE_DB_PATH", DEFAULT_DB_PATH))

    # Flask Secret Key
    SECRET_KEY = os.environ.get("SHIFTWISE_SECRET_KEY")
    if not SECRET_KEY:
        warnings.warn(
            "SHIFTWISE_SECRET_KEY is not set — using an ephemeral session "
            "secret. All sessions are invalidated on every restart. Set a "
            "stable SHIFTWISE_SECRET_KEY for any real deployment."
        )
        SECRET_KEY = secrets.token_hex(32)

    # Proxy headers
    BEHIND_PROXY = os.environ.get("SHIFTWISE_BEHIND_PROXY", "").lower() in ("1", "true", "yes")

    # Session cookie security
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = os.environ.get("SHIFTWISE_SESSION_COOKIE_SAMESITE", "Lax")
    SESSION_COOKIE_SECURE = os.environ.get(
        "SHIFTWISE_SESSION_COOKIE_SECURE", ""
    ).lower() in ("1", "true", "yes")

    # Bootstrap & Seeding
    BOOTSTRAP_MANAGER_PASSWORD = os.environ.get("SHIFTWISE_BOOTSTRAP_MANAGER_PASSWORD")
    DEMO_SEED = os.environ.get("SHIFTWISE_DEMO_SEED")
