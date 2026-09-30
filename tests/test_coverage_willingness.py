from datetime import date

import app as appmod


def test_employee_coverage_willingness_is_saved_and_honored(isolated_db):
    appmod.init_db(seed_demo=True)
    client = appmod.app.test_client()
    response = client.post("/login", data={"username": "alex", "password": "alex"})
    assert response.status_code == 302

    week = appmod.monday_of(date.today()).isoformat()
    conn = appmod.db()
    shift_ids = [row["id"] for row in conn.execute(
        "SELECT id FROM shifts WHERE week_start=? AND area='front' ORDER BY id", (week,)
    )]
    alex_id = conn.execute("SELECT id FROM users WHERE username='alex'").fetchone()["id"]
    conn.execute("UPDATE users SET station='back' WHERE username IN ('sam','taylor')")
    conn.commit()
    conn.close()
    assert len(shift_ids) >= 2

    form = {}
    for rank, shift_id in enumerate(shift_ids, 1):
        form[f"rank_{shift_id}"] = str(rank)
        form[f"cover_{shift_id}"] = "yes" if rank == 1 else "no"
    response = client.post("/pick", data=form, follow_redirects=True)
    assert b"Preferences saved" in response.data

    conn = appmod.db()
    saved = conn.execute(
        "SELECT willing FROM coverage_preferences WHERE user_id=? AND shift_id=?",
        (alex_id, shift_ids[0]),
    ).fetchone()
    refused = conn.execute(
        "SELECT willing FROM coverage_preferences WHERE user_id=? AND shift_id=?",
        (alex_id, shift_ids[1]),
    ).fetchone()
    assert saved and saved["willing"] == 1
    assert refused and refused["willing"] == 0
    conn.execute("DELETE FROM assignments")
    conn.commit()
    assert appmod.coverage_plan(conn, week, shift_ids[1], out_uid=-1) is None
    assert appmod.coverage_plan(conn, week, shift_ids[0], out_uid=-1) == alex_id
    conn.close()

    page = client.get("/")
    assert b"Willing to cover" in page.data
    assert b">Yes<" in page.data and b">No<" in page.data
