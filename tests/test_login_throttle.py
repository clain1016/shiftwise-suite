"""Sign-in throttling and username-enumeration resistance."""
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import app as appmod
from shiftwise.security import CsrfAwareTestClient


class LoginThrottleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="shiftwise-throttle-")
        appmod.DB_PATH = Path(self.temporary.name) / "scheduler.db"
        appmod.init_db(seed_demo=True)
        appmod.app.test_client_class = CsrfAwareTestClient
        self.client = appmod.app.test_client()

    def tearDown(self):
        self.temporary.cleanup()

    def attempt(self, username, password):
        return self.client.post("/login",
                                data={"username": username, "password": password},
                                follow_redirects=True)

    def expire_lock(self):
        conn = appmod.db()
        conn.execute("UPDATE login_attempts SET locked_until=?",
                     ((datetime.now() - timedelta(seconds=1)).isoformat(),))
        conn.commit()
        conn.close()

    def test_lockout_blocks_even_the_correct_password(self):
        for _ in range(5):
            self.attempt("alex", "wrong-password")
        locked = self.attempt("alex", "alex")
        self.assertIn(b"Too many failed sign-in attempts", locked.data)
        self.assertNotIn(b"Log out", locked.data)

    def test_successful_sign_in_resets_the_counter(self):
        for _ in range(4):
            self.attempt("alex", "wrong-password")
        self.assertIn(b"Log out", self.attempt("alex", "alex").data)
        for _ in range(4):
            self.attempt("alex", "wrong-password")
        self.assertIn(b"Log out", self.attempt("alex", "alex").data)

    def test_unknown_username_is_hashed_anyway(self):
        with patch("shiftwise.auth.check_password_hash",
                   side_effect=appmod.check_password_hash) as verify:
            self.attempt("ghost", "wrong-password")
        self.assertEqual(verify.call_count, 1)

    def test_unknown_username_is_throttled_like_a_real_one(self):
        for _ in range(5):
            self.attempt("ghost", "wrong-password")
        locked = self.attempt("ghost", "ghost")
        self.assertIn(b"Too many failed sign-in attempts", locked.data)

    def test_lockout_expires(self):
        for _ in range(5):
            self.attempt("alex", "wrong-password")
        self.expire_lock()
        self.assertIn(b"Log out", self.attempt("alex", "alex").data)

    def test_lockout_message_does_not_reveal_whether_the_account_exists(self):
        for _ in range(5):
            self.attempt("alex", "wrong-password")
        known = self.attempt("alex", "alex").data
        for _ in range(5):
            self.attempt("ghost", "wrong-password")
        unknown = self.attempt("ghost", "ghost").data
        self.assertIn(b"Too many failed sign-in attempts", known)
        self.assertEqual(known, unknown)
