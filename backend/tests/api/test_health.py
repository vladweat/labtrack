from uuid import UUID

import httpx
import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from app import main


@pytest.mark.asyncio
async def test_liveness_does_not_require_postgresql(monkeypatch) -> None:
    client_request_id = "req-123456"

    def fail_if_database_is_accessed():
        raise AssertionError("liveness must not touch PostgreSQL")

    monkeypatch.setattr(main, "get_engine", fail_if_database_is_accessed)
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://labtrack.test") as client:
        response = await client.get("/health/live", headers={"X-Request-ID": client_request_id})

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    request_id = response.headers["X-Request-ID"]
    UUID(request_id)
    assert request_id != client_request_id


@pytest.mark.asyncio
async def test_readiness_returns_503_when_postgresql_is_unavailable(monkeypatch) -> None:
    engine = create_async_engine(
        "postgresql+asyncpg://labtrack:local@127.0.0.1:1/labtrack",
        connect_args={"timeout": 0.2},
    )
    monkeypatch.setattr(main, "get_engine", lambda: engine)
    transport = httpx.ASGITransport(app=main.app)

    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://labtrack.test"
        ) as client:
            response = await client.get("/health/ready")
    finally:
        await engine.dispose()

    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}
