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
- 🗺️ **[Master Reorganization Plan](ShiftWise%20Suite%20reorganization%20plan.md):** Strategic roadmap for codebase modernization and refactoring.

---

## Running Tests

All test suites use isolated temporary SQLite databases:

```sh
# Run unittest suites
.venv/bin/pytest -v test_container.py test_review_fixes.py shiftwise-mock/test_lan_bind.py

# Run full procedural test suite
for test in test_*.py; do
    .venv/bin/python "$test"
done

# Run the 11-phase stress gauntlet
.venv/bin/python shiftwise-mock/gauntlet.py
```

---

## License

Internal proprietary software — all rights reserved.
