"""ShiftWise database connection, schema, and lifecycle management."""
import os
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

from werkzeug.security import generate_password_hash

ROOT_DIR = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get("SHIFTWISE_DB_PATH", ROOT_DIR / "scheduler.db"))

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    password TEXT NOT NULL,
    name TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'employee',
    weekly_hours INTEGER DEFAULT 40,
    employment_type TEXT NOT NULL DEFAULT 'part_time',
    hired_on TEXT,
    station TEXT NOT NULL DEFAULT 'front',  -- 'front' = front of house, 'back' = back of house
    email TEXT,
    phone TEXT
);
CREATE TABLE IF NOT EXISTS shifts (
    id INTEGER PRIMARY KEY,
    week_start TEXT NOT NULL,
    day TEXT NOT NULL,
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL,
    slots INTEGER NOT NULL DEFAULT 1,
    note TEXT,
    area TEXT NOT NULL DEFAULT 'front'  -- 'front' = front of house, 'back' = back of house
);
CREATE TABLE IF NOT EXISTS picks (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    shift_id INTEGER NOT NULL,
    rank INTEGER NOT NULL,
    UNIQUE(user_id, shift_id)
);
CREATE TABLE IF NOT EXISTS coverage_preferences (
    user_id INTEGER NOT NULL,
    shift_id INTEGER NOT NULL,
    willing INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY(user_id, shift_id)
);
CREATE TABLE IF NOT EXISTS assignments (
    id INTEGER PRIMARY KEY,
    shift_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'proposed',
    UNIQUE(shift_id, user_id)
);
CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    kind TEXT NOT NULL,              -- day_off / vacation / sick / swap / switch
    shift_id INTEGER,                -- sick: the shift missed; switch: the shift given up
    day TEXT,                        -- day_off: which weekday
    target_shift_id INTEGER,         -- switch: the desired shift
    vacation_start TEXT,             -- vacation: range start (inclusive)
    vacation_end TEXT,             -- vacation: range end (inclusive)
    week_start TEXT,               -- day_off: the week requested
    target_user_id INTEGER,        -- employee-directed swap: requested coworker
    reason TEXT,                   -- decision explanation shown to requester
    status TEXT NOT NULL DEFAULT 'approved',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    shift_id INTEGER,
    kind TEXT NOT NULL,
    message TEXT NOT NULL,
    created_at TEXT NOT NULL,
    read INTEGER NOT NULL DEFAULT 0
);
"""


def db():
    """Create and configure a SQLite connection for the current DB_PATH."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def monday_of(d):
    """Return the Monday preceding or matching the given date."""
    return d - timedelta(days=d.weekday())


