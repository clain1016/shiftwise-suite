from pathlib import Path
import sys
from datetime import datetime

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import app as appmod
from test_support import isolate_database


def test_approve_dispatch_vacation_and_swap():
    """R3 regression: vacation and swap approve handlers via the real endpoint.

    The big request-flow tests cover day_off and switch approval; this pins
    the two remaining dispatch entries (vacation, swap) after the if/elif ->
    dict refactor. Pure behavior check: same statuses and flash messages.
    """
    _test_db = isolate_database(appmod)
    try:
        DB = appmod.DB_PATH
        if DB.exists():
            DB.unlink()
        appmod.init_db(seed_demo=True)
        client = appmod.app.test_client()
        r = client.post("/login", data={"username": "manager", "password": "manager"},
                        follow_redirects=True)
        assert b"Log out" in r.data, "manager login failed"

        conn = appmod.db()
        uid = conn.execute("SELECT id FROM users WHERE role='employee' LIMIT 1").fetchone()[0]
        now = datetime.now().isoformat(timespec="seconds")
        # Vacation over a range with no shifts -> no coverage gaps -> approved.
        vid = conn.execute(
            "INSERT INTO requests (user_id, kind, vacation_start, vacation_end, "
            "status, created_at) VALUES (?, 'vacation', '2030-01-06', '2030-01-12', "
            "'approved', ?)", (uid, now)).lastrowid
        # Swap with no swap_requested assignment -> no eligible coverer path.
        shift_id = conn.execute("SELECT id FROM shifts LIMIT 1").fetchone()[0]
        sid = conn.execute(
            "INSERT INTO requests (user_id, kind, shift_id, status, created_at) "
            "VALUES (?, 'swap', ?, 'approved', ?)", (uid, shift_id, now)).lastrowid
        conn.commit()
        conn.close()

        r = client.post(f"/manager/requests/{vid}/approve", follow_redirects=True)
        assert b"Request approved." in r.data, "vacation approve flash missing"
        conn = appmod.db()
        st = conn.execute("SELECT status FROM requests WHERE id=?", (vid,)).fetchone()[0]
        conn.close()
        assert st == "approved_ok", f"vacation status: {st}"

        r = client.post(f"/manager/requests/{sid}/approve", follow_redirects=True)
        assert b"No eligible coverer is available for that swap." in r.data, \
            "swap no-cover flash missing"

        print("vacation + swap approve dispatch: OK")
    finally:
        _test_db.cleanup()


if __name__ == "__main__":
    test_approve_dispatch_vacation_and_swap()
