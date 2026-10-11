"""Async SQLAlchemy engine, session factory, and FastAPI session dependency."""

from collections.abc import AsyncIterator
from functools import lru_cache

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings


@lru_cache(maxsize=1)
def get_engine():
    settings = get_settings()
    return create_async_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=5,
        pool_recycle=1800,
    )


@lru_cache(maxsize=1)
def get_session_factory() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(get_engine(), expire_on_commit=False, autoflush=False)


async def get_db_session() -> AsyncIterator[AsyncSession]:
    """Yield one request-scoped session; transaction boundaries belong to use cases."""

    async with get_session_factory()() as session:
        yield session


async def dispose_engine() -> None:
    """Close pooled connections during application shutdown."""

    if get_engine.cache_info().currsize:
        await get_engine().dispose()
