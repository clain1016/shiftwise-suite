from datetime import date

import app as appmod


def test_manager_denial_reason_is_saved_and_sent_to_employee(isolated_db):
    appmod.init_db(seed_demo=True)
    client = appmod.app.test_client()
    client.post("/login", data={"username": "alex", "password": "alex"})
    client.post("/request/day_off", data={"day": "Tue"})

    week = appmod.monday_of(date.today()).isoformat()
    conn = appmod.db()
    alex_id = conn.execute("SELECT id FROM users WHERE username='alex'").fetchone()["id"]
    request_id = conn.execute(
        "SELECT id FROM requests WHERE user_id=? AND kind='day_off' AND week_start=?",
        (alex_id, week),
    ).fetchone()["id"]
    conn.close()

    client.post("/logout")
    client.post("/login", data={"username": "manager", "password": "manager"})
    response = client.post(
        f"/manager/requests/{request_id}/deny",
        data={"reason": "Coverage is already at the minimum."},
        follow_redirects=True,
    )
    assert response.status_code == 200
    conn = appmod.db()
    saved = conn.execute(
        "SELECT status, reason FROM requests WHERE id=?", (request_id,)
    ).fetchone()
    notification = conn.execute(
        "SELECT message FROM notifications WHERE user_id=? AND kind='conflict' "
        "ORDER BY id DESC LIMIT 1", (alex_id,),
    ).fetchone()
    conn.close()
    assert saved["status"] == "denied"
    assert saved["reason"] == "Coverage is already at the minimum."
    assert "Coverage is already at the minimum." in notification["message"]
