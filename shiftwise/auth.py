"""Authentication session helpers and decorators."""
from functools import wraps

from flask import abort, redirect, session, url_for

from shiftwise.db import db


def login_required(role=None):
    """Decorator requiring an authenticated session and optional matching role."""
    def deco(f):
        @wraps(f)
        def wrapper(*a, **kw):
            if "uid" not in session:
                return redirect(url_for("auth.login") if "auth.login" in str(url_for("login")) else url_for("login"))
            conn = db()
            user = conn.execute("SELECT role FROM users WHERE id=?",
                                (session["uid"],)).fetchone()
            conn.close()
            if not user:
                session.clear()
                return redirect(url_for("auth.login") if "auth.login" in str(url_for("login")) else url_for("login"))
            if role and (user["role"] != role or session.get("role") != role):
                abort(403)
            return f(*a, **kw)
        return wrapper
    return deco
