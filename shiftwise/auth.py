"""Authentication session helpers, login throttling, and decorators."""
import os
from datetime import datetime, timedelta
from functools import wraps

from flask import abort, redirect, request, session, url_for
from werkzeug.routing import BuildError
from werkzeug.security import check_password_hash, generate_password_hash

from shiftwise.db import db

# Verified whenever the username is unknown, so a missing account costs the
# same as a wrong password (no username enumeration by response time).
_DUMMY_HASH = generate_password_hash("shiftwise-dummy-password")

DEFAULT_MAX_FAILURES = 5
DEFAULT_MAX_IP_FAILURES = 25
DEFAULT_LOCKOUT_SECONDS = 900


def _int_env(name, default):
    try:
        return max(0, int(os.environ.get(name, "")))
    except ValueError:
        return default


def max_failures():
    """Failures allowed for one username before it is locked out."""
    return _int_env("SHIFTWISE_LOGIN_MAX_FAILURES", DEFAULT_MAX_FAILURES)


def max_ip_failures():
    """Failures allowed from one client address (catches username spraying)."""
    return _int_env("SHIFTWISE_LOGIN_IP_MAX_FAILURES", DEFAULT_MAX_IP_FAILURES)


def lockout_seconds():
    """How long a locked identity stays locked."""
    return _int_env("SHIFTWISE_LOGIN_LOCKOUT_SECONDS", DEFAULT_LOCKOUT_SECONDS)


def client_ip():
    """Client address, for the per-address failure counter."""
    return request.remote_addr or "unknown"


def _limits(username, ip):
    return {
        f"user:{username.strip().lower()}": max_failures(),
        f"ip:{ip}": max_ip_failures(),
    }


def lockout_remaining(conn, username, ip):
    """Seconds before this username/address may try again; 0 when unlocked."""
    limits = _limits(username, ip)
    keys = list(limits)
    placeholders = ",".join("?" * len(keys))
    now = datetime.now()
    remaining = 0.0
    for row in conn.execute(
            f"SELECT key, locked_until FROM login_attempts WHERE key IN ({placeholders})",
            keys):
        if not row["locked_until"] or not limits[row["key"]]:
            continue
        remaining = max(
            remaining,
            (datetime.fromisoformat(row["locked_until"]) - now).total_seconds())
    return remaining


def note_login_failure(conn, username, ip):
    """Count a failed attempt and lock the identity once past its limit."""
    locked_until = (datetime.now() + timedelta(seconds=lockout_seconds())).isoformat()
    for key, limit in _limits(username, ip).items():
        row = conn.execute("SELECT failures FROM login_attempts WHERE key=?",
                           (key,)).fetchone()
        failures = (row["failures"] if row else 0) + 1
        if limit and failures >= limit:
            conn.execute(
                "INSERT INTO login_attempts (key, failures, locked_until) VALUES (?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET failures=excluded.failures, "
                "locked_until=excluded.locked_until",
                (key, failures, locked_until))
        else:
            conn.execute(
                "INSERT INTO login_attempts (key, failures) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET failures=excluded.failures",
                (key, failures))


def clear_login_failures(conn, username, ip):
    """Forget the failure history for a successful sign-in."""
    keys = list(_limits(username, ip))
    placeholders = ",".join("?" * len(keys))
    conn.execute(f"DELETE FROM login_attempts WHERE key IN ({placeholders})", keys)


def verify_credentials(conn, username, password):
    """Return the matching user row, doing the same work for unknown users."""
    user = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    if user:
        return user if check_password_hash(user["password"], password) else None
    check_password_hash(_DUMMY_HASH, password)
    return None


def _login_url():
    """Sign-in URL, tolerating either the blueprint or the aliased endpoint."""
    for endpoint in ("auth.login", "login"):
        try:
            return url_for(endpoint)
        except BuildError:
            continue
    return "/login"


def login_required(role=None):
    """Decorator requiring an authenticated session and optional matching role."""
    def deco(f):
        @wraps(f)
        def wrapper(*a, **kw):
            if "uid" not in session:
                return redirect(_login_url())
            conn = db()
            user = conn.execute("SELECT role FROM users WHERE id=?",
                                (session["uid"],)).fetchone()
            conn.close()
            if not user:
                session.clear()
                return redirect(_login_url())
            if role and (user["role"] != role or session.get("role") != role):
                abort(403)
            return f(*a, **kw)
        return wrapper
    return deco
