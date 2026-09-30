# ShiftWise Suite

ShiftWise is a modern Flask and SQLite staff scheduling and shift-management platform tailored for hospitality and service industries with Front-of-House (FOH) and Back-of-House (BOH) operations.

Employees submit ranked preferences for weekly shifts, and the auto-scheduler generates conflict-free schedules honoring weekly hour caps, minimum days off, and strict house boundaries. Managers retain full administrative oversight with sticky assignment overrides, conflict review, and request management.

---

## Quickstart (Local Development)

### 1. Setup Virtual Environment
```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -r requirements-dev.txt
```

### 2. Launch Development Server
```sh
export SHIFTWISE_SECRET_KEY=$(python3 -c 'import secrets; print(secrets.token_hex(32))')
export SHIFTWISE_BOOTSTRAP_MANAGER_PASSWORD="your-strong-manager-password"
.venv/bin/python app.py
```
Open [http://127.0.0.1:5000](http://127.0.0.1:5000) and sign in as `manager`.

---

## Email and text schedule links

The manager can save employee email addresses and E.164 phone numbers on the Roster page, then send the employee a link to sign in and submit schedule preferences. Set `SHIFTWISE_PUBLIC_URL` to the app's externally reachable HTTPS address. Email uses SMTP (`SHIFTWISE_SMTP_HOST`, `SHIFTWISE_SMTP_FROM`, and optionally `SHIFTWISE_SMTP_PORT`, `SHIFTWISE_SMTP_USER`, `SHIFTWISE_SMTP_PASSWORD`). SMS uses Twilio (`SHIFTWISE_TWILIO_ACCOUNT_SID`, `SHIFTWISE_TWILIO_AUTH_TOKEN`, `SHIFTWISE_TWILIO_FROM`). Configure whichever channel you plan to use in `.env` or the deployment secrets file, then restart the app. The message contains the sign-in link, not a password; employees still sign in with their existing account.

---

## Quickstart (Container Deployment)

ShiftWise includes production-grade container orchestration with Gunicorn behind a Caddy reverse proxy providing automatic TLS and security headers:

```sh
# Copy environment configuration
cp .env.example .env

# Edit .env with your domain and secrets, then build and run:
docker compose up -d --build

# View container status
docker compose ps
```

---

## Documentation Directory

Comprehensive documentation is available in the [`docs/`](docs/) directory:

- 📐 **[Architecture & System Design](docs/ARCHITECTURE.md):** High-level architecture, scheduling algorithms, priority sorting, coverage planning, and house boundaries.
- 🗄️ **[Data Model & Storage Schema](docs/DATA_MODEL.md):** SQLite schema specifications, table relationships, assignment lifecycle, and request state machines.
- 💻 **[Development Guide](docs/DEVELOPMENT.md):** Local setup, running unit/integration tests, and using the gauntlet/chaos simulation harnesses.
- 🚀 **[Production Deployment](docs/DEPLOYMENT.md):** Multi-container Docker Compose deployment, Caddy TLS setup, and security hardening.
- ⚙️ **[Environment Variables](docs/ENVIRONMENT.md):** Complete reference of all `SHIFTWISE_*` configuration parameters.

---

## Running Tests & Simulation Harnesses

All test suites use isolated temporary SQLite databases:

```sh
# Run entire test suite with pytest
.venv/bin/pytest tests/

# Run the 11-phase stress gauntlet
.venv/bin/python tools/gauntlet.py

# Run the sequential conflict scenario simulation
.venv/bin/python tools/scenario_demo.py

# Run the randomized house-chaos simulation
.venv/bin/python tools/scenario_random.py

# Run all tests and simulations in one command
./tools/run_all.sh
```

---

## License

Internal proprietary software — all rights reserved.
