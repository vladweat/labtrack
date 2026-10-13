# Backend development

The backend is a FastAPI modular monolith. Foundation includes typed settings, async PostgreSQL sessions, an explicit Alembic identity/scope migration, short-lived identity-only JWTs, Argon2id password verification, active-membership role primitives, health probes, and JSON request-correlated logs. Shipment workflows, user provisioning endpoints, and Celery tasks are not implemented yet.

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

Start the API from this directory:

```bash
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

The liveness endpoint is `http://127.0.0.1:8000/health/live`; readiness is `http://127.0.0.1:8000/health/ready` and returns 503 if PostgreSQL is unavailable. The auth routes are under `/api/v1/auth`; `POST /token` accepts a JSON email/password body and returns a short-lived Bearer token, while `GET /me` reloads the active user and memberships from PostgreSQL. User creation and membership administration are intentionally not exposed yet.

JWT roles are not embedded in tokens. Protected use cases must use `Principal`/`require_roles` and verify the active organization or region scope against the target resource. A role match by itself is not an ownership check. Redis is not required for readiness or authentication.

Run static checks with `ruff check .`, `ruff format --check .`, and `python -m compileall -q app alembic tests`. The pytest suite covers credentials/JWT, active and revoked accounts/memberships, scoped organization/region authorization, health behavior, and migration state.

Prepare a dedicated test database; the suite truncates identity/scope tables before each test and refuses to run without an explicit test-database name and opt-in flag:

```bash
docker compose exec -T postgres createdb -U labtrack labtrack_test
LABTRACK_TESTING=1 POSTGRES_DB=labtrack_test pytest -q
```

The CI job checks Ruff lint and format, Python compilation, migration upgrade/drift/downgrade/upgrade, and the full pytest suite against PostgreSQL 15.
