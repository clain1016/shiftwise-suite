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
            "VALUES (?, 'Mon', ?, ?, 1, 'front')",
            (week, start, end),
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

    response = client.post(
        "/request/swap",
        data={
            "shift_id": alex_shift,
            "target_user_id": users["sam"],
            "target_shift_id": sam_shift,
        },
        follow_redirects=True,
    )
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
        f"/request/{request_row['id']}/respond",
        data={"decision": "accept"},
        follow_redirects=True,
    )
    assert b"Swap accepted" in response.data

    conn = appmod.db()
    assert (
        conn.execute("SELECT user_id FROM assignments WHERE shift_id=?", (alex_shift,)).fetchone()[
            "user_id"
        ]
        == users["sam"]
    )
    assert (
        conn.execute("SELECT user_id FROM assignments WHERE shift_id=?", (sam_shift,)).fetchone()[
            "user_id"
        ]
        == users["alex"]
    )
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
    alex.post(
        "/request/swap",
        data={
            "shift_id": alex_shift,
            "target_user_id": users["sam"],
            "target_shift_id": sam_shift,
        },
    )
    conn = appmod.db()
    request_id = conn.execute(
        "SELECT id FROM requests WHERE kind='swap' AND target_user_id=? ORDER BY id DESC LIMIT 1",
        (users["sam"],),
    ).fetchone()["id"]
    conn.close()

    manager = appmod.app.test_client()
    _login(manager, "manager")
    manager_requests = manager.get("/manager/requests")
    # The manager's page must not expose an employee→coworker swap invitation
    # (manager.requests filters `NOT (r.kind='swap' AND r.target_user_id IS NOT NULL)`);
    # the same descriptor must still be visible to the requester, so this
    # assertion cannot pass by the string simply not being rendered anywhere.
    assert b"Mon 01:00-03:00" not in manager_requests.data
    assert b"Mon 01:00-03:00" in alex.get("/my-requests").data

    other_employee = appmod.app.test_client()
    _login(other_employee, "taylor")
    denied = other_employee.post(
        f"/request/{request_id}/respond", data={"decision": "reject", "reason": "No"}
    )
    assert denied.status_code == 404

    sam = appmod.app.test_client()
    _login(sam, "sam")
    missing_reason = sam.post(
        f"/request/{request_id}/respond",
        data={"decision": "reject"},
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


def test_pending_swap_invitation_survives_a_scheduler_rebuild(isolated_db):
    users, (alex_shift, sam_shift) = _setup_swap_pair()
    week = appmod.monday_of(date.today()).isoformat()
    # The demo seed fills the same week with a full slate of shifts; drop them so
    # the rebuild can only touch the pair under test. Otherwise the coverage
    # backfill hands the coworker's (auto-assigned 'notified') shift to a third
    # employee before the invited coworker can accept it.
    conn = appmod.db()
    conn.execute(
        "DELETE FROM shifts WHERE week_start=? AND id NOT IN (?,?)", (week, alex_shift, sam_shift)
    )
    conn.commit()
    conn.close()

    alex = appmod.app.test_client()
    _login(alex, "alex")
    response = alex.post(
        "/request/swap",
        data={
            "shift_id": alex_shift,
            "target_assignment": f"{users['sam']}:{sam_shift}",
        },
        follow_redirects=True,
    )
    assert b"Swap request sent to Sam Chen" in response.data

    conn = appmod.db()
    request_id = conn.execute(
        "SELECT id FROM requests WHERE kind='swap' AND shift_id=?", (alex_shift,)
    ).fetchone()["id"]
    source = conn.execute(
        "SELECT status FROM assignments WHERE shift_id=? AND user_id=?",
        (alex_shift, users["alex"]),
    ).fetchone()
    conn.close()
    assert source is not None
    assert source["status"] == "swap_invited"

    appmod.run_scheduler(week)

    conn = appmod.db()
    source = conn.execute(
        "SELECT status FROM assignments WHERE shift_id=? AND user_id=?",
        (alex_shift, users["alex"]),
    ).fetchone()
    counted_as_staff = conn.execute(
        "SELECT COUNT(*) FROM assignments WHERE shift_id=? AND user_id=? "
        "AND status NOT IN ('sick','swap_requested')",
        (alex_shift, users["alex"]),
    ).fetchone()[0]
    request_row = conn.execute("SELECT status FROM requests WHERE id=?", (request_id,)).fetchone()
    conn.close()
    assert source is not None
    assert source["status"] == "swap_invited"
    assert counted_as_staff == 1
    assert request_row["status"] == "approved"

    alex.get("/logout")
    sam = appmod.app.test_client()
    _login(sam, "sam")
    accepted = sam.post(
        f"/request/{request_id}/respond",
        data={"decision": "accept"},
        follow_redirects=True,
    )
    assert b"Swap accepted" in accepted.data

    conn = appmod.db()
    rows = {
        r["shift_id"]: r
        for r in conn.execute(
            "SELECT shift_id, user_id, status FROM assignments WHERE shift_id IN (?,?)",
            (alex_shift, sam_shift),
        )
    }
    request_row = conn.execute("SELECT status FROM requests WHERE id=?", (request_id,)).fetchone()
    conn.close()
    assert rows[alex_shift]["user_id"] == users["sam"]
    assert rows[alex_shift]["status"] == "switch_fixed"
    assert rows[sam_shift]["user_id"] == users["alex"]
    assert rows[sam_shift]["status"] == "switch_fixed"
    assert request_row["status"] == "approved_ok"


def test_declined_swap_invitation_returns_the_shift_to_the_requester(isolated_db):
    users, (alex_shift, sam_shift) = _setup_swap_pair()
    alex = appmod.app.test_client()
    _login(alex, "alex")
    alex.post(
        "/request/swap",
        data={
            "shift_id": alex_shift,
            "target_assignment": f"{users['sam']}:{sam_shift}",
        },
    )
    conn = appmod.db()
    request_id = conn.execute(
        "SELECT id FROM requests WHERE kind='swap' AND shift_id=?", (alex_shift,)
    ).fetchone()["id"]
    invited = conn.execute(
        "SELECT status FROM assignments WHERE shift_id=? AND user_id=?",
        (alex_shift, users["alex"]),
    ).fetchone()["status"]
    conn.close()
    assert invited == "swap_invited"

    sam = appmod.app.test_client()
    _login(sam, "sam")
    sam.post(
        f"/request/{request_id}/respond",
        data={"decision": "reject", "reason": "I need that shift."},
        follow_redirects=True,
    )

    conn = appmod.db()
    request_row = conn.execute("SELECT status FROM requests WHERE id=?", (request_id,)).fetchone()
    source = conn.execute(
        "SELECT status FROM assignments WHERE shift_id=? AND user_id=?",
        (alex_shift, users["alex"]),
    ).fetchone()
    conn.close()
    assert request_row["status"] == "denied"
    assert source["status"] == "confirmed"


def test_manager_placed_shifts_cannot_be_involved_in_employee_swaps(isolated_db):
    users, (alex_shift, sam_shift) = _setup_swap_pair()
    week = appmod.monday_of(date.today()).isoformat()
    conn = appmod.db()
    # A second, regularly-assigned shift keeps the swap form rendered so the
    # dropdown exclusion below is actually observable rather than vacuous.
    cur = conn.execute(
        "INSERT INTO shifts (week_start, day, start_time, end_time, slots, area) "
        "VALUES (?, 'Tue', '01:00', '03:00', 1, 'front')",
        (week,),
    )
    extra_shift = cur.lastrowid
    conn.execute(
        "INSERT INTO assignments (shift_id, user_id, status) VALUES (?, ?, 'confirmed')",
        (extra_shift, users["alex"]),
    )
    conn.execute("UPDATE assignments SET status='manager_fixed' WHERE shift_id=?", (alex_shift,))
    conn.commit()
    conn.close()

    alex = appmod.app.test_client()
    _login(alex, "alex")

    # (a) the manager-set shift is never offered as a swap source
    dashboard = alex.get("/")
    source_select = dashboard.data.split(b'id="swap-source"')[1].split(b"</select>")[0]
    assert f'value="{alex_shift}"'.encode() not in source_select
    assert f'value="{extra_shift}"'.encode() in source_select

    # (b) a crafted manager-set source is rejected and no request is recorded
    rejected = alex.post(
        "/request/swap",
        data={
            "shift_id": alex_shift,
            "target_assignment": f"{users['sam']}:{sam_shift}",
        },
        follow_redirects=True,
    )
    assert b"Choose valid shifts in the same week and house" in rejected.data
    conn = appmod.db()
    assert conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 0
    conn.close()

    # (c) the same for a manager-set target shift
    conn = appmod.db()
    conn.execute("UPDATE assignments SET status='notified' WHERE shift_id=?", (alex_shift,))
    conn.execute("UPDATE assignments SET status='manager_fixed' WHERE shift_id=?", (sam_shift,))
    conn.commit()
    conn.close()
    rejected = alex.post(
        "/request/swap",
        data={
            "shift_id": extra_shift,
            "target_assignment": f"{users['sam']}:{sam_shift}",
        },
        follow_redirects=True,
    )
    assert b"Choose valid shifts in the same week and house" in rejected.data
    conn = appmod.db()
    assert conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 0
    conn.close()
