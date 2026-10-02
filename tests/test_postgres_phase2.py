"""Phase 2 tests: PostgreSQL DDL, per-engine init_db, and DB-cascade reliance.

No live PostgreSQL server is available in this environment, so server-side
execution (real cascade deletes, orphan checks) is validated by inspection
and left for the reviewer with a live database. These tests pin the DDL
structure, prove init_db() dispatches to the PostgreSQL DDL, prove
mock_seed's SQL survives the psycopg placeholder translation through the
real facade classes, and prove the delete routes rely on DB cascades on
PostgreSQL while keeping the manual cascades on SQLite.
"""

import importlib
import re
from datetime import time as dt_time
from types import SimpleNamespace

import pytest
from flask import session

import app as appmod

sdb = importlib.import_module("shiftwise.db")
manager_routes = importlib.import_module("shiftwise.routes.manager")
roster_routes = importlib.import_module("shiftwise.routes.roster")
auth_mod = importlib.import_module("shiftwise.auth")
mock_seed = importlib.import_module("mock_seed")


# ---------------------------------------------------------------------------
# DDL structure
# ---------------------------------------------------------------------------

TABLES = (
    "users",
    "shifts",
    "picks",
    "coverage_preferences",
    "assignments",
    "requests",
    "notifications",
    "login_attempts",
)


def test_postgres_ddl_defines_all_tables():
    for table in TABLES:
        assert f"CREATE TABLE IF NOT EXISTS {table} (" in sdb.POSTGRES_SCHEMA


def test_postgres_ddl_uses_bigserial_primary_keys():
    for table in ("users", "shifts", "picks", "assignments", "requests", "notifications"):
        pattern = re.compile(
            rf"CREATE TABLE IF NOT EXISTS {table} \(\s*id BIGSERIAL PRIMARY KEY,",
            re.MULTILINE,
        )
        assert pattern.search(sdb.POSTGRES_SCHEMA), table


def test_postgres_ddl_fk_delete_actions_per_column():
    """Pin the exact ON DELETE action of every foreign key, per column.

    Count-based pins can be gamed by a compensating swap between two
    columns; this pins each (table, column) pair individually.
    """
    expected = {
        ("picks", "user_id"): "CASCADE",
        ("picks", "shift_id"): "CASCADE",
        ("coverage_preferences", "user_id"): "CASCADE",
        ("coverage_preferences", "shift_id"): "CASCADE",
        ("assignments", "shift_id"): "CASCADE",
        ("assignments", "user_id"): "CASCADE",
        ("requests", "user_id"): "CASCADE",
        ("requests", "shift_id"): "SET NULL",
        ("requests", "target_shift_id"): "SET NULL",
        ("requests", "target_user_id"): "SET NULL",
        ("notifications", "user_id"): "CASCADE",
        ("notifications", "shift_id"): "SET NULL",
    }
    schema = sdb.POSTGRES_SCHEMA
    for (table, column), action in expected.items():
        block = re.search(
            rf"CREATE TABLE IF NOT EXISTS {table} \((.*?)\n\);",
            schema,
            re.DOTALL,
        )
        assert block, f"table {table} not found in POSTGRES_SCHEMA"
        match = re.search(
            rf"\b{column}\b[^,]*?REFERENCES \w+\(id\) ON DELETE (CASCADE|SET NULL)",
            block.group(1),
        )
        assert match, f"{table}.{column}: no FK with a delete action"
        assert match.group(1) == action, (
            f"{table}.{column}: expected ON DELETE {action}, found ON DELETE {match.group(1)}"
        )
    # No more and no fewer FKs than pinned above.
    assert len(re.findall(r"REFERENCES \w+\(id\)", schema)) == len(expected)


def test_postgres_ddl_every_fk_declares_delete_action():
    actions = re.findall(
        r"REFERENCES \w+\(id\)( ON DELETE (?:CASCADE|SET NULL))?",
        sdb.POSTGRES_SCHEMA,
    )
    assert actions, "no foreign keys found in POSTGRES_SCHEMA"
    assert all(actions), "every REFERENCES must declare ON DELETE CASCADE/SET NULL"


