"""Mock smoke tests: FOH/BOH separation, shift coverage, and seed shape."""
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
TESTS_DIR = ROOT_DIR / "tests"
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

import app as appmod
import mock_seed
import pytest
from test_support import isolate_database
from datetime import date

def test_mock_smoke():
    _test_db = isolate_database(appmod)

    DB = appmod.DB_PATH
    if DB.exists():
        DB.unlink()
    appmod.init_db(seed_demo=True)
    mock_seed.seed(appmod, force=True)

    week = appmod.monday_of(__import__("datetime").date.today()).isoformat()
    conn = appmod.db()
    ids = {(r["area"], r["day"], r["start_time"]): r["id"]
           for r in conn.execute("SELECT id, day, area, start_time FROM shifts WHERE week_start=?", (week,))}
    conn.close()
    mon_front, mon_back = ids[("front", "Mon", "07:00")], ids[("back", "Mon", "07:00")]

    # Each employee ranks all 21 shifts in their own house with unique ranks.
    def full_form(client_user, mon_id):
        conn = appmod.db()
        house = conn.execute(
            "SELECT station FROM users WHERE username=?", (client_user,)).fetchone()["station"]
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM shifts WHERE week_start=? AND area=? ORDER BY id",
            (week, house))]
        conn.close()
        ids.remove(mon_id)
        form = {f"rank_{mon_id}": "1"}
        for i, sid in enumerate(ids):
            form[f"rank_{sid}"] = str(i + 2)
        return form

    for u in ("maria", "devon", "priya", "alex", "sam"):
        c = appmod.app.test_client()
        c.post("/login", data={"username": u, "password": u})
        c.post("/pick", data=full_form(u, mon_front))
    for u in ("jordan", "taylor", "riley", "casey", "morgan"):
        c = appmod.app.test_client()
        c.post("/login", data={"username": u, "password": u})
        c.post("/pick", data=full_form(u, mon_back))

    appmod.run_scheduler(week)

    conn = appmod.db()
    rows = conn.execute(
        "SELECT u.username, s.area, s.day FROM assignments a "
        "JOIN users u ON u.id=a.user_id JOIN shifts s ON s.id=a.shift_id "
        "WHERE s.day='Mon' AND s.start_time='07:00'").fetchall()
    cross = conn.execute(
        "SELECT u.username FROM assignments a JOIN users u ON u.id=a.user_id "
        "JOIN shifts s ON s.id=a.shift_id WHERE s.area!=u.station").fetchall()
    conn.close()

    on_front = sorted(r["username"] for r in rows if r["area"] == "front")
    on_back = sorted(r["username"] for r in rows if r["area"] == "back")
    assert not cross, f"cross-house leak: {[r['username'] for r in cross]}"
    assert on_front == ["maria"], f"FOH Mon opening got {on_front}"
    assert on_back == ["morgan"], f"BOH Mon opening got {on_back}"
    _test_db.cleanup()


def test_seeded_demo_has_staggered_shifts_and_peak_staffing():
    _test_db = isolate_database(appmod)
    appmod.init_db(seed_demo=True)
    mock_seed.seed(appmod, force=True)
    conn = appmod.db()
    week = appmod.monday_of(date.today()).isoformat()
    expected_windows = [("07:00", "15:00"), ("11:00", "19:00"), ("15:00", "23:00")]

    for area in ("front", "back"):
        employees = conn.execute(
            "SELECT username, weekly_hours FROM users WHERE role='employee' AND station=?",
            (area,),
        ).fetchall()
        assert len(employees) == 7
        assert {row["weekly_hours"] for row in employees} == {40}
        for day in ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"):
            shifts = conn.execute(
                "SELECT start_time, end_time, slots FROM shifts "
                "WHERE week_start=? AND area=? AND day=? ORDER BY start_time",
                (week, area, day),
            ).fetchall()
            assert [(row["start_time"], row["end_time"]) for row in shifts] == expected_windows
            expected_slots = 2 if day in ("Thu", "Fri", "Sat") else 1
            assert [row["slots"] for row in shifts] == [expected_slots] * 3
            for hour in range(11, 19):
                staffing = sum(
                    row["slots"] for row in shifts
                    if row["start_time"] <= f"{hour:02}:00" and row["end_time"] > f"{hour:02}:00"
                )
                assert staffing >= 2, f"{area} {day} has {staffing} staff at {hour}:00"

        for employee in employees:
            ranks = [row["rank"] for row in conn.execute(
                "SELECT p.rank FROM picks p JOIN shifts s ON s.id=p.shift_id "
                "WHERE p.user_id=? AND s.week_start=? ORDER BY p.rank",
                (conn.execute("SELECT id FROM users WHERE username=?",
                              (employee["username"],)).fetchone()["id"], week),
            )]
            assert ranks == list(range(1, 22)), (employee["username"], ranks)
    conn.close()
    _test_db.cleanup()


