# Environment Variables Reference
> Status: current · Last verified: 2026-09-29

All ShiftWise configuration is driven through environment variables prefixed with `SHIFTWISE_`.

## Core Application Variables

| Variable | Type | Default Value | Used In | Description & Recommendations |
|---|---|---|---|---|
| `SHIFTWISE_SECRET_KEY` | String | *Ephemeral (Generated)* | `app.py`, `docker-entrypoint.sh` | Cryptographic secret key used to sign session cookies. **Must be fixed and persistent** in production (32-byte hex); an ephemeral key logs a warning and invalidates all user logins on restart. |
| `SHIFTWISE_BOOTSTRAP_MANAGER_PASSWORD` | String | *None* | `app.py`, `docker-entrypoint.sh` | Password used to create the initial `manager` account on a fresh database, or upgrade legacy plain-text `manager` passwords. Must be at least 12 characters. |
| `SHIFTWISE_DB_PATH` | Path | `scheduler.db` | `app.py`, `Dockerfile`, `docker-compose.yml` | Filesystem path to the SQLite database. In Docker deployments, defaults to `/data/scheduler.db`. |
| `SHIFTWISE_HOST` | String | `127.0.0.1` | `app.py` | Bind interface for the Flask development server. Defaults to loopback (`127.0.0.1`); set to `0.0.0.0` for LAN access. |
| `SHIFTWISE_PORT` | Integer | `5000` | `app.py`, `Dockerfile` | HTTP listening port for the application server. (Mock demo defaults to `5001`). |
| `SHIFTWISE_BEHIND_PROXY` | Boolean | `""` (False) | `app.py` | When set to `1`, `true`, or `yes`, wraps the WSGI application with Werkzeug's `ProxyFix` middleware to trust `X-Forwarded-*` headers from Caddy or Nginx. |
| `SHIFTWISE_SESSION_COOKIE_SECURE` | Boolean | `""` (False) | `app.py` | When set to `1`, `true`, or `yes`, marks the session cookie with the `Secure` flag (browser will only send it over HTTPS). |
| `SHIFTWISE_SESSION_COOKIE_SAMESITE` | String | `Lax` | `app.py` | `SameSite` attribute for the session cookie (`Lax`, `Strict`, or `None`). |
| `SHIFTWISE_DEMO_SEED` | String | `""` (0) | `app.py`, `docker-entrypoint.sh` | When set to `1`, `8`, or `mock`, populates a fresh database with the demo roster and shift schedule. Keep `0` for production deployments. |

---

## Container & Proxy Variables

| Variable | Type | Default Value | Used In | Description & Recommendations |
|---|---|---|---|---|
| `SHIFTWISE_PUBLIC_HOSTNAME` | String | `localhost` | `Caddyfile`, `docker-compose.yml` | Public domain name for the service. Caddy uses this hostname to provision automatic TLS certificates. |
| `SHIFTWISE_ACME_EMAIL` | String | `operator@cysys.dev` | `Caddyfile`, `docker-compose.yml` | Email address registered with Let's Encrypt / ZeroSSL for certificate renewal and expiration notices. |
| `SHIFTWISE_HTTP_PORT` | Integer | `80` | `docker-compose.yml` | External host port mapped to Caddy's HTTP listener. |
| `SHIFTWISE_HTTPS_PORT` | Integer | `443` | `docker-compose.yml` | External host port mapped to Caddy's HTTPS listener. |
| `SHIFTWISE_ENV_FILE` | Path | `/srv/cyberdyne/secrets/...` | `docker-compose.yml` | Optional host path to a secondary machine-local secrets file. |

---

## Security & Login Throttling Variables

| Variable | Type | Default Value | Used In | Description & Recommendations |
|---|---|---|---|---|
| `SHIFTWISE_LOGIN_MAX_FAILURES` | Integer | `5` | `shiftwise/auth.py` | Consecutive failed sign-ins allowed for one username before that username is locked out. `0` disables the username lockout. |
| `SHIFTWISE_LOGIN_IP_MAX_FAILURES` | Integer | `25` | `shiftwise/auth.py` | Consecutive failed sign-ins allowed from one client address, which catches username spraying. `0` disables the address lockout. |
| `SHIFTWISE_LOGIN_LOCKOUT_SECONDS` | Integer | `900` | `shiftwise/auth.py` | How long a locked username or address stays locked. |

---

## Test & Simulation Variables

| Variable | Type | Default Value | Used In | Description |
|---|---|---|---|---|
| `SHIFTWISE_SCENARIO_DB_PATH` | Path | *None* | `scenario_demo.py`, `scenario_random.py` | Overrides the database path when running automated scenario or chaos simulations. |