def test_postgres_ddl_column_types():
    schema = sdb.POSTGRES_SCHEMA
    assert "slots INTEGER NOT NULL DEFAULT 1 CHECK (slots >= 1)" in schema
    assert "willing BOOLEAN NOT NULL DEFAULT TRUE" in schema
    assert "read BOOLEAN NOT NULL DEFAULT FALSE" in schema
    assert "created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()" in schema
    assert "hired_on DATE," in schema
    assert "week_start DATE NOT NULL," in schema


def test_postgres_ddl_parses_as_valid_postgres():
    pglast = pytest.importorskip("pglast")
    for statement in sdb._split_ddl(sdb.POSTGRES_SCHEMA):
        pglast.parse_sql(statement)  # raises on syntax error


def test_split_ddl_yields_individual_create_statements():
    statements = sdb._split_ddl(sdb.POSTGRES_SCHEMA)
    assert len(statements) == 8
    assert all(s.startswith("CREATE TABLE IF NOT EXISTS") for s in statements)


def test_split_ddl_ignores_semicolons_in_strings_comments_and_dollar_quotes():
    ddl = (
        "CREATE TABLE t (note TEXT DEFAULT 'a;b');\n"
        "-- a line comment with ; semicolon\n"
        "CREATE TABLE u (id INT); /* block ; comment */\n"
        'CREATE TABLE v ("weird;name" INT);\n'
        "CREATE FUNCTION f() RETURNS void AS $$\n"
        "BEGIN RAISE NOTICE 'x;y'; END;\n"
        "$$ LANGUAGE plpgsql;"
    )
    statements = sdb._split_ddl(ddl)
    assert len(statements) == 4
    assert statements[0] == "CREATE TABLE t (note TEXT DEFAULT 'a;b')"
    assert statements[1].endswith("CREATE TABLE u (id INT)")
    # Comments attach to the statement they precede; the quoted identifier
    # keeps its semicolon.
    assert statements[2].endswith('CREATE TABLE v ("weird;name" INT)')
    assert "/* block ; comment */" in statements[2]
    assert statements[3].startswith("CREATE FUNCTION f()")
    assert "RAISE NOTICE 'x;y'" in statements[3]
    assert statements[3].endswith("$$ LANGUAGE plpgsql")


def test_split_ddl_handles_tagged_dollar_quotes_and_escapes():
    ddl = (
        "CREATE FUNCTION g() RETURNS void AS $body$\n"
        "BEGIN RAISE NOTICE 'it''s; here'; END;\n"
        "$body$ LANGUAGE plpgsql;\n"
        "CREATE TABLE w (note TEXT DEFAULT 'don''t; split');"
    )
    statements = sdb._split_ddl(ddl)
    assert len(statements) == 2
    assert "RAISE NOTICE 'it''s; here'" in statements[0]
    assert statements[1] == "CREATE TABLE w (note TEXT DEFAULT 'don''t; split')"


def test_database_engine_public_accessor():
    assert sdb.database_engine() == sdb._database_engine()
    assert appmod.database_engine is sdb.database_engine
    assert "database_engine" in appmod.__all__
    assert "POSTGRES_SCHEMA" in appmod.__all__


# ---------------------------------------------------------------------------
# Recording fake connection
# ---------------------------------------------------------------------------


class _Rows:
    def __init__(self, rows):
        self._rows = list(rows)

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _RecordingConn:
    """Stands in for a DB connection: records statements, answers canned reads."""

    def __init__(self, handler=None):
        self.statements = []  # list of (sql, params)
        self.executemany_calls = []  # list of (sql, [params, ...])
        self.events = []  # ordered event log ("execute", "commit", ...)
        self.committed = False
        self.closed = False
        self._handler = handler or (lambda sql, params: [])

    def execute(self, sql, params=()):
        self.statements.append((sql, params))
        self.events.append("execute")
        return _Rows(self._handler(sql, params))

    def executemany(self, sql, seq):
        seq = list(seq)
        self.executemany_calls.append((sql, seq))
        self.events.append("executemany")
        return None

    def commit(self):
        self.events.append("commit")
        self.committed = True

    def rollback(self):
        self.events.append("rollback")

    def close(self):
        self.events.append("close")
        self.closed = True


