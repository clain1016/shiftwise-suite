"""Phase 3 tests: Live PostgreSQL route and engine query verification.

Validates that route queries, conflict handling (ON CONFLICT), temporal scalar
normalization, declarative cascades, transactions (begin_write), and scheduler
execution work against a real PostgreSQL instance.

When a live PostgreSQL server is reachable (via DATABASE_URL or the local dev
container), these tests execute against it. Otherwise, they skip gracefully.
"""

import os
from datetime import date, datetime

import pytest

import app as appmod
import mock_seed
from shiftwise.db import (
    UNIQUE_VIOLATION_ERRORS,
    begin_write,
    db,
    init_db,
    monday_of,
)
from shiftwise.domain.constants import DAYS
from shiftwise.scheduler.engine import run_scheduler
from shiftwise.security import use_csrf_aware_test_client

POSTGRES_TEST_URL = os.environ.get(
    "TEST_DATABASE_URL",
    os.environ.get(
        "DATABASE_URL",
        "postgresql://postgres:devpass@127.0.0.1:5432/shiftwise_phase3_test",
    ),
)


def _is_postgres_available():
    try:
        import psycopg

        with psycopg.connect(POSTGRES_TEST_URL, connect_timeout=2) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1;")
                return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _is_postgres_available(),
    reason="Live PostgreSQL server not reachable at " + POSTGRES_TEST_URL,
)


@pytest.fixture(autouse=True)
def postgres_env(monkeypatch):
    """Point application to the live test PostgreSQL database for each test."""
    monkeypatch.setenv("SHIFTWISE_DB_ENGINE", "postgres")
    monkeypatch.setenv("DATABASE_URL", POSTGRES_TEST_URL)
    monkeypatch.setenv("SHIFTWISE_BOOTSTRAP_MANAGER_PASSWORD", "bootstrap-manager-pass")

    # Initialize a clean schema before each test
    init_db(seed_demo=False)
    conn = db()
    for table in (
        "picks",
        "coverage_preferences",
        "assignments",
        "notifications",
        "requests",
        "login_attempts",
        "shifts",
        "users",
    ):
        conn.execute(f"DELETE FROM {table}")
    conn.commit()
    conn.close()

    yield

    conn = db()
    for table in (
        "picks",
        "coverage_preferences",
        "assignments",
        "notifications",
        "requests",
        "login_attempts",
        "shifts",
        "users",
    ):
        conn.execute(f"DELETE FROM {table}")
    conn.commit()
    conn.close()


def test_postgres_pool_guc_options():
    """Verify ConnectionPool configures statement_timeout and lock_timeout via options."""
    conn = db()
    cur = conn.cursor()
    cur.execute("SHOW statement_timeout;")
    st = cur.fetchone()[0]
    cur.execute("SHOW lock_timeout;")
    lt = cur.fetchone()[0]
    conn.close()
    assert st == "30s"
    assert lt == "5s"


def test_postgres_temporal_normalization():
    """Verify _sqlite_scalar normalizes DATE, TIME, and TIMESTAMPTZ columns into string shapes."""
    conn = db()
    today_str = date.today().isoformat()
    now_str = datetime.now().isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO users (username, password, name, role, hired_on) "
        "VALUES (?, ?, ?, 'employee', ?)",
        ("worker1", "pass123", "Worker One", today_str),
    )
    user_id = conn.execute("SELECT id, hired_on FROM users WHERE username='worker1'").fetchone()
    assert isinstance(user_id["hired_on"], str)
    assert user_id["hired_on"] == today_str

    conn.execute(
        "INSERT INTO shifts (week_start, day, start_time, end_time, slots, area) "
        "VALUES (?, 'Mon', '09:00', '17:00', 1, 'front')",
        (monday_of(date.today()).isoformat(),),
    )
    shift = conn.execute("SELECT start_time, end_time FROM shifts LIMIT 1").fetchone()
    assert isinstance(shift["start_time"], str)
    assert shift["start_time"] == "09:00"
    assert isinstance(shift["end_time"], str)
    assert shift["end_time"] == "17:00"

    conn.execute(
        "INSERT INTO notifications (user_id, kind, message, created_at) VALUES (?, ?, ?, ?)",
        (user_id["id"], "alert", "Test message", now_str),
    )
    notif = conn.execute("SELECT created_at FROM notifications LIMIT 1").fetchone()
    assert isinstance(notif["created_at"], str)
    assert "T" in notif["created_at"]
    conn.commit()
    conn.close()


def test_postgres_mock_seed_and_counts():
    """Verify full demo mock seeding on live PostgreSQL."""
    n_emp, n_days, n_picks = mock_seed.seed(appmod, force=True)
    assert n_emp == 14
    assert n_days == 7
    assert n_picks == 294

    conn = db()
    emp_count = conn.execute("SELECT COUNT(*) c FROM users WHERE role='employee'").fetchone()["c"]
    shift_count = conn.execute("SELECT COUNT(*) c FROM shifts").fetchone()["c"]
    pick_count = conn.execute("SELECT COUNT(*) c FROM picks").fetchone()["c"]
    conn.close()
    assert emp_count == 14
    assert shift_count == 42  # 7 days * 3 windows * 2 houses
    assert pick_count == 294


