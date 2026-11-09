# Backend development

The backend is a FastAPI modular monolith. Implemented modules cover scoped identity and organizations, Samples and Shipments, bilateral whole-Shipment Pickup, per-container Delivery, lab receiving and revisions, custody incidents and exceptional receipt, Courier offline sync, audit/idempotency, operations, analytics, and Inbox. PostgreSQL is authoritative for business state; Redis/Celery support best-effort notifications. User provisioning is operator-controlled through `python -m app.cli`.

See the [architecture](../docs/architecture.md), [workflows](../docs/workflows.md), and [API guide](../docs/api.md) for a compact implementation overview.

From this directory, create a virtual environment and install the project with development tools:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
```

Create the local environment file from the repository root if it does not exist, then start PostgreSQL:

```bash
cd ..
test -f .env || cp .env.example .env
docker compose up -d postgres
cd backend
```

Apply database migrations explicitly before starting the API:

```bash
alembic upgrade head
```

The `POSTGRES_USER` account owns the schema and is used by Alembic. The API uses
`APP_POSTGRES_USER`/`APP_POSTGRES_PASSWORD`, which must be a non-owner role. A
fresh Compose volume creates this restricted role from values in `.env`. For an
existing volume, execute the SQL in `backend/database/init/10-runtime-role.sh`
once through the PostgreSQL owner, then restart the API.

Start the API from this directory:

```bash
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000 --no-proxy-headers
```

The login limiter uses the ASGI transport peer as the source address by
default. To use a reverse proxy's `X-Real-IP`, set
`LOGIN_TRUSTED_PROXY_NETWORKS` to a comma-separated list of that proxy's exact
IP addresses or CIDRs, and configure the proxy to overwrite or remove
client-supplied forwarding headers. The limiter never reads
`X-Forwarded-For`. The demo Docker images and local run commands explicitly
use `--no-proxy-headers`; do not re-enable Uvicorn's proxy middleware because
it can rewrite the ASGI client address before the application checks the
immediate peer.

The checked demo configuration has Caddy and no Nginx config. Caddy currently
sets `X-Real-IP` from `{remote_host}`, which is the connecting peer; this is
the client only when Caddy receives the connection directly. If another load
balancer is in front, configure Caddy to trust only that balancer and set
`X-Real-IP` from Caddy's validated client address. Remove incoming
`X-Forwarded-For` in the proxy configuration as well. For Caddy, the relevant
upstream directives are:

```caddyfile
header_up X-Real-IP {client_ip}
header_up -X-Forwarded-For
```

With no upstream load balancer, Caddy resolves `client_ip` to its direct
socket peer. If a load balancer is present, add only its CIDRs to Caddy's
`servers > trusted_proxies` configuration before relying on `client_ip`.
The demo Compose file does not publish the API port, but the API joins both
`edge` and `data`; Caddy and the seed job join `edge`, while the database,
Redis, migration, worker, and Beat services join `data`. A trusted network
covering either subnet would therefore trust more peers than Caddy. Pin the
Caddy address and pass its exact `/32` or `/128` through
`LOGIN_TRUSTED_PROXY_NETWORKS`; leave the setting empty if the immediate peer
cannot be pinned and restricted.

The checked demo Compose file does not currently pass this setting to the API.
To enable proxy identity there, add
`LOGIN_TRUSTED_PROXY_NETWORKS: ${DEMO_LOGIN_TRUSTED_PROXY_NETWORKS:-}` to the
API environment, define that private deployment variable as the pinned Caddy
peer `/32` or `/128`, and apply the Caddy directives above. Without those
changes the API stays on transport-peer identity, so requests through Caddy
share its bucket.

Failed-login limits are process-local and default to 30 failures per source in
900 seconds. Restarts clear the counters, and each worker or replica has its
own counters; this is not distributed enforcement.

The liveness endpoint is `http://127.0.0.1:8000/health/live`; readiness is `http://127.0.0.1:8000/health/ready` and returns 503 if PostgreSQL is unavailable. The auth routes are under `/api/v1/auth`; `POST /token` accepts a JSON email/password body and returns a short-lived Bearer token, while `GET /me` reloads the active user and memberships from PostgreSQL. Account creation and membership grants are operator-only CLI operations; there is no public administrator endpoint.

### Provision users and memberships

Run commands from `backend/` after configuring `.env` and applying migrations. The operator needs the backend environment and database connection; no password is passed in shell history or process arguments.

Create the first laboratory manager:

```bash
python -m app.cli provision-user \
  --email manager@example.test \
  --display-name "Laboratory Manager" \
  --role LAB_MANAGER \
  --organization-code CENTRAL_LAB
```

The command prompts twice for an initial password without echoing it. Passwords must be 12–256 characters. Organization roles are `SENDER_OPERATOR`, `LAB_OPERATOR`, and `LAB_MANAGER`; region roles are `COURIER`, `REGIONAL_OPERATIONS`, and `REGIONAL_ANALYST`.

Add another role or scope to an existing user:

```bash
python -m app.cli grant-membership \
  --email courier@example.test \
  --role COURIER \
  --region-code PILOT
```

Each command validates that the selected scope exists and is active, checks that the role matches the scope type and organization kind, and commits the account/membership change as one transaction. `SENDER_OPERATOR` requires a `HEALTHCARE_FACILITY`; `LAB_OPERATOR` and `LAB_MANAGER` require a `LABORATORY`. Existing email and duplicate membership grants are rejected. Provisioning does not create organizations or regions; those records must already exist.

The separate `seed-demo` command creates only synthetic Sender, Courier, Lab,
and Shipment data. It requires `APP_ENV=demo`, `DEMO_SEED_ENABLED=true`, and a
database name containing `demo` or `test`; see the
[demo deployment guide](../docs/deploy.md) for the isolated local setup and
reset procedure. The separate local-only PostgreSQL backup/restore drill and
its recovery limits are documented in the
[backup/restore guide](../docs/backup-restore.md).

JWT roles are not embedded in tokens. Protected use cases must use `Principal`/`require_roles` and verify the active organization or region scope against the target resource. A role match by itself is not an ownership check. Redis is not required for readiness or authentication.

Useful static checks are `ruff check .`, `ruff format --check .`, and `python -m compileall -q app alembic tests`.

Prepare a dedicated test database and runtime role; the suite truncates test rows before each test and refuses to run without an explicit test-database name and opt-in flag:

```bash
docker compose exec -T postgres createdb -U labtrack labtrack_test
docker compose exec -T -e POSTGRES_DB=labtrack_test postgres /docker-entrypoint-initdb.d/10-runtime-role.sh
LABTRACK_TESTING=1 POSTGRES_DB=labtrack_test pytest -q
```

Set `APP_POSTGRES_USER` and `APP_POSTGRES_PASSWORD` for the API test connection;
the owner credentials stay in `POSTGRES_USER` and `POSTGRES_PASSWORD` for
migrations and isolated cleanup.

The CI job checks Ruff lint and format, Python compilation, migration upgrade/drift/downgrade/upgrade, and the full pytest suite against PostgreSQL 15.