def init_db(seed_demo=False, mock_roster=False):
    """Initialize schema, apply migrations, enforce passwords, and seed data if empty."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=15.0)
    conn.execute("PRAGMA busy_timeout=15000")
    conn.executescript(SCHEMA)

    # migration: add new columns to an existing users table if missing
    cols = [r[1] for r in conn.execute("PRAGMA table_info(users)")]
    if "employment_type" not in cols:
        conn.execute(
            "ALTER TABLE users ADD COLUMN employment_type TEXT NOT NULL DEFAULT 'part_time'")
    if "hired_on" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN hired_on TEXT")
    if "station" not in cols:
        conn.execute(
            "ALTER TABLE users ADD COLUMN station TEXT NOT NULL DEFAULT 'front'")
    if "email" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN email TEXT")
    if "phone" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN phone TEXT")
    scols = [r[1] for r in conn.execute("PRAGMA table_info(shifts)")]
    if "area" not in scols:
        conn.execute(
            "ALTER TABLE shifts ADD COLUMN area TEXT NOT NULL DEFAULT 'front'")
    rcols = [r[1] for r in conn.execute("PRAGMA table_info(requests)")]
    if "vacation_start" not in rcols:
        conn.execute("ALTER TABLE requests ADD COLUMN vacation_start TEXT")
    if "vacation_end" not in rcols:
        conn.execute("ALTER TABLE requests ADD COLUMN vacation_end TEXT")
    if "week_start" not in rcols:
        conn.execute("ALTER TABLE requests ADD COLUMN week_start TEXT")
    if "target_user_id" not in rcols:
        conn.execute("ALTER TABLE requests ADD COLUMN target_user_id INTEGER")
    if "reason" not in rcols:
        conn.execute("ALTER TABLE requests ADD COLUMN reason TEXT")

    for user in conn.execute("SELECT id, username, password FROM users").fetchall():
        password = user[2]
        if password.startswith(("scrypt:", "pbkdf2:")):
            continue
        if user[1] == "manager" and password == "manager" and not seed_demo:
            password = os.environ.get("SHIFTWISE_BOOTSTRAP_MANAGER_PASSWORD")
            if not password or len(password) < 12:
                conn.close()
                raise RuntimeError(
                    "Set SHIFTWISE_BOOTSTRAP_MANAGER_PASSWORD (at least 12 characters) to replace the demo manager password"
                )
        conn.execute("UPDATE users SET password=? WHERE id=?",
                     (generate_password_hash(password), user[0]))

    if not conn.execute("SELECT 1 FROM users LIMIT 1").fetchone():
        bootstrap_password = os.environ.get("SHIFTWISE_BOOTSTRAP_MANAGER_PASSWORD")
        if mock_roster or seed_demo in ("8", "mock", 8) or (
                seed_demo and os.environ.get("SHIFTWISE_DEMO_SEED") in ("1", "8", "mock")):
            conn.close()
            import mock_seed
            target_mod = sys.modules.get("app") or sys.modules[__name__]
            mock_seed.seed(target_mod)
            return
        if not seed_demo and (not bootstrap_password or len(bootstrap_password) < 12):
            conn.close()
            raise RuntimeError(
                "Set SHIFTWISE_BOOTSTRAP_MANAGER_PASSWORD (at least 12 characters) to create the first manager"
            )
        if not seed_demo:
            conn.execute(
                "INSERT INTO users (username, password, name, role, weekly_hours, "
                "employment_type, station) VALUES (?,?,?,?,?,?,?)",
                ("manager", generate_password_hash(bootstrap_password),
                 "Store Manager", "manager", 40, "full_time", "front"))
        else:
            conn.executemany(
                "INSERT INTO users (username, password, name, role, weekly_hours,"
                " employment_type, hired_on, station) VALUES (?,?,?,?,?,?,?,?)",
                [
                    ("manager", generate_password_hash("manager"), "Store Manager", "manager", 40, "full_time", "2020-01-15", "front"),
                    ("alex", generate_password_hash("alex"), "Alex Rivera", "employee", 30, "full_time", "2021-03-01", "front"),
                    ("sam", generate_password_hash("sam"), "Sam Chen", "employee", 25, "part_time", "2023-06-10", "front"),
                    ("taylor", generate_password_hash("taylor"), "Taylor Brooks", "employee", 20, "part_time", "2025-02-11", "front"),
                    ("jordan", generate_password_hash("jordan"), "Jordan Diaz", "employee", 35, "part_time", "2024-11-20", "back"),
                    ("casey", generate_password_hash("casey"), "Casey Boots", "employee", 35, "part_time", "2023-05-01", "back"),
                    ("morgan", generate_password_hash("morgan"), "Morgan Vale", "employee", 40, "full_time", "2022-08-15", "back"),
                ],
            )
            week = monday_of(date.today()).isoformat()
            demo = [
                (week, "Mon", "09:00", "17:00", 1, None, "front"),
                (week, "Tue", "09:00", "17:00", 1, None, "front"),
                (week, "Wed", "09:00", "17:00", 1, None, "front"),
                (week, "Thu", "09:00", "17:00", 1, None, "front"),
                (week, "Fri", "09:00", "17:00", 1, None, "front"),
                (week, "Sat", "10:00", "18:00", 2, "Weekend rush", "front"),
                (week, "Sun", "11:00", "16:00", 1, "Short day", "front"),
                (week, "Mon", "06:00", "14:00", 1, None, "back"),
                (week, "Tue", "06:00", "14:00", 1, None, "back"),
                (week, "Wed", "06:00", "14:00", 1, None, "back"),
                (week, "Thu", "06:00", "14:00", 1, None, "back"),
                (week, "Fri", "11:00", "20:00", 1, "Dinner prep + service", "back"),
                (week, "Sat", "10:00", "20:00", 1, "Weekend covers", "back"),
                (week, "Sun", "10:00", "16:00", 1, "Brunch", "back"),
            ]
            conn.executemany(
                "INSERT INTO shifts (week_start, day, start_time, end_time, slots, note, area) "
                "VALUES (?,?,?,?,?,?,?)", demo)
    conn.commit()
    conn.close()