def test_postgres_on_conflict_picks_upsert():
    """Verify ON CONFLICT (user_id, shift_id) DO UPDATE SET rank=excluded.rank preserves row ID."""
    mock_seed.seed(appmod, force=True)
    conn = db()
    maria = conn.execute("SELECT id FROM users WHERE username='maria'").fetchone()["id"]
    shift = conn.execute("SELECT id FROM shifts LIMIT 1").fetchone()["id"]

    conn.execute(
        "INSERT INTO picks (user_id, shift_id, rank) VALUES (?, ?, 1) "
        "ON CONFLICT (user_id, shift_id) DO UPDATE SET rank=excluded.rank",
        (maria, shift),
    )
    conn.commit()
    pick_id1 = conn.execute(
        "SELECT id, rank FROM picks WHERE user_id=? AND shift_id=?", (maria, shift)
    ).fetchone()
    assert pick_id1["rank"] == 1

    # Update rank for same (user_id, shift_id)
    conn.execute(
        "INSERT INTO picks (user_id, shift_id, rank) VALUES (?, ?, 5) "
        "ON CONFLICT (user_id, shift_id) DO UPDATE SET rank=excluded.rank",
        (maria, shift),
    )
    conn.commit()
    pick_id2 = conn.execute(
        "SELECT id, rank FROM picks WHERE user_id=? AND shift_id=?", (maria, shift)
    ).fetchone()
    assert pick_id2["rank"] == 5
    assert pick_id2["id"] == pick_id1["id"]  # Row ID does not churn
    conn.close()


def test_postgres_on_conflict_assignments_do_nothing():
    """Verify assignments ON CONFLICT DO NOTHING ignores duplicates cleanly."""
    mock_seed.seed(appmod, force=True)
    conn = db()
    maria = conn.execute("SELECT id FROM users WHERE username='maria'").fetchone()["id"]
    shift = conn.execute("SELECT id FROM shifts LIMIT 1").fetchone()["id"]

    conn.execute(
        "INSERT INTO assignments (shift_id, user_id) VALUES (?, ?) ON CONFLICT DO NOTHING",
        (shift, maria),
    )
    conn.commit()
    count1 = conn.execute(
        "SELECT COUNT(*) c FROM assignments WHERE shift_id=? AND user_id=?", (shift, maria)
    ).fetchone()["c"]
    assert count1 == 1

    # Duplicate insert does nothing
    conn.execute(
        "INSERT INTO assignments (shift_id, user_id) VALUES (?, ?) ON CONFLICT DO NOTHING",
        (shift, maria),
    )
    conn.commit()
    count2 = conn.execute(
        "SELECT COUNT(*) c FROM assignments WHERE shift_id=? AND user_id=?", (shift, maria)
    ).fetchone()["c"]
    assert count2 == 1
    conn.close()


def test_postgres_on_conflict_coverage_preferences():
    """Verify coverage_preferences upsert sets boolean willing flag correctly."""
    mock_seed.seed(appmod, force=True)
    conn = db()
    maria = conn.execute("SELECT id FROM users WHERE username='maria'").fetchone()["id"]
    shift = conn.execute("SELECT id FROM shifts LIMIT 1").fetchone()["id"]

    conn.execute(
        "INSERT INTO coverage_preferences (user_id, shift_id, willing) VALUES (?, ?, ?) "
        "ON CONFLICT (user_id, shift_id) DO UPDATE SET willing=excluded.willing",
        (maria, shift, True),
    )
    conn.commit()
    pref1 = conn.execute(
        "SELECT willing FROM coverage_preferences WHERE user_id=? AND shift_id=?",
        (maria, shift),
    ).fetchone()["willing"]
    assert pref1 is True

    conn.execute(
        "INSERT INTO coverage_preferences (user_id, shift_id, willing) VALUES (?, ?, ?) "
        "ON CONFLICT (user_id, shift_id) DO UPDATE SET willing=excluded.willing",
        (maria, shift, False),
    )
    conn.commit()
    pref2 = conn.execute(
        "SELECT willing FROM coverage_preferences WHERE user_id=? AND shift_id=?",
        (maria, shift),
    ).fetchone()["willing"]
    assert pref2 is False
    conn.close()


def test_postgres_unique_violation_catch():
    """Verify UNIQUE_VIOLATION_ERRORS catches duplicate key on live PostgreSQL."""
    conn = db()
    conn.execute("INSERT INTO users (username, password, name) VALUES ('dupe', 'hash', 'Dupe 1')")
    conn.commit()

    with pytest.raises(UNIQUE_VIOLATION_ERRORS):
        conn.execute(
            "INSERT INTO users (username, password, name) VALUES ('dupe', 'hash2', 'Dupe 2')"
        )
        conn.commit()
    conn.close()


