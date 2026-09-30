"""CSRF enforcement for the hand-rolled HTML forms (shiftwise/security.py)."""
import re
import sys
import tempfile
import unittest
from pathlib import Path

from flask.testing import FlaskClient

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import app as appmod
import mock_seed
from shiftwise.security import CSRF_FIELD, CSRF_HEADER, CsrfAwareTestClient

FORM_RE = re.compile(r"<form\b[^>]*>.*?</form>", re.S)


class CsrfTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="shiftwise-csrf-")
        appmod.DB_PATH = Path(self.temporary.name) / "scheduler.db"
        appmod.init_db(seed_demo=True)
        mock_seed.seed(appmod)
        appmod.run_scheduler(appmod.monday_of(appmod.date.today()).isoformat())
        appmod.app.test_client_class = CsrfAwareTestClient
        # `self.client` behaves like the tools/tests clients; `self.plain` is
        # a bare client, so it has to supply the token itself.
        self.client = appmod.app.test_client()
        self.plain = FlaskClient(appmod.app, appmod.app.response_class)

    def tearDown(self):
        self.temporary.cleanup()

    def login(self, client, username):
        return client.post("/login",
                           data={"username": username, "password": username})

    def token_of(self, client):
        client.get("/login")
        with client.session_transaction() as flask_session:
            return flask_session[CSRF_FIELD]

    def assert_forms_are_protected(self, client, path):
        html = client.get(path).data.decode()
        protected = 0
        for form in FORM_RE.findall(html):
            if 'method="post"' not in form[:form.index(">") + 1].lower() \
                    and 'formmethod="post"' not in form.lower():
                continue
            self.assertIn(CSRF_FIELD, form,
                          f"{path} renders a POST form without a CSRF field")
            protected += 1
        return protected

    def test_post_without_a_token_is_rejected(self):
        response = self.plain.post("/login", data={"username": "manager",
                                                   "password": "manager"})
        self.assertEqual(response.status_code, 400)
        with self.plain.session_transaction() as flask_session:
            self.assertNotIn("uid", flask_session)

    def test_forged_token_is_rejected(self):
        self.plain.get("/login")
        response = self.plain.post("/login", data={"username": "manager",
                                                   "password": "manager",
                                                   CSRF_FIELD: "forged"})
        self.assertEqual(response.status_code, 400)

    def test_token_from_another_session_is_rejected(self):
        stolen = self.token_of(FlaskClient(appmod.app, appmod.app.response_class))
        self.plain.get("/login")
        response = self.plain.post("/login", data={"username": "manager",
                                                   "password": "manager",
                                                   CSRF_FIELD: stolen})
        self.assertEqual(response.status_code, 400)

    def test_header_token_is_accepted(self):
        response = self.plain.post(
            "/login", data={"username": "manager", "password": "manager"},
            headers={CSRF_HEADER: self.token_of(self.plain)})
        self.assertEqual(response.status_code, 302)

    def test_get_requests_need_no_token(self):
        self.assertEqual(self.plain.get("/healthz").status_code, 200)
        self.assertEqual(self.plain.get("/login").status_code, 200)

    def test_every_rendered_post_form_carries_a_token(self):
        self.login(self.client, "manager")
        conn = appmod.db()
        conn.execute(
            "INSERT INTO requests (user_id, kind, day, week_start, created_at) "
            "VALUES ((SELECT id FROM users WHERE username='alex'),'day_off','Fri',?,?)",
            (appmod.monday_of(appmod.date.today()).isoformat(), "2026-01-01T00:00:00"))
        conn.commit()
        conn.close()

        protected = sum(self.assert_forms_are_protected(self.client, path) for path in (
            "/manager", "/manager/requests", "/manager/roster",
            "/manager/conflicts", "/account/password"))
        # the manager pages alone render the add-shift, delete-shift,
        # approve/deny, roster and override forms
        self.assertGreater(protected, 10)

        employee = appmod.app.test_client()
        self.login(employee, "alex")
        dashboard = employee.get("/").data.decode()
        self.assertIn("switch-form", dashboard)
        self.assertGreater(self.assert_forms_are_protected(employee, "/"), 0)

    def test_employee_swap_forms_carry_a_token(self):
        """The self-service swap form and the swap-response form are protected."""
        week = appmod.monday_of(appmod.date.today()).isoformat()
        conn = appmod.db()
        users = {
            row["username"]: row["id"] for row in conn.execute(
                "SELECT id, username FROM users WHERE username IN ('alex','sam')")
        }
        shift_ids = []
        for start, end in (("01:00", "03:00"), ("03:00", "05:00")):
            cursor = conn.execute(
                "INSERT INTO shifts (week_start, day, start_time, end_time, slots, area) "
                "VALUES (?, 'Mon', ?, ?, 1, 'front')", (week, start, end))
            shift_ids.append(cursor.lastrowid)
        conn.executemany(
            "INSERT INTO assignments (shift_id, user_id, status) VALUES (?, ?, 'notified')",
            [(shift_ids[0], users["alex"]), (shift_ids[1], users["sam"])])
        # an employee-directed swap invitation for alex, so that /my-requests
        # renders the accept/decline form
        conn.execute(
            "INSERT INTO requests (user_id, kind, shift_id, target_shift_id, target_user_id, "
            "status, created_at) VALUES (?, 'swap', ?, ?, ?, 'approved', ?)",
            (users["sam"], shift_ids[1], shift_ids[0], users["alex"], "2026-01-01T00:00:00"))
        conn.commit()
        conn.close()

        employee = appmod.app.test_client()
        self.login(employee, "alex")
        dashboard = employee.get("/").data.decode()
        self.assertIn("request/swap", dashboard, "the swap-request form did not render")
        self.assertGreater(self.assert_forms_are_protected(employee, "/"), 0)

        history = employee.get("/my-requests").data.decode()
        self.assertIn("/respond", history, "the swap-response form did not render")
        self.assertGreater(self.assert_forms_are_protected(employee, "/my-requests"), 0)
