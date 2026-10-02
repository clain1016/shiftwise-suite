from datetime import date
from pathlib import Path
from uuid import uuid4

import app as appmod
from shiftwise.presentation import format_clock


def _login(client, username="alex"):
    return client.post("/login", data={"username": username, "password": username})


def _init_demo(appmod):
    db_path = Path(appmod.DB_PATH)
    assert db_path.parent.name.startswith("shiftwise-test-")
    for path in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
        if path.exists():
            path.unlink()
    appmod.init_db(seed_demo=True)
    conn = appmod.db()
    conn.execute("DELETE FROM login_attempts")
    for user in conn.execute("SELECT id, username FROM users").fetchall():
        conn.execute(
            "UPDATE users SET password=?, time_format='24h' WHERE id=?",
            (appmod.generate_password_hash(user["username"]), user["id"]),
        )
    conn.commit()
    conn.close()


def test_clock_format_defaults_to_existing_24_hour_display(isolated_db):
    _init_demo(appmod)
    username = f"clocktest_{uuid4().hex}"
    conn = appmod.db()
    conn.execute(
        "INSERT INTO users (username, password, name, role, station) VALUES (?,?,?,?,?)",
        (username, appmod.generate_password_hash(username), "Clock Test", "employee", "front"),
    )
    conn.execute(
        "INSERT INTO shifts (week_start, day, start_time, end_time, slots, area) "
        "VALUES (?, 'Mon', '13:05', '14:00', 1, 'front')",
        (appmod.monday_of(date.today()).isoformat(),),
    )
    conn.commit()
    conn.close()
    client = appmod.app.test_client()
    _login(client, username)

    response = client.get("/")
    conn = appmod.db()
    row = conn.execute("SELECT time_format FROM users WHERE username=?", (username,)).fetchone()
    conn.close()

    assert row["time_format"] == "24h"
    assert b"13:05" in response.data
    assert b"1:05 PM" not in response.data


def test_user_can_save_12_hour_clock_format(isolated_db):
    _init_demo(appmod)
    conn = appmod.db()
    conn.execute(
        "INSERT INTO shifts (week_start, day, start_time, end_time, slots, area) "
        "VALUES (?, 'Mon', '13:05', '14:00', 1, 'front')",
        (appmod.monday_of(date.today()).isoformat(),),
    )
    conn.commit()
    conn.close()
    client = appmod.app.test_client()
    login = _login(client)

    assert login.status_code == 302, login.data

    response = client.post("/settings", data={"time_format": "12h"}, follow_redirects=True)

    conn = appmod.db()
    row = conn.execute("SELECT time_format FROM users WHERE username='alex'").fetchone()
    conn.close()
    assert response.status_code == 200
    assert row["time_format"] == "12h"
    dashboard = client.get("/")
    calendar = client.get("/calendar")
    assert dashboard.status_code == 200 and calendar.status_code == 200
    assert b"1:05 PM" in dashboard.data
    assert b"1:05 PM" in calendar.data


def test_invalid_clock_format_does_not_change_saved_preference(isolated_db):
    _init_demo(appmod)
    client = appmod.app.test_client()
    _login(client)
    client.post("/settings", data={"time_format": "12h"}, follow_redirects=True)

    response = client.post("/settings", data={"time_format": "wall-clock"}, follow_redirects=True)

    conn = appmod.db()
    row = conn.execute("SELECT time_format FROM users WHERE username='alex'").fetchone()
    conn.close()
    assert response.status_code == 200
    assert row["time_format"] == "12h"
    assert b"Choose either 12-hour or 24-hour time" in response.data


def test_clock_formatter_handles_noon_midnight_and_afternoon(isolated_db):
    _init_demo(appmod)
    client = appmod.app.test_client()
    _login(client)
    conn = appmod.db()
    conn.execute("UPDATE users SET time_format='12h' WHERE username='alex'")
    conn.execute(
        "INSERT INTO shifts (week_start, day, start_time, end_time, slots, area) "
        "VALUES (?, 'Sun', '00:00', '12:00', 1, 'front')",
        (appmod.monday_of(date.today()).isoformat(),),
    )
    conn.commit()
    conn.close()

    page = client.get("/").data
    assert b"12:00 AM" in page
    assert b"12:00 PM" in page

    assert format_clock("13:05", "24h") == "13:05"
    assert format_clock("not-a-time", "12h") == "not-a-time"


def test_settings_are_available_from_dashboard_navigation(isolated_db):
    _init_demo(appmod)
    client = appmod.app.test_client()
    _login(client)

    response = client.get("/settings")

    assert response.status_code == 200
    assert b"Clock format" in response.data
    assert b"12-hour" in response.data and b"24-hour" in response.data


def test_general_settings_page_contains_account_controls(isolated_db):
    _init_demo(appmod)
    client = appmod.app.test_client()
    _login(client)

    response = client.get("/settings")

    assert response.status_code == 200
    for label in (b"Display name", b"Username", b"Email", b"Current password", b"New password"):
        assert label in response.data


