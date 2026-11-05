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
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

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