def _postgres_env(monkeypatch):
    monkeypatch.setenv("SHIFTWISE_DB_ENGINE", "postgres")
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@localhost:5432/testdb")


# ---------------------------------------------------------------------------
# init_db() per-engine dispatch
# ---------------------------------------------------------------------------


def test_init_db_postgres_applies_schema_and_bootstraps(monkeypatch):
    _postgres_env(monkeypatch)
    monkeypatch.setenv("SHIFTWISE_BOOTSTRAP_MANAGER_PASSWORD", "long-enough-password")
    conn = _RecordingConn()
    monkeypatch.setattr(sdb, "db", lambda: conn)

    sdb.init_db()

    creates = [sql for sql, _ in conn.statements if sql.startswith("CREATE TABLE IF NOT EXISTS")]
    assert [c.split()[5] for c in creates] == list(TABLES)
    inserts = [sql for sql, _ in conn.statements if sql.startswith("INSERT INTO users")]
    assert len(inserts) == 1
    assert conn.committed and conn.closed


def test_init_db_postgres_seed_demo_inserts_demo_week(monkeypatch):
    _postgres_env(monkeypatch)
    conn = _RecordingConn()
    monkeypatch.setattr(sdb, "db", lambda: conn)

    sdb.init_db(seed_demo=True)

    by_table = {}
    for sql, rows in conn.executemany_calls:
        table = sql.split()[2]
        by_table.setdefault(table, []).extend(rows)
    assert len(by_table["users"]) == 7
    assert len(by_table["shifts"]) == 14
    assert conn.committed and conn.closed


def test_init_db_postgres_rejects_short_bootstrap_password(monkeypatch):
    _postgres_env(monkeypatch)
    monkeypatch.setenv("SHIFTWISE_BOOTSTRAP_MANAGER_PASSWORD", "short")
    conn = _RecordingConn()
    monkeypatch.setattr(sdb, "db", lambda: conn)
    with pytest.raises(RuntimeError, match="SHIFTWISE_BOOTSTRAP_MANAGER_PASSWORD"):
        sdb.init_db()


def test_init_db_postgres_commits_schema_before_mock_seed_handoff(monkeypatch):
    """Regression test for the review's Blocker 1.

    _seed_if_empty closes the init lease and mock_seed.seed() opens its own
    pooled connection; the DDL commit must land before that handoff or the
    pool rolls the uncommitted schema back (UndefinedTable on the first
    DELETE FROM). The recording facade is faithful here: commit/close are
    real ordered events, not no-ops.
    """
    _postgres_env(monkeypatch)
    conn = _RecordingConn()
    monkeypatch.setattr(sdb, "db", lambda: conn)
    seed_calls = []
    monkeypatch.setattr(
        mock_seed,
        "seed",
        lambda appmod, force=False: (
            conn.events.append("mock_seed.seed"),
            seed_calls.append(force),
        ),
    )

    sdb.init_db(mock_roster=True)

    # The PG path applies exactly POSTGRES_SCHEMA (statement-split), not SCHEMA.
    creates = [sql for sql, _ in conn.statements if sql.startswith("CREATE TABLE IF NOT EXISTS")]
    assert creates == sdb._split_ddl(sdb.POSTGRES_SCHEMA)
    assert not any("INTEGER PRIMARY KEY" in sql for sql in creates)
    # The schema commit lands before mock_seed opens its own connection.
    assert "mock_seed.seed" in conn.events
    assert conn.events.index("commit") < conn.events.index("mock_seed.seed")
    # The demo-seed handoff is an intended wipe: force=True must be passed.
    assert seed_calls == [True]


