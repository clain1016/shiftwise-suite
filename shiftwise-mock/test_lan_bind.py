"""The default mock listener must accept requests via the host's LAN address."""
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import tempfile
import time
from urllib.error import URLError
from urllib.request import urlopen

MOCK_DIR = Path(__file__).resolve().parent
PYTHON = MOCK_DIR.parent / ".venv" / "bin" / "python"

def test_lan_bind():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as route:
        route.connect(("192.0.2.1", 80))  # determine the local interface; no packet sent
        lan_ip = route.getsockname()[0]
    assert not lan_ip.startswith("127."), "No LAN interface is available"

    with tempfile.TemporaryDirectory(prefix="shiftwise-mock-bind-") as tmp:
        test_db = Path(tmp) / "scheduler.db"
        source_db = MOCK_DIR / "scheduler.db"
        if source_db.exists() and source_db.stat().st_size > 0:
            with sqlite3.connect(source_db) as source:
                with sqlite3.connect(test_db) as target:
                    source.backup(target)
        with socket.socket() as probe:
            probe.bind(("0.0.0.0", 0))
            port = probe.getsockname()[1]
        env = os.environ.copy()
        env.pop("SHIFTWISE_HOST", None)
        env["SHIFTWISE_PORT"] = str(port)
        env["SHIFTWISE_DB_PATH"] = str(test_db)
        env.setdefault("SHIFTWISE_BOOTSTRAP_MANAGER_PASSWORD", "testbootstrap123")
        server = subprocess.Popen(
            [str(PYTHON), "app.py"], cwd=MOCK_DIR, env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
        )
        try:
            url = f"http://{lan_ip}:{port}/healthz"
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if server.poll() is not None:
                    assert server.stderr is not None
                    raise AssertionError("Mock failed to start: " + server.stderr.read())
                try:
                    with urlopen(url, timeout=0.3) as response:
                        assert response.status == 200
                        print(f"Mock LAN listener: {url} OK")
                        break
                except (URLError, TimeoutError):
                    time.sleep(0.1)
            else:
                raise AssertionError(f"Mock did not accept LAN requests at {url}")
        finally:
            server.terminate()
            try:
                server.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
                server.communicate()


if __name__ == "__main__":
    test_lan_bind()
