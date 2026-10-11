"""ASGI application entry point."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.api.v1.router import api_router
from app.core.config import get_settings
from app.core.logging import configure_logging, normalized_request_id, request_id_var
from app.db.session import dispose_engine, get_engine

logger = logging.getLogger(__name__)
settings = get_settings()
configure_logging(settings.log_level)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Release pooled DB connections on graceful shutdown."""

    yield
    await dispose_engine()


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.include_router(api_router, prefix="/api/v1")


@app.middleware("http")
async def add_request_id(request: Request, call_next):
    request_id = normalized_request_id(request.headers.get("X-Request-ID"))
    context_token = request_id_var.set(request_id)
    try:
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response
    finally:
        request_id_var.reset(context_token)


@app.get("/health/live", tags=["health"], include_in_schema=False)
async def health_live() -> dict[str, str]:
    """Report that the API process is running without contacting dependencies."""

    return {"status": "ok"}


@app.get("/health/ready", tags=["health"], include_in_schema=False, response_model=None)
async def health_ready() -> JSONResponse | dict[str, str]:
    """Report readiness only when the authoritative PostgreSQL database responds."""

    try:
        async with get_engine().connect() as connection:
            await connection.execute(text("SELECT 1"))
    except SQLAlchemyError:
        logger.exception("database readiness probe failed")
        return JSONResponse(status_code=503, content={"status": "unavailable"})
    return {"status": "ok"}
