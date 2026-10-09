# Backend development

The initial backend scaffold follows the planned FastAPI modular monolith. Only `/health/live` is implemented; domain endpoints, database metadata, Alembic runtime, and Celery tasks are not implemented yet.

From this directory, create a virtual environment and install the project with development tools:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
```

Start the API from this directory:

```bash
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

The liveness endpoint is `http://127.0.0.1:8000/health/live`. Start PostgreSQL and Redis from the repository root with `docker compose up -d` when database-backed development begins.
