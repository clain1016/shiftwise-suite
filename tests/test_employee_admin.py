import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import app as appmod


class EmployeeAdminTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="shiftwise-employee-admin-")
        appmod.DB_PATH = Path(self.temporary.name) / "scheduler.db"
        appmod.init_db(seed_demo=True)
        self.client = appmod.app.test_client()
        self.week = appmod.monday_of(date.today()).isoformat()

    def tearDown(self):
        self.temporary.cleanup()

    def login(self, username):
        self.client.get("/logout")
        response = self.client.post("/login", data={
            "username": username, "password": username}, follow_redirects=True)
        self.assertIn(b"Log out", response.data)
        return response

    def employee_id(self, username="alex"):
        conn = appmod.db()
        row = conn.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
        conn.close()
        return row["id"]

    def test_manager_can_reset_employee_password_without_revealing_old_password(self):
        self.login("manager")
        response = self.client.post(
            f"/manager/roster/{self.employee_id()}/password",
            data={"password": "fresh-private-passphrase",
                  "confirm_password": "fresh-private-passphrase"}, follow_redirects=True)
        self.assertIn(b"password reset", response.data.lower())
        self.assertNotIn(b"fresh-private-passphrase", response.data)
        conn = appmod.db()
        stored = conn.execute("SELECT password FROM users WHERE username='alex'").fetchone()[0]
        conn.close()
        self.assertNotEqual(stored, "fresh-private-passphrase")
        self.assertTrue(appmod.check_password_hash(stored, "fresh-private-passphrase"))
        self.client.post("/logout")
        self.assertIn(b"Log out", self.client.post("/login", data={
            "username": "alex", "password": "fresh-private-passphrase"},
            follow_redirects=True).data)

    def test_password_reset_rejects_short_password(self):
        self.login("manager")
        response = self.client.post(
            f"/manager/roster/{self.employee_id()}/password",
            data={"password": "short"}, follow_redirects=True)
        self.assertIn(b"at least 12 characters", response.data)
        self.client.post("/logout")
        self.assertIn(b"Log out", self.client.post("/login", data={
            "username": "alex", "password": "alex"}, follow_redirects=True).data)

    def test_password_reset_rejects_mismatched_confirmation(self):
        self.login("manager")
        response = self.client.post(
            f"/manager/roster/{self.employee_id()}/password",
            data={"password": "fresh-private-passphrase",
                  "confirm_password": "different-private-passphrase"},
            follow_redirects=True)
        self.assertIn(b"passwords do not match", response.data.lower())
        conn = appmod.db()
        stored = conn.execute(
            "SELECT password FROM users WHERE username='alex'"
        ).fetchone()[0]
        conn.close()
        self.assertTrue(appmod.check_password_hash(stored, "alex"))

    def test_roster_password_reset_requires_confirmation_in_a_dialog(self):
        self.login("manager")
        response = self.client.get("/manager/roster")
        self.assertIn(b"showModal()", response.data)
        self.assertIn(b"confirm_password", response.data)
        self.assertNotIn(b"New password (12+)", response.data)

    def test_employee_cannot_reset_another_employees_password(self):
        self.login("alex")
        response = self.client.post(
            f"/manager/roster/{self.employee_id('sam')}/password",
            data={"password": "fresh-private-passphrase"})
        self.assertEqual(response.status_code, 403)

    def test_manager_can_delete_employee_and_related_records(self):
        uid = self.employee_id()
        conn = appmod.db()
        shift_id = conn.execute(
            "SELECT id FROM shifts WHERE week_start=? LIMIT 1", (self.week,)).fetchone()[0]
        conn.execute("INSERT INTO picks (user_id, shift_id, rank) VALUES (?,?,?)",
                     (uid, shift_id, 1))
        conn.execute("INSERT OR IGNORE INTO assignments (shift_id,user_id,status) "
                     "VALUES (?,?, 'manager_fixed')", (shift_id, uid))
        conn.execute("INSERT INTO requests (user_id, kind, status, created_at) "
                     "VALUES (?, 'day_off', 'approved', '2026-01-01T00:00:00')", (uid,))
        conn.execute(
            "INSERT INTO requests (user_id, kind, shift_id, target_user_id, status, created_at) "
            "VALUES (?, 'swap', ?, ?, 'approved', '2026-01-01T00:00:00')",
            (self.employee_id("sam"), shift_id, uid))
        conn.execute("INSERT INTO notifications (user_id, kind, message, created_at) "
                     "VALUES (?, 'assignment', 'test', '2026-01-01T00:00:00')", (uid,))
        conn.commit()
        conn.close()

        employee_client = appmod.app.test_client()
        employee_client.post("/login", data={"username": "alex", "password": "alex"})
        self.login("manager")
        response = self.client.post(
            f"/manager/roster/{uid}/delete", data={"confirm": "yes"},
            follow_redirects=True)
        self.assertIn(b"Employee deleted", response.data)
        self.assertIn(b"Log in", employee_client.get("/", follow_redirects=True).data)
        conn = appmod.db()
        sam_id = self.employee_id("sam")
        for table in ("users", "picks", "assignments", "requests", "notifications"):
            self.assertFalse(conn.execute(
                f"SELECT 1 FROM {table} WHERE "
                f"{'id' if table == 'users' else 'user_id'}=?", (uid,)).fetchone(), table)
        self.assertFalse(conn.execute(
            "SELECT 1 FROM requests WHERE target_user_id=?", (uid,)).fetchone())
        cancellation = conn.execute(
            "SELECT message FROM notifications WHERE user_id=? AND kind='conflict' "
            "ORDER BY id DESC LIMIT 1", (sam_id,)).fetchone()
        self.assertIn("invited employee was removed", cancellation["message"])
        conn.close()

    def test_employee_cannot_delete_account_from_manager_roster(self):
        self.login("alex")
        response = self.client.post(
            f"/manager/roster/{self.employee_id('sam')}/delete",
            data={"confirm": "yes"})
        self.assertEqual(response.status_code, 403)