def test_init_db_postgres_rolls_back_and_returns_lease_on_ddl_failure(monkeypatch):
    """_init_db_postgres must not leak the pooled lease on failure."""
    _postgres_env(monkeypatch)

    class _FailingConn(_RecordingConn):
        def execute(self, sql, params=()):
            if sql.startswith("CREATE TABLE"):
                raise RuntimeError("simulated DDL failure")
            return super().execute(sql, params)

    conn = _FailingConn()
    monkeypatch.setattr(sdb, "db", lambda: conn)
    with pytest.raises(RuntimeError, match="simulated DDL failure"):
        sdb.init_db()
    assert "rollback" in conn.events
    assert conn.closed


def test_postgres_pool_sets_defensive_timeouts(monkeypatch):
    """The pool must bound runaway statements and lock waits server-side."""
    import psycopg_pool

    captured = {}

    class _FakePool:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def open(self, wait=True):
            pass

    monkeypatch.setattr(psycopg_pool, "ConnectionPool", _FakePool)
    monkeypatch.setattr(sdb, "_postgres_pool", None)
    monkeypatch.setattr(sdb, "_postgres_pool_url", None)

    sdb._postgres_pool_for("postgresql://u:p@localhost:5432/db")

    assert captured["kwargs"]["statement_timeout"] == "30s"
    assert captured["kwargs"]["lock_timeout"] == "5s"


# ---------------------------------------------------------------------------
# mock_seed through the real psycopg facade (translation + row adaptation)
# ---------------------------------------------------------------------------


class _StubColumn:
    def __init__(self, name):
        self.name = name


class _PsycopgStubCursor:
    """Pretends to be a psycopg cursor: records translated SQL, serves a tiny store."""

    def __init__(self, store):
        self.store = store
        self.queries = []  # (translated_sql, params)
        self.description = None
        self._rows = []
        self._pos = 0

    def _columns_of(self, query):
        start = query.index("(") + 1
        end = query.index(")", start)
        return [c.strip() for c in query[start:end].split(",")]

    def _set(self, rows, columns):
        self._rows = list(rows)
        self._pos = 0
        self.description = [_StubColumn(c) for c in columns]

    def execute(self, query, params=()):
        self.queries.append((query, params))
        store = self.store
        if query.startswith("DELETE FROM"):
            store[query.split()[2]] = []
            self._set([], [])
        elif query.startswith("INSERT INTO users"):
            row = dict(zip(self._columns_of(query), params))
            row["id"] = len(store["users"]) + 1
            store["users"].append(row)
            self._set([], [])
        elif query.startswith("UPDATE users SET password="):
            for row in store["users"]:
                if row["username"] == params[1]:
                    row["password"] = params[0]
            self._set([], [])
        elif query.startswith("INSERT INTO shifts"):
            row = dict(zip(self._columns_of(query), params))
            row["id"] = len(store["shifts"]) + 1
            store["shifts"].append(row)
            self._set([], [])
        elif query.startswith("INSERT INTO picks"):
            row = dict(zip(self._columns_of(query), params))
            row["id"] = len(store["picks"]) + 1
            store["picks"].append(row)
            self._set([], [])
        elif query == "SELECT id, day, area, start_time FROM shifts":
            # Faithful to PostgreSQL, which adapts TIME to datetime.time
            # (SQLite returns TEXT). The seed must normalize these to HH:MM;
            # a stub that fabricates strings would hide that bug.
            self._set(
                [
                    (r["id"], r["day"], r["area"], dt_time.fromisoformat(r["start_time"]))
                    for r in store["shifts"]
                ],
                ["id", "day", "area", "start_time"],
            )
        elif query == "SELECT 1 FROM users LIMIT 1":
            # R6 force guard probe: empty store -> guard passes.
            self._set([(1,)] if store["users"] else [], ["1"])
        elif query == "SELECT id, station FROM users WHERE username=%s":
            match = [r for r in store["users"] if r["username"] == params[0]]
            self._set([(m["id"], m["station"]) for m in match], ["id", "station"])
        elif query == "SELECT COUNT(*) c FROM users WHERE role='employee'":
            self._set([(sum(1 for r in store["users"] if r["role"] == "employee"),)], ["c"])
        elif query == "SELECT COUNT(DISTINCT day) c FROM shifts":
            self._set([(len({r["day"] for r in store["shifts"]}),)], ["c"])
        elif query == "SELECT COUNT(*) c FROM picks":
            self._set([(len(store["picks"]),)], ["c"])
        else:
            raise AssertionError(f"unexpected query in stub: {query!r}")
        return self

    def executemany(self, query, seq):
        for params in seq:
            self.execute(query, params)
        return self

    def fetchone(self):
        if self._pos >= len(self._rows):
            return None
        row = self._rows[self._pos]
        self._pos += 1
        return row

    def fetchall(self):
        rows = self._rows[self._pos :]
        self._pos = len(self._rows)
        return rows