def test_user_can_update_profile_from_general_settings(isolated_db):
    _init_demo(appmod)
    client = appmod.app.test_client()
    _login(client)

    response = client.post(
        "/settings",
        data={
            "action": "profile",
            "name": "Alex Updated",
            "username": "alex_updated",
            "email": "alex.updated@example.test",
        },
        follow_redirects=True,
    )

    conn = appmod.db()
    row = conn.execute(
        "SELECT name, username, email FROM users WHERE id=(SELECT id FROM users "
        "WHERE username='alex_updated')"
    ).fetchone()
    conn.close()
    assert response.status_code == 200
    assert row is not None
    assert tuple(row) == ("Alex Updated", "alex_updated", "alex.updated@example.test")
    assert b"Alex Updated" in response.data
    assert client.get("/settings").status_code == 200
    client.get("/logout")
    assert (
        client.post(
            "/login",
            data={
                "username": "alex_updated",
                "password": "alex",
            },
        ).status_code
        == 302
    )


def test_profile_rejects_duplicate_username_and_invalid_email(isolated_db):
    _init_demo(appmod)
    client = appmod.app.test_client()
    _login(client)

    duplicate = client.post(
        "/settings",
        data={
            "action": "profile",
            "name": "Alex Rivera",
            "username": "sam",
            "email": "alex@example.test",
        },
        follow_redirects=True,
    )
    invalid_email = client.post(
        "/settings",
        data={
            "action": "profile",
            "name": "Alex Rivera",
            "username": "alex",
            "email": "not-an-email",
        },
        follow_redirects=True,
    )

    conn = appmod.db()
    row = conn.execute("SELECT name, username, email FROM users WHERE username='alex'").fetchone()
    conn.close()
    assert b"already in use" in duplicate.data
    assert b"valid email" in invalid_email.data
    assert tuple(row) == ("Alex Rivera", "alex", None)


def test_profile_can_clear_optional_email(isolated_db):
    _init_demo(appmod)
    conn = appmod.db()
    conn.execute("UPDATE users SET email='alex@example.test' WHERE username='alex'")
    conn.commit()
    conn.close()
    client = appmod.app.test_client()
    _login(client)

    response = client.post(
        "/settings",
        data={
            "action": "profile",
            "name": "Alex Rivera",
            "username": "alex",
            "email": "",
        },
        follow_redirects=True,
    )

    conn = appmod.db()
    email = conn.execute("SELECT email FROM users WHERE username='alex'").fetchone()["email"]
    conn.close()
    assert response.status_code == 200
    assert email is None


def test_user_can_change_password_from_general_settings(isolated_db):
    _init_demo(appmod)
    client = appmod.app.test_client()
    _login(client)

    response = client.post(
        "/settings",
        data={
            "action": "password",
            "current_password": "alex",
            "new_password": "a-new-secure-password",
        },
        follow_redirects=True,
    )

    conn = appmod.db()
    password = conn.execute("SELECT password FROM users WHERE username='alex'").fetchone()[
        "password"
    ]
    conn.close()
    assert response.status_code == 200
    assert b"Password changed" in response.data
    assert password != "a-new-secure-password"
    assert client.get("/settings").status_code == 302
    old_login = client.post("/login", data={"username": "alex", "password": "alex"})
    assert b"Wrong username or password" in old_login.data
    assert (
        client.post(
            "/login",
            data={
                "username": "alex",
                "password": "a-new-secure-password",
            },
        ).status_code
        == 302
    )


def test_password_change_rejects_wrong_current_and_short_new_password(isolated_db):
    _init_demo(appmod)
    client = appmod.app.test_client()
    _login(client)

    wrong_current = client.post(
        "/settings",
        data={
            "action": "password",
            "current_password": "wrong",
            "new_password": "a-new-secure-password",
        },
        follow_redirects=True,
    )
    short_password = client.post(
        "/settings",
        data={
            "action": "password",
            "current_password": "alex",
            "new_password": "short",
        },
        follow_redirects=True,
    )

    conn = appmod.db()
    password = conn.execute("SELECT password FROM users WHERE username='alex'").fetchone()[
        "password"
    ]
    conn.close()
    assert b"Current password is incorrect" in wrong_current.data
    assert b"at least 12 characters" in short_password.data
    assert appmod.check_password_hash(password, "alex")


def test_settings_is_last_in_desktop_and_mobile_navigation(isolated_db):
    _init_demo(appmod)
    client = appmod.app.test_client()
    _login(client)

    response = client.get("/settings")
    html = response.data.decode()
    desktop_links = html.split('<div class="links">', 1)[1].split("</div>", 1)[0]
    mobile_menu = html.split('<div class="mobile-menu" id="mobileMenu">', 1)[1].split(
        "<script>", 1
    )[0]
    last_mobile_link = mobile_menu[mobile_menu.rfind("<a ") :].split("</a>", 1)[0]

    assert desktop_links.rfind("Settings") > desktop_links.rfind("My requests")
    assert "Settings" in last_mobile_link

    client.get("/logout")
    client.post("/login", data={"username": "manager", "password": "manager"})
    manager_html = client.get("/settings").data.decode()
    manager_desktop = manager_html.split('<div class="links">', 1)[1].split("</div>", 1)[0]
    manager_mobile = manager_html.split('<div class="mobile-menu" id="mobileMenu">', 1)[1].split(
        "<script>", 1
    )[0]
    manager_last_mobile_link = manager_mobile[manager_mobile.rfind("<a ") :].split("</a>", 1)[0]
    assert manager_desktop.rfind("Settings") > manager_desktop.rfind("Roster")
    assert "Settings" in manager_last_mobile_link