def test_seeded_week_fills_peak_and_nonpeak_shifts_without_rule_violations():
    _test_db = isolate_database(appmod)
    appmod.init_db(seed_demo=True)
    mock_seed.seed(appmod, force=True)
    week = appmod.monday_of(date.today()).isoformat()
    appmod.run_scheduler(week)
    conn = appmod.db()

    shifts = conn.execute(
        "SELECT s.id, s.day, s.slots, COUNT(a.id) AS staffed FROM shifts s "
        "LEFT JOIN assignments a ON a.shift_id=s.id "
        "AND a.status NOT IN ('sick','swap_requested') "
        "WHERE s.week_start=? GROUP BY s.id ORDER BY s.id", (week,),
    ).fetchall()
    assert shifts
    gaps = [(shift["day"], shift["id"], shift["staffed"], shift["slots"])
            for shift in shifts if shift["staffed"] != shift["slots"]]
    assert not gaps, gaps
    for shift in shifts:
        assert shift["staffed"] == shift["slots"], (
            shift["day"], shift["id"], shift["staffed"], shift["slots"])

    employee_totals = conn.execute(
        "SELECT u.username, u.weekly_hours, COUNT(DISTINCT s.day) AS workdays, "
        "SUM((CAST(substr(s.end_time,1,2) AS INTEGER)*60 + "
        "CAST(substr(s.end_time,4,2) AS INTEGER) - "
        "CAST(substr(s.start_time,1,2) AS INTEGER)*60 - "
        "CAST(substr(s.start_time,4,2) AS INTEGER))/60.0) AS hours "
        "FROM assignments a JOIN users u ON u.id=a.user_id "
        "JOIN shifts s ON s.id=a.shift_id "
        "WHERE s.week_start=? AND a.status NOT IN ('sick','swap_requested') "
        "GROUP BY u.id", (week,),
    ).fetchall()
    for employee in employee_totals:
        assert employee["hours"] <= employee["weekly_hours"]
        assert employee["workdays"] <= 5

    cross_house = conn.execute(
        "SELECT u.username FROM assignments a JOIN users u ON u.id=a.user_id "
        "JOIN shifts s ON s.id=a.shift_id WHERE s.week_start=? AND s.area!=u.station",
        (week,),
    ).fetchall()
    assert not cross_house
    peak_slots = sum(s["slots"] for s in shifts if s["day"] in ("Thu", "Fri", "Sat"))
    nonpeak_slots = sum(s["slots"] for s in shifts if s["day"] not in ("Thu", "Fri", "Sat"))
    assert peak_slots > nonpeak_slots
    conn.close()
    _test_db.cleanup()


def test_seed_refuses_to_wipe_nonempty_db_without_force():
    """R6 guard: seed() must raise on a non-empty DB unless force=True."""
    _test_db = isolate_database(appmod)
    appmod.init_db(seed_demo=True)  # leaves a non-empty users table

    with pytest.raises(RuntimeError, match="force=True"):
        mock_seed.seed(appmod)

    # The data is untouched by the refused call...
    conn = appmod.db()
    assert conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"] == 7
    conn.close()

    # ...and an explicit force=True still wipes and reseeds.
    n, d, p = mock_seed.seed(appmod, force=True)
    assert (n, d, p) == (14, 7, 14 * 21)
    _test_db.cleanup()


def test_seed_on_empty_db_needs_no_force():
    """The guard passes silently when the users table is empty."""
    _test_db = isolate_database(appmod)
    appmod.init_db(seed_demo=True)
    conn = appmod.db()
    conn.execute("DELETE FROM users")
    conn.commit()
    conn.close()
    n, d, p = mock_seed.seed(appmod)
    assert (n, d, p) == (14, 7, 14 * 21)
    _test_db.cleanup()


if __name__ == "__main__":
    test_mock_smoke()
    print("MOCK SMOKE TEST PASSED")