class _FacadeConn:
    """Minimal connection driving seed() through the real _PostgresCursor."""

    def __init__(self, stub):
        self._cursor = sdb._PostgresCursor(stub)
        self.stub = stub

    def execute(self, sql, params=()):
        return self._cursor.execute(sql, params)

    def executemany(self, sql, seq):
        return self._cursor.executemany(sql, seq)

    def commit(self):
        pass

    def close(self):
        pass


def test_mock_seed_runs_cleanly_through_postgres_facade():
    store = {"users": [], "shifts": [], "picks": []}
    stub = _PsycopgStubCursor(store)
    appmod_stub = SimpleNamespace(
        db=lambda: _FacadeConn(stub),
        generate_password_hash=lambda username: f"hash:{username}",
    )

    n, days, picks = mock_seed.seed(appmod_stub)

    assert (n, days, picks) == (14, 7, 14 * 21)
    # Every statement seed() issued survived qmark->%s translation: no raw
    # qmarks remain and placeholders are present where params were bound.
    assert stub.queries, "seed() issued no statements"
    for sql, params in stub.queries:
        assert "?" not in sql, f"untranslated qmark in {sql!r}"
    assert any("%s" in sql and params for sql, params in stub.queries)
    # Spot-check translated statements.
    # R6: the first statement is now the force-guard emptiness probe.
    assert stub.queries[0][0] == "SELECT 1 FROM users LIMIT 1"
    assert stub.queries[1][0] == "DELETE FROM picks"
    assert any(
        q.startswith(
            "INSERT INTO users (username, password, name, role, "
            "weekly_hours, employment_type, hired_on, station) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)"
        )
        for q, _ in stub.queries
    )


# ---------------------------------------------------------------------------
# Delete routes: manual cascades only on SQLite, DB cascades on PostgreSQL
# ---------------------------------------------------------------------------


def _auth_handler(sql, params):
    if sql.startswith("SELECT role FROM users"):
        return [{"id": 1, "role": "manager"}]
    return []


def _login_and_call(monkeypatch, routes_mod, route_fn, path, form=None, handler=None):
    conn = _RecordingConn(handler=handler or _auth_handler)
    monkeypatch.setattr(routes_mod, "db", lambda: conn)
    monkeypatch.setattr(auth_mod, "db", lambda: conn)
    monkeypatch.setattr(routes_mod, "run_scheduler", lambda week: None)
    with appmod.app.test_request_context(path, method="POST", data=form or {}):
        session["uid"] = 1
        session["role"] = "manager"
        response = route_fn()
    return conn, response


