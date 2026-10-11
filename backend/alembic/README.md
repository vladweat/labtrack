# Database migrations

Alembic uses the async SQLAlchemy URL from the repository-root `.env`. The first revision creates regions, organizations, users, and scoped memberships for identity and access-control foundations. Keep revisions in `versions/`; apply them as an explicit command rather than on API or worker startup.

```bash
cd backend
alembic upgrade head
alembic current
```

CI applies the migration to a disposable PostgreSQL 15 service. Do not use SQLite as a substitute for PostgreSQL constraint, locking, or concurrency verification.