def test_postgres_begin_write_and_close_rollback():
    """Verify begin_write executes without error, and close() rolls back aborted transactions."""
    conn = db()
    begin_write(conn)
    conn.execute(
        "INSERT INTO users (username, password, name) VALUES ('uncommitted', 'hash', 'Uncommitted')"
    )
    # Close without commit -> must roll back
    conn.close()

    conn2 = db()
    row = conn2.execute("SELECT 1 FROM users WHERE username='uncommitted'").fetchone()
    conn2.close()
    assert row is None


def test_postgres_declarative_fk_cascades():
    """Verify deleting a shift cascades to picks, coverage_preferences, and assignments."""
    mock_seed.seed(appmod, force=True)
    conn = db()
    shift = conn.execute("SELECT id FROM shifts LIMIT 1").fetchone()["id"]
    maria = conn.execute("SELECT id FROM users WHERE username='maria'").fetchone()["id"]

    conn.execute(
        "INSERT INTO assignments (shift_id, user_id) VALUES (?, ?) ON CONFLICT DO NOTHING",
        (shift, maria),
    )
    conn.execute(
        "INSERT INTO coverage_preferences (user_id, shift_id, willing) VALUES (?, ?, TRUE) "
        "ON CONFLICT (user_id, shift_id) DO UPDATE SET willing=TRUE",
        (maria, shift),
    )
    conn.commit()

    # Verify rows exist
    assert conn.execute("SELECT 1 FROM picks WHERE shift_id=?", (shift,)).fetchone()
    assert conn.execute("SELECT 1 FROM coverage_preferences WHERE shift_id=?", (shift,)).fetchone()
    assert conn.execute("SELECT 1 FROM assignments WHERE shift_id=?", (shift,)).fetchone()

    # Delete shift: database ON DELETE CASCADE must drop all child rows atomically
    conn.execute("DELETE FROM shifts WHERE id=?", (shift,))
    conn.commit()

    assert not conn.execute("SELECT 1 FROM picks WHERE shift_id=?", (shift,)).fetchall()
    assert not conn.execute(
        "SELECT 1 FROM coverage_preferences WHERE shift_id=?", (shift,)
    ).fetchall()
    assert not conn.execute("SELECT 1 FROM assignments WHERE shift_id=?", (shift,)).fetchall()
    conn.close()


def test_postgres_roster_ordering_query():
    """Verify boolean expression ordering in roster query works on PostgreSQL."""
    conn = db()
    conn.execute(
        "INSERT INTO users (username, password, name, role, employment_type, hired_on) VALUES "
        "('pt_late', 'p', 'PT Late', 'employee', 'part_time', '2024-01-01'), "
        "('ft_new', 'p', 'FT New', 'employee', 'full_time', '2023-01-01'), "
        "('ft_old', 'p', 'FT Old', 'employee', 'full_time', '2020-01-01'), "
        "('pt_early', 'p', 'PT Early', 'employee', 'part_time', '2022-01-01')"
    )
    conn.commit()

    rows = conn.execute(
        "SELECT username FROM users WHERE role='employee' "
        "ORDER BY employment_type='full_time' DESC, hired_on ISNULL, hired_on"
    ).fetchall()
    conn.close()
    usernames = [r["username"] for r in rows]
    # Full time first (oldest first), then part time (oldest first)
    assert usernames == ["ft_old", "ft_new", "pt_early", "pt_late"]


def test_postgres_scheduler_and_routes_flow():
    """Test full HTTP flow through Flask test client running on live PostgreSQL."""
    mock_seed.seed(appmod, force=True)
    use_csrf_aware_test_client(appmod.app)
    client = appmod.app.test_client()

    # 1. Login as employee
    r = client.post(
        "/login", data={"username": "maria", "password": "maria"}, follow_redirects=True
    )
    assert b"Log out" in r.data

    # 2. View dashboard: triggers UPDATE notifications SET read=TRUE
    dash = client.get("/")
    assert dash.status_code == 200

    # 3. Submit day picks
    form = {f"rank_day_{day}": str(i + 1) for i, day in enumerate(DAYS)}
    pick_resp = client.post("/pick", data=form, follow_redirects=True)
    assert b"Preferences saved" in pick_resp.data

    # 4. Request a day off
    off_resp = client.post("/request/day_off", data={"day": "Wed"}, follow_redirects=True)
    assert b"Day off requested" in off_resp.data

    # 5. Run scheduler over week
    week = monday_of(date.today()).isoformat()
    n_picks = run_scheduler(week)
    assert n_picks > 0

    # 6. Verify calendar healthz check
    healthz = client.get("/healthz")
    assert healthz.status_code == 200
    assert healthz.json == {"status": "ok"}
