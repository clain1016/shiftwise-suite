from datetime import date

import app as appmod


def _setup_swap_pair():
    appmod.init_db(seed_demo=True)
    week = appmod.monday_of(date.today()).isoformat()
    conn = appmod.db()
    users = {
        row["username"]: row["id"]
        for row in conn.execute(
            "SELECT id, username FROM users WHERE username IN ('alex','sam','taylor')"
        )
    }
    shift_ids = []
    for start, end in (("01:00", "03:00"), ("03:00", "05:00")):
        cur = conn.execute(
            "INSERT INTO shifts (week_start, day, start_time, end_time, slots, area) "
            "VALUES (?, 'Mon', ?, ?, 1, 'front')", (week, start, end)
        )
        shift_ids.append(cur.lastrowid)
    conn.executemany(
        "INSERT INTO assignments (shift_id, user_id, status) VALUES (?, ?, 'notified')",
        [(shift_ids[0], users["alex"]), (shift_ids[1], users["sam"])],
    )
    conn.commit()
    conn.close()
    return users, shift_ids


def _login(client, username):
    response = client.post("/login", data={"username": username, "password": username})
    assert response.status_code == 302


def test_employee_can_request_and_accept_an_in_house_shift_swap(isolated_db):
    users, (alex_shift, sam_shift) = _setup_swap_pair()
    client = appmod.app.test_client()
    _login(client, "alex")

    response = client.post("/request/swap", data={
        "shift_id": alex_shift,
        "target_user_id": users["sam"],
        "target_shift_id": sam_shift,
    }, follow_redirects=True)
    assert b"Swap request sent to Sam Chen" in response.data

    conn = appmod.db()
    request_row = conn.execute(
        "SELECT * FROM requests WHERE kind='swap' AND target_user_id=? ORDER BY id DESC LIMIT 1",
        (users["sam"],),
    ).fetchone()
    assert request_row and request_row["status"] == "approved"
    conn.close()

    client.post("/logout")
    _login(client, "sam")
    history = client.get("/my-requests")
    assert b"pending" in history.data
    assert b"Mon 01:00-03:00" in history.data
    response = client.post(
        f"/request/{request_row['id']}/respond", data={"decision": "accept"},
        follow_redirects=True,
    )
    assert b"Swap accepted" in response.data

    conn = appmod.db()
    assert conn.execute(
        "SELECT user_id FROM assignments WHERE shift_id=?", (alex_shift,)
    ).fetchone()["user_id"] == users["sam"]
    assert conn.execute(
        "SELECT user_id FROM assignments WHERE shift_id=?", (sam_shift,)
    ).fetchone()["user_id"] == users["alex"]
    request_row = conn.execute(
        "SELECT status, reason FROM requests WHERE id=?", (request_row["id"],)
    ).fetchone()
    conn.close()
    assert request_row["status"] == "approved_ok"
    assert "Accepted" in request_row["reason"]


def test_swap_request_is_confined_to_target_employee_and_records_rejection_reason(isolated_db):
    users, (alex_shift, sam_shift) = _setup_swap_pair()
    alex = appmod.app.test_client()
    _login(alex, "alex")
    alex.post("/request/swap", data={
        "shift_id": alex_shift,
        "target_user_id": users["sam"],
        "target_shift_id": sam_shift,
    })
    conn = appmod.db()
    request_id = conn.execute(
        "SELECT id FROM requests WHERE kind='swap' AND target_user_id=? ORDER BY id DESC LIMIT 1",
        (users["sam"],),
    ).fetchone()["id"]
    conn.close()

    manager = appmod.app.test_client()
    _login(manager, "manager")
    manager_requests = manager.get("/manager/requests")
    assert b"Sam Chen" not in manager_requests.data

    other_employee = appmod.app.test_client()
    _login(other_employee, "taylor")
    denied = other_employee.post(
        f"/request/{request_id}/respond", data={"decision": "reject", "reason": "No"}
    )
    assert denied.status_code == 404

    sam = appmod.app.test_client()
    _login(sam, "sam")
    missing_reason = sam.post(
        f"/request/{request_id}/respond", data={"decision": "reject"},
        follow_redirects=True,
    )
    assert b"include a reason" in missing_reason.data
    response = sam.post(
        f"/request/{request_id}/respond",
        data={"decision": "reject", "reason": "I need that shift."},
        follow_redirects=True,
    )
    assert b"I need that shift." in response.data
    conn = appmod.db()
    request_row = conn.execute(
        "SELECT status, reason FROM requests WHERE id=?", (request_id,)
    ).fetchone()
    conn.close()
    assert request_row["status"] == "denied"
    assert request_row["reason"] == "I need that shift."
