"""CSRF protection for ShiftWise's plain HTML forms.

ShiftWise renders every form by hand, so the token is a hand-rolled
double-submit value: a random per-session token is embedded in each rendered
POST form, and every unsafe request must echo it back in the ``_csrf`` form
field or the ``X-CSRF-Token`` header. The session cookie's ``SameSite`` policy
(``shiftwise/config.py``) stays as a second line of defence — it is not a
substitute for the token, because operators may turn it off.
"""

import hmac
import secrets

from flask import abort, request, session
from flask.testing import FlaskClient

CSRF_FIELD = "_csrf"
CSRF_HEADER = "X-CSRF-Token"
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def ensure_token():
    """Return this session's CSRF token, creating one on first use."""
    token = session.get(CSRF_FIELD)
    if not token:
        token = secrets.token_urlsafe(32)
        session[CSRF_FIELD] = token
    return token


def token_is_valid(supplied):
    """Constant-time comparison of a submitted token against the session's."""
    expected = session.get(CSRF_FIELD)
    if not expected or not supplied:
        return False
    return hmac.compare_digest(expected, supplied)


def _supplied_token():
    return request.form.get(CSRF_FIELD) or request.headers.get(CSRF_HEADER)


def init_app(flask_app):
    """Install CSRF enforcement and expose the token to Jinja templates."""
    flask_app.jinja_env.globals["csrf_token"] = ensure_token

    @flask_app.before_request
    def _enforce_csrf():
        ensure_token()
        if request.method in UNSAFE_METHODS and not token_is_valid(_supplied_token()):
            abort(400, description="Missing or invalid CSRF token.")


class CsrfAwareTestClient(FlaskClient):
    """Test/tooling client: attaches a valid token to unsafe requests.

    Used by ``tests/`` and the ``tools/`` harnesses so they exercise the real
    check instead of turning it off — a form that forgets the token still
    fails, which is what ``tests/test_csrf.py`` covers.
    """

    def csrf_token(self):
        with self.session_transaction() as client_session:
            return client_session.setdefault(CSRF_FIELD, secrets.token_urlsafe(32))

    def open(self, *args, **kwargs):
        method = kwargs.get("method") or (args[1] if len(args) > 1 else "GET")
        if str(method).upper() in UNSAFE_METHODS:
            headers = dict(kwargs.get("headers") or {})
            headers.setdefault(CSRF_HEADER, self.csrf_token())
            kwargs["headers"] = headers
        return super().open(*args, **kwargs)


def use_csrf_aware_test_client(flask_app):
    """Make ``flask_app.test_client()`` return the CSRF-aware client."""
    flask_app.test_client_class = CsrfAwareTestClient
    return flask_app
