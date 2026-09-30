# Production Deployment Guide
> Status: current · Last verified: 2026-09-29

## 1. Overview

ShiftWise deploys as a multi-container stack orchestrated via Docker Compose:
- **`app` container:** Python 3.12-slim executing Gunicorn with 2 workers and 2 threads. Runs as non-root user `shiftwise` (`UID 10001`).
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
                  │
                  ▼
       ┌────────────────────────┐
       │ Persistent SQLite Vol  │
       │ (/data/scheduler.db)   │
       └────────────────────────┘
```

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
- **Health Checks:** Probes `/healthz` every 15s to verify SQLite responsiveness.

---

## 6. Backups & Maintenance

The SQLite database resides in the named Docker volume `shiftwise-data` mounted at `/data/scheduler.db`.

To create an online backup using SQLite's backup API:
```sh
docker compose exec app sqlite3 /data/scheduler.db ".backup '/data/backup-$(date +%Y%m%d%H%M%S).db'"
```
