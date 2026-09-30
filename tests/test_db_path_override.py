from pathlib import Path


def test_database_connection_uses_app_db_path_override(isolated_db):
    import app as appmod

    expected = Path(appmod.DB_PATH).resolve()
    conn = appmod.db()
    actual = Path(conn.execute("PRAGMA database_list").fetchone()["file"]).resolve()
    conn.close()

    assert actual == expected
