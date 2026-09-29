# ShiftWise Suite

ShiftWise is a Flask and SQLite staff scheduler. Employees rank shifts, and the scheduler assigns available staff by employment type and hire date. The repository also contains a separate mock demo.

## Setup

Create a virtual environment and install the dependency:

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

On first start, set a manager password of at least 12 characters and a stable, random session secret. The manager account is created as `manager`; add employees from the Roster page.

```sh
read -rsp 'Manager password: ' SHIFTWISE_BOOTSTRAP_MANAGER_PASSWORD
export SHIFTWISE_BOOTSTRAP_MANAGER_PASSWORD
export SHIFTWISE_SECRET_KEY="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
.venv/bin/python app.py
```

The app listens on `127.0.0.1:5000` by default. Set `SHIFTWISE_HOST` and `SHIFTWISE_PORT` to change the listener. Keep the session secret stable across restarts. Set `SHIFTWISE_DB_PATH` to place the SQLite database elsewhere. The bootstrap password is needed only when creating the first manager or replacing an old demo manager password.

Existing databases with plain text passwords are migrated to password hashes at startup. If an existing manager still uses the old demo password `manager`, set `SHIFTWISE_BOOTSTRAP_MANAGER_PASSWORD` to replace it during migration. Employees who used shared demo passwords can change them through the Password link after signing in.

## Mock demo

The mock source mirrors the main app. To sync it and create a fresh ten-employee, Mon–Sun FOH/BOH demo database:

```sh
./shiftwise-mock/sync.sh --reseed
cd shiftwise-mock
../.venv/bin/python app.py
```

`--reseed` deletes the mock database and its request history. Run `./shiftwise-mock/sync.sh` without that flag to keep the current mock data, then restart the mock process to load the synced code. The mock listens on port 5001 on all interfaces: open `http://<this-machine's-LAN-IPv4>:5001/login` from another device on the same network (`hostname -I` shows local addresses). Only run it on a trusted LAN: the demo accounts have known passwords and local HTTP is unencrypted. Set `SHIFTWISE_HOST=127.0.0.1` when starting it to restrict access to this machine. The real app still defaults to loopback on port 5000. The suite's `.venv` is used by both apps and all tests; the older `scheduler` checkout is not required.

## Tests

The root `test_*.py` scripts and mock tests use temporary databases. The LAN bind test backs up the existing seeded mock database into a temporary copy, so run `sync.sh --reseed` once before it. Tests do not modify either app's `scheduler.db`. Run them with the virtual environment's Python, for example:

```sh
.venv/bin/python test_flow.py
.venv/bin/python test_requests.py
.venv/bin/python shiftwise-mock/test_mock.py
.venv/bin/python shiftwise-mock/test_lan_bind.py
```

## Container deployment

ShiftWise includes container deployment files to run Gunicorn behind a Caddy reverse proxy with automatic TLS.

### Environment

Copy `.env.example` to `.env`:

```sh
cp .env.example .env
```

Set the required values in `.env`:

```sh
SHIFTWISE_SECRET_KEY=$(python3 -c 'import secrets; print(secrets.token_hex(32))')
SHIFTWISE_BOOTSTRAP_MANAGER_PASSWORD="your-strong-manager-password"
SHIFTWISE_PUBLIC_HOSTNAME="shiftwise.example.com"
SHIFTWISE_ACME_EMAIL="admin@example.com"
SHIFTWISE_DEMO_SEED=1
```

### Docker Compose

```sh
# Validate configuration
docker compose config

# Build and launch stack in background
docker compose up -d --build

# Check status and health
docker compose ps

# View logs
docker compose logs -f
```
