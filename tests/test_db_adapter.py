import importlib
import sqlite3

import pytest

import app

sdb = importlib.import_module("shiftwise.db")


def test_database_engine_selection(monkeypatch):
    # Explicit SQLite
    monkeypatch.setenv("SHIFTWISE_DB_ENGINE", "sqlite")
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@localhost:5432/testdb")
    assert sdb._database_engine() == "sqlite"

    # Explicit PostgreSQL with URL
    monkeypatch.setenv("SHIFTWISE_DB_ENGINE", "postgres")
    assert sdb._database_engine() == "postgres"

    monkeypatch.setenv("SHIFTWISE_DB_ENGINE", "postgresql")
    assert sdb._database_engine() == "postgres"

    # Explicit PostgreSQL without URL raises RuntimeError
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(RuntimeError, match="DATABASE_URL is required"):
        sdb._database_engine()

    # Invalid engine raises ValueError
    monkeypatch.setenv("SHIFTWISE_DB_ENGINE", "oracle")
    with pytest.raises(ValueError, match="SHIFTWISE_DB_ENGINE must be one of"):
        sdb._database_engine()

    # Inferred from DATABASE_URL
    monkeypatch.delenv("SHIFTWISE_DB_ENGINE", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgres://user:pass@localhost:5432/testdb")
    assert sdb._database_engine() == "postgres"

    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@localhost:5432/testdb")
    assert sdb._database_engine() == "postgres"

    # Inferred fallback to SQLite
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert sdb._database_engine() == "sqlite"

    # init_db() supports PostgreSQL since Phase 2: it dispatches to the
    # PostgreSQL DDL instead of raising. Full coverage (schema application,
    # seeding, mock_seed through the psycopg facade) lives in
    # tests/test_postgres_phase2.py using a stubbed backend.
    monkeypatch.setenv("SHIFTWISE_DB_ENGINE", "postgres")
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@localhost:5432/testdb")
    monkeypatch.setattr(
        sdb,
        "db",
        lambda: (_ for _ in ()).throw(AssertionError("stubbed in test_postgres_phase2.py")),
    )
    with pytest.raises(AssertionError, match="stubbed in test_postgres_phase2"):
        sdb.init_db()


def test_qmark_to_psycopg_translation():
    # Simple substitution
    assert (
        sdb._qmark_to_psycopg("SELECT * FROM users WHERE id = ?")
        == "SELECT * FROM users WHERE id = %s"
    )
    assert (
        sdb._qmark_to_psycopg("INSERT INTO t (a, b) VALUES (?, ?)")
        == "INSERT INTO t (a, b) VALUES (%s, %s)"
    )

    # String literals preserving question marks
    sql_with_string = "SELECT * FROM users WHERE note = 'why?' AND id = ?"
    assert (
        sdb._qmark_to_psycopg(sql_with_string)
        == "SELECT * FROM users WHERE note = 'why?' AND id = %s"
    )

    # Escaped quotes inside string literals
    sql_escaped_quote = "SELECT * FROM users WHERE note = 'O''Reilly ?' AND id = ?"
    assert (
        sdb._qmark_to_psycopg(sql_escaped_quote)
        == "SELECT * FROM users WHERE note = 'O''Reilly ?' AND id = %s"
    )

    # Double-quoted identifiers preserving question marks
    sql_double_quote = 'SELECT "col?" FROM t WHERE id = ?'
    assert sdb._qmark_to_psycopg(sql_double_quote) == 'SELECT "col?" FROM t WHERE id = %s'

    # Line comments preserving question marks
    sql_line_comment = "SELECT * FROM t -- what is ?\nWHERE id = ?"
    assert sdb._qmark_to_psycopg(sql_line_comment) == "SELECT * FROM t -- what is ?\nWHERE id = %s"

    # Block comments preserving question marks
    sql_block_comment = "SELECT /* check ? */ * FROM t WHERE id = ?"
    assert sdb._qmark_to_psycopg(sql_block_comment) == "SELECT /* check ? */ * FROM t WHERE id = %s"

    # Literal percent signs are escaped for psycopg (even inside strings:
    # psycopg treats % as a placeholder introducer there too)
    sql_like = "SELECT 1 FROM notifications WHERE message LIKE 'No cover available%' AND id = ?"
    assert sdb._qmark_to_psycopg(sql_like) == (
        "SELECT 1 FROM notifications WHERE message LIKE 'No cover available%%' AND id = %s"
    )
    assert sdb._qmark_to_psycopg("SELECT 100 % 7") == "SELECT 100 %% 7"


def test_postgres_row_semantics():
    class DummyCol:
        def __init__(self, name):
            self.name = name

    description = [DummyCol("id"), DummyCol("username"), DummyCol("role")]
    values = [42, "alex", "employee"]
    row = sdb._PostgresRow(values, description)

    # Dictionary access
    assert row["id"] == 42
    assert row["username"] == "alex"
    assert row["role"] == "employee"

    # Positional access matching sqlite3.Row
    assert row[0] == 42
    assert row[1] == "alex"
    assert row[2] == "employee"
    assert row[0:2] == (42, "alex")

    # Sequence unpacking and iteration matching sqlite3.Row values
    uid, uname, urole = row
    assert (uid, uname, urole) == (42, "alex", "employee")
    assert list(row) == [42, "alex", "employee"]
    assert tuple(row) == (42, "alex", "employee")

    # Dictionary views
    assert list(row.keys()) == ["id", "username", "role"]
    assert list(row.values()) == [42, "alex", "employee"]
    assert dict(row) == {"id": 42, "username": "alex", "role": "employee"}
    assert len(row) == 3

    # Tuple-style description fallback
    tuple_desc = [("id", 23), ("score", 23)]
    row2 = sdb._PostgresRow([1, 99], tuple_desc)
    assert row2["id"] == 1
    assert row2["score"] == 99
    assert list(row2) == [1, 99]


def test_postgres_cursor_and_connection_facade():
    class MockRawCursor:
        def __init__(self):
            self.executed = []
            self.description = [("col_a",), ("col_b",)]
            self._queue = [[10, 20]]

        def execute(self, query, params=()):
            self.executed.append((query, params))

        def executemany(self, query, params_seq):
            self.executed.append((query, list(params_seq)))

        def fetchone(self):
            if self._queue:
                return self._queue.pop(0)
            return None

        def fetchmany(self, size=None):
            return [[10, 20]]

        def fetchall(self):
            return [[10, 20]]

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            pass

    class MockRawConn:
        def __init__(self):
            self.cursor_instance = MockRawCursor()
            self.committed = False
            self.rolled_back = False

        def cursor(self, *args, **kwargs):
            return self.cursor_instance

        def commit(self):
            self.committed = True

        def rollback(self):
            self.rolled_back = True

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            pass

    class MockPool:
        def __init__(self):
            self.returned = []

        def putconn(self, conn):
            self.returned.append(conn)

    pool = MockPool()
    raw_conn = MockRawConn()
    pconn = sdb._PostgresConnection(pool, raw_conn)

    # Query execution through facade translating qmark
    pconn.execute("SELECT * FROM t WHERE a = ? AND b = ?", (1, 2))
    assert raw_conn.cursor_instance.executed[-1] == (
        "SELECT * FROM t WHERE a = %s AND b = %s",
        (1, 2),
    )

    # Row return and fetching
    cur = pconn.cursor()
    row = cur.fetchone()
    assert isinstance(row, sdb._PostgresRow)
    assert row[0] == 10
    assert row["col_a"] == 10

    # Iteration over cursor
    raw_conn.cursor_instance._queue = [[1, 2]]
    rows = list(cur)
    assert len(rows) == 1
    assert rows[0][0] == 1

    # fetchall and fetchmany
    assert len(cur.fetchall()) == 1
    assert len(cur.fetchmany(1)) == 1

    # Executemany translation
    pconn.executemany("INSERT INTO t VALUES (?, ?)", [(1, 2), (3, 4)])
    assert raw_conn.cursor_instance.executed[-1] == (
        "INSERT INTO t VALUES (%s, %s)",
        [(1, 2), (3, 4)],
    )

    # Context managers return wrapper
    with pconn as c:
        assert c is pconn
    with pconn.cursor() as wrapped_cur:
        assert isinstance(wrapped_cur, sdb._PostgresCursor)

    # Commit and rollback
    pconn.commit()
    assert raw_conn.committed is True
    pconn.rollback()
    assert raw_conn.rolled_back is True

    # Closing returns connection to pool
    pconn.close()
    assert pool.returned == [raw_conn]

    # Multiple close calls are idempotent
    pconn.close()
    assert len(pool.returned) == 1

    # A putconn failure still detaches the lease (no leak, no reuse)
    class FailingPool:
        def putconn(self, conn):
            raise RuntimeError("pool is closed")

    pconn2 = sdb._PostgresConnection(FailingPool(), MockRawConn())
    with pytest.raises(RuntimeError, match="pool is closed"):
        pconn2.close()
    # lease is detached: further use raises, second close is a silent no-op
    with pytest.raises(RuntimeError, match="closed"):
        pconn2.cursor()
    pconn2.close()

    # Operations after close raise RuntimeError
    with pytest.raises(RuntimeError, match="closed"):
        pconn.cursor()
    with pytest.raises(RuntimeError, match="closed"):
        pconn.execute("SELECT 1")
    with pytest.raises(RuntimeError, match="closed"):
        pconn.commit()
    with pytest.raises(RuntimeError, match="closed"):
        pconn.rollback()


def test_execute_sql_helper_sqlite():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    sdb.execute_sql(conn, "CREATE TABLE test (id INTEGER, val TEXT)")
    sdb.execute_sql(conn, "INSERT INTO test VALUES (?, ?)", (1, "hello"))
    cur = sdb.execute_sql(conn, "SELECT id, val FROM test WHERE id = ?", (1,))
    row = cur.fetchone()
    assert row["id"] == 1
    assert row["val"] == "hello"
    conn.close()


def test_app_facade_exports():
    assert hasattr(app, "execute_sql")
    assert app.execute_sql is sdb.execute_sql
    assert "execute_sql" in app.__all__

    assert hasattr(app, "begin_write")
    assert app.begin_write is sdb.begin_write
    assert "begin_write" in app.__all__

    assert hasattr(app, "UNIQUE_VIOLATION_ERRORS")
    assert app.UNIQUE_VIOLATION_ERRORS is sdb.UNIQUE_VIOLATION_ERRORS
    assert "UNIQUE_VIOLATION_ERRORS" in app.__all__
