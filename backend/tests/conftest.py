"""Shared guardrails for tests that intentionally mutate their PostgreSQL database."""

import os

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import get_settings
from app.db.session import get_engine


@pytest.fixture(scope="session", autouse=True)
def require_disposable_postgres_database() -> None:
    """Refuse test execution unless it is explicitly pointed at a test database."""

    settings = get_settings()
    if os.getenv("LABTRACK_TESTING") != "1":
        pytest.exit("Set LABTRACK_TESTING=1 to confirm this is an isolated test run.")
    if settings.database_url.get_backend_name() != "postgresql":
        pytest.exit("Foundation integration tests require PostgreSQL.")
    if "test" not in (settings.postgres_db or "").casefold():
        pytest.exit("POSTGRES_DB must include 'test'; tests clear the disposable database.")


@pytest_asyncio.fixture(autouse=True)
async def clean_foundation_tables():
    """Isolate each test by clearing rows from the dedicated test database."""

    engine = create_async_engine(get_settings().migration_database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("TRUNCATE TABLE users CASCADE"))
            await connection.execute(text("DELETE FROM organizations WHERE code <> 'CENTRAL_LAB'"))
            await connection.execute(text("DELETE FROM regions WHERE code <> 'PILOT'"))
        yield
    finally:
        await engine.dispose()
        # pytest-asyncio uses a per-test loop; discard asyncpg connections before it closes.
        await get_engine().dispose()
