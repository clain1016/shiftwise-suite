# Production Deployment Guide
> Status: current · Last verified: 2026-10-02

## 1. Overview

ShiftWise deploys as a multi-container stack orchestrated via Docker Compose:
- **`app` container:** Python 3.12-slim executing Gunicorn with 2 workers and 2 threads. Runs as non-root user `shiftwise` (`UID 10001`).
- **`postgres` container:** PostgreSQL 16 (alpine) with a persistent `postgres-data` volume and a `pg_isready` healthcheck. The app connects via `DATABASE_URL` and waits for postgres to be healthy before starting.
- **`caddy` container:** Caddy 2.9 reverse proxy providing automatic HTTPS (Let's Encrypt / ZeroSSL), HTTP/2 & HTTP/3 support, modern TLS ciphers, and security header injection.

```
       [ Client HTTPS Traffic ]
                  │
                  ▼ (Port 80/443)
       ┌────────────────────────┐
       │     Caddy Proxy        │
       │ (TLS Termination, Sec) │
       └──────────┬─────────────┘
                  │ Internal Network (app-proxy)
                  ▼ (Port 5000)
       ┌────────────────────────┐
       │   Gunicorn + App       │
       │ (Flask Scheduler)      │
       └──────────┬─────────────┘
                  │ DATABASE_URL
                  ▼
       ┌────────────────────────┐
       │ PostgreSQL 16          │
       │ (postgres-data volume) │
       └────────────────────────┘
```

> The app selects its database backend from `DATABASE_URL` (PostgreSQL when
> set to a `postgresql://` URL, SQLite otherwise). The compose stack defaults
> to the bundled postgres service; set `DATABASE_URL` to an empty value to
> run the stack on the legacy SQLite volume (`shiftwise-data`). Note: `app`
> depends on the bundled `postgres` container, so compose always starts it —
> it simply idles when the app isn't using it (empty `DATABASE_URL` or an
> external database URL).

---

## 2. Prerequisites & Host Requirements

- Docker Engine 24.0+ and Docker Compose v2.
- A public DNS `A` or `AAAA` record pointing your domain to the server's public IP.
- Ports `80` and `443` open in your firewall.

---

## 3. Configuration Setup

Copy the template `.env.example` to `.env`:
```sh
cp .env.example .env
```

Configure the environment variables in `.env`:

```ini
# Generate a cryptographically secure 32-byte session secret:
SHIFTWISE_SECRET_KEY=9a4f6d8c2e1b3a5f7e9d0c2b4a6f8e1d3c5b7a9e2f4a6c8e0b2d4f6a8c0e2b4d

# Initial manager password (minimum 12 characters; used only on first boot):
SHIFTWISE_BOOTSTRAP_MANAGER_PASSWORD=ChangeMeWithStrongPassphrase123!

# Fully qualified domain name for automatic TLS:
SHIFTWISE_PUBLIC_HOSTNAME=scheduler.yourdomain.com

# Contact email for ACME certificate expiration notices:
SHIFTWISE_ACME_EMAIL=admin@yourdomain.com

# Demo seeding: Keep 0 for production!
SHIFTWISE_DEMO_SEED=0

# Proxy & cookie security:
SHIFTWISE_BEHIND_PROXY=1
SHIFTWISE_SESSION_COOKIE_SECURE=1

# PostgreSQL (bundled service): the defaults below match docker-compose.yml
# out of the box. CHANGE POSTGRES_PASSWORD for any non-local deployment —
# the default is a development convenience only. docker-compose.yml assembles
# the app's default DATABASE_URL from these three values automatically, so
# changing them here propagates without further edits. (If the password
# contains URL-special characters — : / @ ? # — percent-encode it.)
POSTGRES_DB=shiftwise
POSTGRES_USER=shiftwise
POSTGRES_PASSWORD=change-me-in-production

# Database URL for the app. The compose default (assembled in
# docker-compose.yml from POSTGRES_USER/PASSWORD/DB) already points at the
# bundled postgres service — set this only to point at an external database,
# or to an empty value to run the app on the legacy SQLite database instead.
# Note: the bundled postgres container still starts (and idles) whenever you
# bring the stack up with compose.
# DATABASE_URL=postgresql://shiftwise:change-me-in-production@postgres:5432/shiftwise
```

---

## 4. Launching the Stack

1. **Validate Configuration:**
   ```sh
   docker compose config
   ```

2. **Build and Start Containers:**
   ```sh
   docker compose up -d --build
   ```

3. **Check Service Status:**
   ```sh
   docker compose ps
   ```

4. **Inspect Logs:**
   ```sh
   docker compose logs -f app
   docker compose logs -f caddy
   ```

---

## 5. Security & Hardening Features

- **Non-Root Execution:** App runs under user `shiftwise` (`UID:GID 10001:10001`).
- **Capability Dropping:** All Linux capabilities dropped (`cap_drop: [ALL]`), with only `NET_BIND_SERVICE` granted to Caddy.
- **No New Privileges:** Container processes cannot escalate privileges (`no-new-privileges:true`).
- **Read-Only Root Filesystems:** Caddy root filesystem is read-only with ephemeral `/tmp` tmpfs.
- **Fail-Fast Secret Checks:** `docker-entrypoint.sh` halts container boot if `SHIFTWISE_SECRET_KEY` is missing or default.
- **Strict Headers:** Caddy enforces `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: strict-origin-when-cross-origin`, and `Strict-Transport-Security`.
- **Health Checks:** Probes `/healthz` every 15s to verify app responsiveness; the postgres service is gated on `pg_isready`, and the app waits for postgres to be healthy before starting.

---

## 6. Backups & Maintenance

The PostgreSQL database resides in the named Docker volume `postgres-data`.

To create a backup with `pg_dump`:
```sh
docker compose exec postgres pg_dump -U shiftwise shiftwise > backup-$(date +%Y%m%d%H%M%S).sql
```

To restore from a backup:
```sh
cat backup-YYYYMMDDHHMMSS.sql | docker compose exec -T postgres psql -U shiftwise shiftwise
```

> If you run the stack on SQLite instead (`DATABASE_URL` empty), the database
> lives in the `shiftwise-data` volume at `/data/scheduler.db`. Back it up
> with SQLite's backup API:
> ```sh
> docker compose exec app sqlite3 /data/scheduler.db ".backup '/data/backup-$(date +%Y%m%d%H%M%S).db'"
> ```