def test_delete_shift_sqlite_keeps_manual_cascades(monkeypatch):
    def handler(sql, params):
        rows = _auth_handler(sql, params)
        if sql.startswith("SELECT week_start FROM shifts"):
            rows = [{"week_start": "2026-09-28"}]
        return rows

    monkeypatch.setattr(manager_routes, "database_engine", lambda: "sqlite")
    conn, _ = _login_and_call(
        monkeypatch,
        manager_routes,
        lambda: manager_routes.delete_shift(9),
        "/manager/shift/delete/9",
        handler=handler,
    )
    sqls = [sql for sql, _ in conn.statements]
    assert any("DELETE FROM picks WHERE shift_id=?" in s for s in sqls)
    assert any("DELETE FROM coverage_preferences WHERE shift_id=?" in s for s in sqls)
    assert any("DELETE FROM assignments WHERE shift_id=?" in s for s in sqls)
    assert any(s.startswith("DELETE FROM shifts WHERE id=?") for s in sqls)


def test_delete_shift_postgres_relies_on_db_cascade(monkeypatch):
    def handler(sql, params):
        rows = _auth_handler(sql, params)
        if sql.startswith("SELECT week_start FROM shifts"):
            rows = [{"week_start": "2026-09-28"}]
        return rows

    monkeypatch.setattr(manager_routes, "database_engine", lambda: "postgres")
    conn, _ = _login_and_call(
        monkeypatch,
        manager_routes,
        lambda: manager_routes.delete_shift(9),
        "/manager/shift/delete/9",
        handler=handler,
    )
    sqls = [sql for sql, _ in conn.statements]
    # Request supersede still runs first on both engines (it is a status
    # update, not a cascade, and must precede the SET NULL on shift delete).
    assert any("UPDATE requests SET status='superseded'" in s for s in sqls)
    assert any(s.startswith("DELETE FROM shifts WHERE id=?") for s in sqls)
    # Dependent picks/coverage/assignments are left for ON DELETE CASCADE.
    assert not any("DELETE FROM picks" in s for s in sqls)
    assert not any("DELETE FROM coverage_preferences" in s for s in sqls)
    assert not any("DELETE FROM assignments" in s for s in sqls)


def _roster_handler(sql, params):
    rows = _auth_handler(sql, params)
    if sql.startswith("SELECT id FROM users WHERE id=?"):
        rows = [{"id": 5}]
    return rows


def test_delete_employee_sqlite_keeps_manual_cascades(monkeypatch):
    monkeypatch.setattr(roster_routes, "database_engine", lambda: "sqlite")
    conn, _ = _login_and_call(
        monkeypatch,
        roster_routes,
        lambda: roster_routes.delete_employee(5),
        "/manager/roster/5/delete",
        form={"confirm": "yes"},
        handler=_roster_handler,
    )
    sqls = [sql for sql, _ in conn.statements]
    for table in ("assignments", "picks", "coverage_preferences", "requests", "notifications"):
        assert any(f"DELETE FROM {table} WHERE user_id=?" in s for s in sqls), table
    assert any("DELETE FROM requests WHERE target_user_id=?" in s for s in sqls)
    assert any("DELETE FROM users WHERE id=? AND role='employee'" in s for s in sqls)


def test_delete_employee_postgres_relies_on_db_cascade(monkeypatch):
    monkeypatch.setattr(roster_routes, "database_engine", lambda: "postgres")
    conn, _ = _login_and_call(
        monkeypatch,
        roster_routes,
        lambda: roster_routes.delete_employee(5),
        "/manager/roster/5/delete",
        form={"confirm": "yes"},
        handler=_roster_handler,
    )
    sqls = [sql for sql, _ in conn.statements]
    # user_id dependents are left for ON DELETE CASCADE ...
    for table in ("assignments", "picks", "coverage_preferences", "notifications"):
        assert not any(f"DELETE FROM {table} WHERE user_id=?" in s for s in sqls), table
    assert not any("DELETE FROM requests WHERE user_id=?" in s for s in sqls)
    # ... but invitee-side swap requests are still cancelled explicitly because
    # the schema only SET NULLs requests.target_user_id.
    assert any("DELETE FROM requests WHERE target_user_id=?" in s for s in sqls)
    assert any("DELETE FROM users WHERE id=? AND role='employee'" in s for s in sqls)
