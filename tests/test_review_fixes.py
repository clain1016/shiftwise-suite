import os
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import app as appmod


class ReviewFixes(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="shiftwise-review-")
        appmod.DB_PATH = Path(self.temporary.name) / "scheduler.db"
        appmod.init_db(seed_demo=True)
        self.client = appmod.app.test_client()
        self.week = appmod.monday_of(date.today()).isoformat()

    def tearDown(self):
        self.temporary.cleanup()

    def login(self, username):
        self.client.get("/logout")
        response = self.client.post(
            "/login", data={"username": username, "password": username},
            follow_redirects=True)
        self.assertIn(b"Log out", response.data)

    def test_bootstrap_hashes_passwords_and_adds_employees(self):
        with tempfile.TemporaryDirectory(prefix="shiftwise-bootstrap-") as path:
            appmod.DB_PATH = Path(path) / "scheduler.db"
            with patch.dict(os.environ, {"SHIFTWISE_BOOTSTRAP_MANAGER_PASSWORD":
                                      "private-manager-passphrase"}):
                appmod.init_db()
            conn = appmod.db()
            users = conn.execute("SELECT username, password FROM users").fetchall()
            self.assertEqual(len(users), 1)
            self.assertEqual(users[0]["username"], "manager")
            self.assertNotEqual(users[0]["password"], "private-manager-passphrase")
            conn.close()
            self.client.post("/login", data={"username": "manager",
                                             "password": "private-manager-passphrase"})
            response = self.client.post("/manager/roster/add", data={
                "username": "newhire", "name": "New Hire",
                "password": "private-employee-passphrase",
                "employment_type": "part_time", "weekly_hours": "24",
                "hired_on": "2026-01-01"}, follow_redirects=True)
            self.assertIn(b"Employee added", response.data)
            conn = appmod.db()
            user = conn.execute("SELECT password FROM users WHERE username='newhire'").fetchone()
            conn.close()
            self.assertTrue(appmod.check_password_hash(
                user["password"], "private-employee-passphrase"))
            changed = self.client.post("/account/password", data={
                "old_password": "private-manager-passphrase",
                "new_password": "replacement-manager-passphrase"},
                follow_redirects=True)
            self.assertIn(b"Password changed", changed.data)
            old_login = self.client.post("/login", data={
                "username": "manager", "password": "private-manager-passphrase"},
                follow_redirects=True)
            self.assertIn(b"Wrong username or password", old_login.data)
            new_login = self.client.post("/login", data={
                "username": "manager", "password": "replacement-manager-passphrase"},
                follow_redirects=True)
            self.assertIn(b"Log out", new_login.data)

    def test_day_off_survives_rebuild_and_denial_restores_eligibility(self):
        self.login("alex")
        conn = appmod.db()
        shifts = conn.execute("SELECT id, day FROM shifts WHERE week_start=? ORDER BY id",
                              (self.week,)).fetchall()
        conn.close()
        response = self.client.post("/pick", data={
            f"rank_{row['id']}": str(rank) for rank, row in enumerate(shifts, 1)},
            follow_redirects=True)
        self.assertIn(b"Preferences saved", response.data)
        self.client.post("/request/day_off", data={"day": "Mon"})
        appmod.run_scheduler(self.week)
        conn = appmod.db()
        alex_id = conn.execute("SELECT id FROM users WHERE username='alex'").fetchone()[0]
        monday = next(row["id"] for row in shifts if row["day"] == "Mon")
        self.assertFalse(conn.execute(
            "SELECT 1 FROM assignments WHERE user_id=? AND shift_id=? "
            "AND status NOT IN ('sick','swap_requested')", (alex_id, monday)).fetchone())
        self.assertTrue(conn.execute(
            "SELECT 1 FROM picks WHERE user_id=? AND shift_id=?",
            (alex_id, monday)).fetchone())
        request_id = conn.execute("SELECT id FROM requests WHERE kind='day_off'").fetchone()[0]
        conn.close()
        self.login("manager")
        self.client.post(f"/manager/requests/{request_id}/deny")
        conn = appmod.db()
        self.assertTrue(conn.execute(
            "SELECT 1 FROM assignments WHERE user_id=? AND shift_id=?",
            (alex_id, monday)).fetchone())
        conn.close()

    def test_multiple_vacation_ranges_remain_active(self):
        next_week = appmod.monday_of(date.today()) + timedelta(days=7)
        conn = appmod.db()
        alex_id = conn.execute("SELECT id FROM users WHERE username='alex'").fetchone()[0]
        shifts = []
        for day in ("Mon", "Thu"):
            shift_id = conn.execute(
                "INSERT INTO shifts (week_start, day, start_time, end_time, slots) "
                "VALUES (?,?,?,?,?)", (next_week.isoformat(), day, "09:00", "17:00", 1)
            ).lastrowid
            shifts.append(shift_id)
            conn.execute("INSERT INTO picks (user_id, shift_id, rank) VALUES (?,?,?)",
                         (alex_id, shift_id, len(shifts)))
        conn.commit()
        conn.close()
        appmod.run_scheduler(next_week.isoformat())
        self.login("alex")
        for offset in (0, 3):
            day = (next_week + timedelta(days=offset)).isoformat()
            self.client.post("/request/vacation", data={"vac_start": day,
                                                        "vac_end": day})
        conn = appmod.db()
        for shift_id in shifts:
            shift = conn.execute("SELECT * FROM shifts WHERE id=?", (shift_id,)).fetchone()
            self.assertIn(alex_id, appmod.unavailable_uids(conn, shift))
            self.assertFalse(conn.execute(
                "SELECT 1 FROM assignments WHERE user_id=? AND shift_id=?",
                (alex_id, shift_id)).fetchone())
        conn.close()

    def test_vacation_coverage_can_suggest_extra_day_for_manager_review(self):
        week = (appmod.date.fromisoformat(self.week) + timedelta(days=7)).isoformat()
        conn = appmod.db()
        alex_id = conn.execute(
            "SELECT id FROM users WHERE username='alex'").fetchone()[0]
        conn.execute("UPDATE users SET station='back' WHERE role='employee' "
                     "AND username NOT IN ('alex', 'sam')")
        conn.execute("UPDATE users SET weekly_hours=40 WHERE id=?", (alex_id,))
        shift_ids = []
        for day in ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat"):
            shift_ids.append(conn.execute(
                "INSERT INTO shifts (week_start, day, start_time, end_time, slots, area) "
                "VALUES (?,?,?,?,?,?)", (week, day, "09:00", "17:00", 1, "front")
            ).lastrowid)
        for shift_id in shift_ids[:5]:
            conn.execute("INSERT INTO assignments (shift_id, user_id, status) "
                         "VALUES (?,?,?)", (shift_id, alex_id, "manager_fixed"))
        conn.execute("INSERT INTO picks (user_id, shift_id, rank) VALUES (?,?,?)",
                     (alex_id, shift_ids[5], 7))
        saturday = (date.fromisoformat(week) + timedelta(days=5)).isoformat()
        conn.execute("INSERT INTO requests (user_id, kind, status, vacation_start, "
                     "vacation_end, created_at) VALUES (?, 'vacation', 'approved', ?, ?, ?)",
                     (conn.execute("SELECT id FROM users WHERE username='sam'").fetchone()[0],
                      saturday, saturday, date.today().isoformat()))
        conn.commit()
        self.assertIsNone(appmod.coverage_plan(conn, week, shift_ids[5], None))
        self.assertEqual(appmod.coverage_plan(
            conn, week, shift_ids[5], None, allow_over_limits=True), alex_id)
        conn.close()
        appmod.run_scheduler(week)
        conn = appmod.db()
        self.assertTrue(conn.execute(
            "SELECT 1 FROM assignments WHERE user_id=? AND shift_id=?",
            (alex_id, shift_ids[5])).fetchone())
        conn.close()
        self.login("manager")
        review_page = self.client.get("/manager/requests")
        self.assertIn(b"Review proposed coverage", review_page.data)

    def test_denied_vacation_releases_its_exclusion(self):
        next_week = appmod.monday_of(date.today()) + timedelta(days=7)
        conn = appmod.db()
        shift_id = conn.execute(
            "INSERT INTO shifts (week_start, day, start_time, end_time, slots) "
            "VALUES (?,?,?,?,?)", (next_week.isoformat(), "Mon", "09:00", "17:00", 1)
        ).lastrowid
        alex_id = conn.execute("SELECT id FROM users WHERE username='alex'").fetchone()[0]
        conn.execute("INSERT INTO picks (user_id, shift_id, rank) VALUES (?,?,?)",
                     (alex_id, shift_id, 1))
        conn.commit()
        conn.close()
        self.login("alex")
        self.client.post("/request/vacation", data={
            "vac_start": next_week.isoformat(), "vac_end": next_week.isoformat()})
        conn = appmod.db()
        request_id = conn.execute("SELECT id FROM requests WHERE kind='vacation'").fetchone()[0]
        conn.close()
        self.login("manager")
        self.client.post(f"/manager/requests/{request_id}/deny")
        conn = appmod.db()
        shift = conn.execute("SELECT * FROM shifts WHERE id=?", (shift_id,)).fetchone()
        self.assertNotIn(alex_id, appmod.unavailable_uids(conn, shift))
        self.assertTrue(conn.execute(
            "SELECT 1 FROM assignments WHERE user_id=? AND shift_id=?",
            (alex_id, shift_id)).fetchone())
        conn.close()

    def test_vacation_approval_requires_coverage(self):
        next_week = appmod.monday_of(date.today()) + timedelta(days=7)
        conn = appmod.db()
        shift_id = conn.execute(
            "INSERT INTO shifts (week_start, day, start_time, end_time, slots) "
            "VALUES (?,?,?,?,?)", (next_week.isoformat(), "Mon", "09:00", "17:00", 99)
        ).lastrowid
        alex_id = conn.execute("SELECT id FROM users WHERE username='alex'").fetchone()[0]
        conn.execute("INSERT INTO picks (user_id, shift_id, rank) VALUES (?,?,?)",
                     (alex_id, shift_id, 1))
        conn.commit()
        conn.close()
        self.login("alex")
        self.client.post("/request/vacation", data={
            "vac_start": next_week.isoformat(), "vac_end": next_week.isoformat()})
        conn = appmod.db()
        request_id = conn.execute("SELECT id FROM requests WHERE kind='vacation'").fetchone()[0]
        conn.close()
        self.login("manager")
        page = self.client.get("/manager/requests")
        self.assertIn(b"Coverage needed", page.data)
        response = self.client.post(f"/manager/requests/{request_id}/approve",
                                    follow_redirects=True)
        self.assertIn(b"coverage is arranged", response.data)
        conn = appmod.db()
        status = conn.execute("SELECT status FROM requests WHERE id=?", (request_id,)).fetchone()[0]
        conn.close()
        self.assertEqual(status, "approved")

    def test_vacation_approval_succeeds_after_shift_is_backfilled(self):
        next_week = appmod.monday_of(date.today()) + timedelta(days=7)
        conn = appmod.db()
        shift_id = conn.execute(
            "INSERT INTO shifts (week_start, day, start_time, end_time, slots, area) "
            "VALUES (?,?,?,?,?,?)", (next_week.isoformat(), "Mon", "09:00", "17:00", 1, "front")
        ).lastrowid
        alex_id = conn.execute("SELECT id FROM users WHERE username='alex'").fetchone()[0]
        conn.execute("INSERT INTO assignments (shift_id, user_id, status) "
                     "VALUES (?,?, 'manager_fixed')", (shift_id, alex_id))
        conn.commit()
        conn.close()
        self.login("alex")
        self.client.post("/request/vacation", data={
            "vac_start": next_week.isoformat(), "vac_end": next_week.isoformat()})
        conn = appmod.db()
        request_id = conn.execute("SELECT id FROM requests WHERE kind='vacation'").fetchone()[0]
        staffed = conn.execute("SELECT COUNT(*) FROM assignments WHERE shift_id=? "
                               "AND status NOT IN ('sick','swap_requested')",
                               (shift_id,)).fetchone()[0]
        conn.close()
        self.assertEqual(staffed, 1, "scheduler should backfill the vacationing employee's shift")
        self.login("manager")
        response = self.client.post(f"/manager/requests/{request_id}/approve",
                                    follow_redirects=True)
        self.assertIn(b"Request approved", response.data)
        conn = appmod.db()
        status = conn.execute("SELECT status FROM requests WHERE id=?", (request_id,)).fetchone()[0]
        notified = conn.execute(
            "SELECT 1 FROM notifications WHERE user_id=? AND message LIKE '%vacation request%' "
            "AND message LIKE '%approved%'", (alex_id,)).fetchone()
        conn.close()
        self.assertEqual(status, "approved_ok")
        self.assertIsNotNone(notified)

    def test_eighth_shift_accepts_complete_ranking(self):
        self.login("manager")
        response = self.client.post("/manager/shift/add", data={
            "day": "Mon", "start": "18:00", "end": "20:00", "slots": "1"},
            follow_redirects=True)
        self.assertIn(b"Shift added", response.data)
        self.login("alex")
        conn = appmod.db()
        # alex is front-of-house: only his own house's shifts are rankable
        ids = [r[0] for r in conn.execute(
            "SELECT s.id FROM shifts s JOIN users u ON u.username='alex' "
            "WHERE s.week_start=? AND s.area=u.station ORDER BY s.id",
            (self.week,))]
        conn.close()
        self.assertGreater(len(ids), 7)  # beyond a single week of shifts
        response = self.client.post("/pick", data={
            f"rank_{sid}": str(rank) for rank, sid in enumerate(ids, 1)},
            follow_redirects=True)
        self.assertIn(b"Preferences saved", response.data)
        conn = appmod.db()
        count = conn.execute("SELECT COUNT(*) FROM picks WHERE user_id="
                             "(SELECT id FROM users WHERE username='alex')").fetchone()[0]
        conn.close()
        self.assertEqual(count, len(ids))

    def test_overlap_rejected_and_unchanged_schedule_not_reposted(self):
        conn = appmod.db()
        conn.execute("DELETE FROM shifts")
        alex_id = conn.execute("SELECT id FROM users WHERE username='alex'").fetchone()[0]
        for rank, (start, end) in enumerate((("09:00", "17:00"),
                                             ("12:00", "16:00")), 1):
            shift_id = conn.execute(
                "INSERT INTO shifts (week_start, day, start_time, end_time, slots) "
                "VALUES (?,?,?,?,?)", (self.week, "Mon", start, end, 1)
            ).lastrowid
            conn.execute("INSERT INTO picks (user_id, shift_id, rank) VALUES (?,?,?)",
                         (alex_id, shift_id, rank))
        conn.commit()
        conn.close()
        appmod.run_scheduler(self.week)
        conn = appmod.db()
        assigned = conn.execute(
            "SELECT COUNT(*) FROM assignments WHERE user_id=? AND status NOT IN ('sick','swap_requested')",
            (alex_id,)).fetchone()[0]
        posted = conn.execute(
            "SELECT COUNT(*) FROM notifications WHERE user_id=? "
            "AND message LIKE 'Schedule posted:%'", (alex_id,)).fetchone()[0]
        all_notifications = conn.execute(
            "SELECT COUNT(*) FROM notifications WHERE user_id=?",
            (alex_id,)).fetchone()[0]
        conn.close()
        self.assertEqual(assigned, 1)
        appmod.run_scheduler(self.week)
        conn = appmod.db()
        reposted = conn.execute(
            "SELECT COUNT(*) FROM notifications WHERE user_id=? "
            "AND message LIKE 'Schedule posted:%'", (alex_id,)).fetchone()[0]
        repeated_notifications = conn.execute(
            "SELECT COUNT(*) FROM notifications WHERE user_id=?",
            (alex_id,)).fetchone()[0]
        conn.close()
        self.assertEqual(reposted, posted)
        self.assertEqual(repeated_notifications, all_notifications)

    def test_uncovered_swap_does_not_fill_capacity_and_can_be_denied(self):
        conn = appmod.db()
        conn.execute("DELETE FROM shifts")
        conn.execute("DELETE FROM users WHERE role='employee' AND username!='alex'")
        alex_id = conn.execute("SELECT id FROM users WHERE username='alex'").fetchone()[0]
        shift_id = conn.execute(
            "INSERT INTO shifts (week_start, day, start_time, end_time, slots) "
            "VALUES (?,?,?,?,?)", (self.week, "Mon", "09:00", "17:00", 1)
        ).lastrowid
        conn.execute("INSERT INTO assignments (shift_id, user_id, status) "
                     "VALUES (?,?,?)", (shift_id, alex_id, "confirmed"))
        conn.commit()
        conn.close()
        self.login("alex")
        self.client.post(f"/swap/{shift_id}")
        conn = appmod.db()
        status = conn.execute("SELECT status FROM assignments WHERE shift_id=?",
                              (shift_id,)).fetchone()[0]
        request_id = conn.execute("SELECT id FROM requests WHERE kind='swap'").fetchone()[0]
        alert_count = conn.execute(
            "SELECT COUNT(*) FROM notifications WHERE shift_id=? "
            "AND message LIKE 'No cover available%'", (shift_id,)).fetchone()[0]
        conn.close()
        self.assertEqual(status, "swap_requested")
        appmod.run_scheduler(self.week)
        conn = appmod.db()
        repeated_alerts = conn.execute(
            "SELECT COUNT(*) FROM notifications WHERE shift_id=? "
            "AND message LIKE 'No cover available%'", (shift_id,)).fetchone()[0]
        conn.close()
        self.assertEqual(repeated_alerts, alert_count)
        self.login("manager")
        response = self.client.get("/manager")
        self.assertIn(b"0/1", response.data)
        self.client.post(f"/manager/requests/{request_id}/deny")
        conn = appmod.db()
        status = conn.execute("SELECT status FROM assignments WHERE shift_id=?",
                              (shift_id,)).fetchone()[0]
        conn.close()
        self.assertEqual(status, "confirmed")

    def test_switch_approval_respects_hours_and_shift_input(self):
        self.login("manager")
        conn = appmod.db()
        original_count = conn.execute("SELECT COUNT(*) FROM shifts").fetchone()[0]
        conn.close()
        self.client.post("/manager/shift/add", data={
            "day": "Mon", "start": "17:00", "end": "09:00", "slots": "1"})
        conn = appmod.db()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM shifts").fetchone()[0],
                         original_count)
        conn.execute("DELETE FROM shifts")
        alex_id = conn.execute("SELECT id FROM users WHERE username='alex'").fetchone()[0]
        conn.execute("UPDATE users SET weekly_hours=16 WHERE id=?", (alex_id,))
        ids = []
        for day, start, end in (("Mon", "09:00", "17:00"),
                                ("Tue", "09:00", "17:00"),
                                ("Wed", "09:00", "21:00")):
            ids.append(conn.execute(
                "INSERT INTO shifts (week_start, day, start_time, end_time, slots) "
                "VALUES (?,?,?,?,?)", (self.week, day, start, end, 1)
            ).lastrowid)
        for shift_id in ids[:2]:
            conn.execute("INSERT INTO assignments (shift_id, user_id, status) "
                         "VALUES (?,?,?)", (shift_id, alex_id, "confirmed"))
        conn.commit()
        conn.close()
        self.login("alex")
        self.client.post(f"/request/switch/{ids[0]}",
                         data={"target_shift": str(ids[2])})
        conn = appmod.db()
        request_id = conn.execute("SELECT id FROM requests WHERE kind='switch'").fetchone()[0]
        conn.close()
        self.login("manager")
        self.client.post(f"/manager/requests/{request_id}/approve")
        conn = appmod.db()
        status = conn.execute("SELECT status FROM requests WHERE id=?",
                              (request_id,)).fetchone()[0]
        kept_source = conn.execute(
            "SELECT 1 FROM assignments WHERE shift_id=? AND user_id=?",
            (ids[0], alex_id)).fetchone()
        moved_target = conn.execute(
            "SELECT 1 FROM assignments WHERE shift_id=? AND user_id=?",
            (ids[2], alex_id)).fetchone()
        conn.close()
        self.assertEqual(status, "denied")
        self.assertTrue(kept_source)
        self.assertFalse(moved_target)

    def test_manager_unassign_does_not_reclaim_same_employee(self):
        conn = appmod.db()
        conn.execute("DELETE FROM shifts")
        alex_id = conn.execute("SELECT id FROM users WHERE username='alex'").fetchone()[0]
        shift_id = conn.execute(
            "INSERT INTO shifts (week_start, day, start_time, end_time, slots) "
            "VALUES (?,?,?,?,?)", (self.week, "Mon", "09:00", "17:00", 1)
        ).lastrowid
        conn.execute("INSERT INTO picks (user_id, shift_id, rank) VALUES (?,?,?)",
                     (alex_id, shift_id, 1))
        conn.commit()
        conn.close()
        appmod.run_scheduler(self.week)
        self.login("manager")
        self.client.post(f"/manager/unassign/{shift_id}/{alex_id}")
        conn = appmod.db()
        reassigned = conn.execute(
            "SELECT 1 FROM assignments WHERE shift_id=? AND user_id=?",
            (shift_id, alex_id)).fetchone()
        conn.close()
        self.assertFalse(reassigned)
        self.client.post(f"/manager/shift/delete/{shift_id}")
        conn = appmod.db()
        replacement = conn.execute(
            "INSERT INTO shifts (week_start, day, start_time, end_time, slots) "
            "VALUES (?,?,?,?,?)", (self.week, "Mon", "09:00", "17:00", 1)
        ).lastrowid
        conn.commit()
        conn.close()
        appmod.run_scheduler(self.week)
        conn = appmod.db()
        replacement_shift = conn.execute("SELECT * FROM shifts WHERE id=?",
                                         (replacement,)).fetchone()
        self.assertNotIn(alex_id, appmod.unavailable_uids(conn, replacement_shift))
        conn.close()

    def test_duplicate_day_off_and_handled_request_denial(self):
        self.login("alex")
        first = self.client.post("/request/day_off", data={"day": "Mon"},
                                 follow_redirects=True)
        self.assertIn(b"Day off requested for Mon", first.data)
        dup = self.client.post("/request/day_off", data={"day": "Mon"},
                               follow_redirects=True)
        self.assertIn(b"You already have an active request for Mon off", dup.data)

        conn = appmod.db()
        req_id = conn.execute("SELECT id FROM requests WHERE kind='day_off' AND status='approved'").fetchone()[0]
        conn.close()

        self.login("manager")
        deny = self.client.post(f"/manager/requests/{req_id}/deny", follow_redirects=True)
        self.assertIn(b"Request denied", deny.data)
        deny_again = self.client.post(f"/manager/requests/{req_id}/deny", follow_redirects=True)
        self.assertIn(b"Request not found or already handled", deny_again.data)

    def test_delete_shift_rebuilds_target_week(self):
        next_week = (appmod.monday_of(date.today()) + timedelta(days=7)).isoformat()
        conn = appmod.db()
        shift_id = conn.execute(
            "INSERT INTO shifts (week_start, day, start_time, end_time, slots) "
            "VALUES (?,?,?,?,?)", (next_week, "Mon", "09:00", "17:00", 1)
        ).lastrowid
        alex_id = conn.execute("SELECT id FROM users WHERE username='alex'").fetchone()[0]
        conn.execute("INSERT INTO picks (user_id, shift_id, rank) VALUES (?,?,?)",
                     (alex_id, shift_id, 1))
        conn.commit()
        conn.close()
        appmod.run_scheduler(next_week)

        conn = appmod.db()
        assigned = conn.execute(
            "SELECT 1 FROM assignments WHERE shift_id=? AND user_id=?",
            (shift_id, alex_id)).fetchone()
        conn.close()
        self.assertTrue(assigned)

        self.login("manager")
        response = self.client.post(f"/manager/shift/delete/{shift_id}", follow_redirects=True)
        self.assertEqual(response.status_code, 200)

        conn = appmod.db()
        remaining_shifts = conn.execute("SELECT 1 FROM shifts WHERE id=?", (shift_id,)).fetchone()
        remaining_assignments = conn.execute("SELECT 1 FROM assignments WHERE shift_id=?", (shift_id,)).fetchone()
        conn.close()
        self.assertIsNone(remaining_shifts)
        self.assertIsNone(remaining_assignments)


    def test_over_limit_cover_only_fills_vacation_gaps(self):
        """Only slots a pending vacation holds open may be filled past the caps."""
        week = appmod.monday_of(date.today()).isoformat()
        conn = appmod.db()
        for table in ("picks", "assignments", "notifications", "requests",
                      "shifts", "users"):
            conn.execute(f"DELETE FROM {table}")
        for username, station, cap in (("alex", "front", 8),
                                       ("sam", "front", 40),
                                       ("bob", "back", 8)):
            conn.execute(
                "INSERT INTO users (username, password, name, role, weekly_hours,"
                " employment_type, hired_on, station) VALUES (?,?,?,?,?,?,?,?)",
                (username, appmod.generate_password_hash(username), username.title(),
                 "employee", cap, "part_time", "2024-01-01", station))
        shifts = {}
        for area, day in (("front", "Mon"), ("front", "Tue"),
                          ("back", "Tue"), ("back", "Wed")):
            shifts[(area, day)] = conn.execute(
                "INSERT INTO shifts (week_start, day, start_time, end_time, slots, area) "
                "VALUES (?,?,?,?,?,?)",
                (week, day, "09:00", "17:00", 1, area)).lastrowid
        uid = {r["username"]: r["id"]
               for r in conn.execute("SELECT id, username FROM users")}
        for (area, day), username in ((("front", "Tue"), "alex"),
                                      (("back", "Tue"), "bob"),
                                      (("front", "Mon"), "sam")):
            conn.execute("INSERT INTO assignments (shift_id, user_id, status) "
                         "VALUES (?,?,'manager_fixed')",
                         (shifts[(area, day)], uid[username]))
        conn.execute("INSERT INTO picks (user_id, shift_id, rank) VALUES (?,?,1)",
                     (uid["alex"], shifts[("front", "Mon")]))
        conn.execute("INSERT INTO picks (user_id, shift_id, rank) VALUES (?,?,1)",
                     (uid["bob"], shifts[("back", "Wed")]))
        # a pending vacation on Monday only — it holds the front Monday slot open
        conn.execute("INSERT INTO requests (user_id, kind, vacation_start, "
                     "vacation_end, status, created_at) VALUES (?,?,?,?,?,?)",
                     (uid["sam"], "vacation", week, week, "approved", week))
        conn.commit()
        conn.close()

        def assigned_to(username, area, day):
            conn = appmod.db()
            found = conn.execute(
                "SELECT 1 FROM assignments a JOIN users u ON u.id=a.user_id "
                "JOIN shifts s ON s.id=a.shift_id WHERE u.username=? AND s.area=? "
                "AND s.day=? AND s.week_start=? "
                "AND a.status NOT IN ('sick','swap_requested')",
                (username, area, day, week)).fetchone() is not None
            conn.close()
            return found

        appmod.run_scheduler(week)
        # the vacation-driven gap may go over alex's 8h cap (16h that week) ...
        self.assertTrue(assigned_to("alex", "front", "Mon"))
        # ... but Wednesday's gap is nobody's vacation, so it stays inside the cap
        self.assertFalse(assigned_to("bob", "back", "Wed"))

        conn = appmod.db()
        conn.execute("DELETE FROM requests")
        conn.commit()
        conn.close()
        appmod.run_scheduler(week)
        self.assertFalse(assigned_to("alex", "front", "Mon"))


if __name__ == "__main__":
    unittest.main()
