"""ASGI application entry point."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.v1.router import api_router
from app.core.config import get_settings
from app.core.errors import APIErrorResponse
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


@app.exception_handler(StarletteHTTPException)
async def documented_http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    """Convert application HTTP failures to the documented safe error envelope."""

    code_by_status = {
        400: "INVALID_REQUEST",
        401: "UNAUTHORIZED",
        403: "FORBIDDEN",
        404: "RESOURCE_NOT_FOUND",
        409: "INVALID_STATE",
        429: "RATE_LIMITED",
        503: "SERVICE_UNAVAILABLE",
    }
    detail = exc.detail if isinstance(exc.detail, str) else "Request failed"
    code = code_by_status.get(exc.status_code, "REQUEST_FAILED")
    if exc.status_code == 409:
        if "Idempotency-Key" in detail:
            code = "IDEMPOTENCY_KEY_REUSED"
        elif "complete sealed manifest" in detail:
            code = "MANIFEST_MISMATCH"
        elif "different actor" in detail:
            code = "SELF_CONFIRMATION_FORBIDDEN"
    request_id = request_id_var.get()
    payload = APIErrorResponse(
        error={"code": code, "message": detail, "details": {}, "request_id": request_id}
    )
    return JSONResponse(
        status_code=exc.status_code,
        content=payload.model_dump(mode="json"),
        headers=exc.headers,
    )


@app.exception_handler(RequestValidationError)
async def documented_validation_error(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Return schema validation failures in the same safe envelope as the API contract."""

    payload = APIErrorResponse(
        error={
            "code": "VALIDATION_ERROR",
            "message": "Request validation failed",
            "details": {},
            "request_id": request_id_var.get(),
        }
    )
    return JSONResponse(status_code=422, content=payload.model_dump(mode="json"))


@app.exception_handler(Exception)
async def documented_internal_error(request: Request, exc: Exception) -> JSONResponse:
    """Hide internal exception details while preserving request correlation."""

    logger.exception("unhandled API exception")
    payload = APIErrorResponse(
        error={
            "code": "INTERNAL_ERROR",
            "message": "Internal server error",
            "details": {},
            "request_id": request_id_var.get(),
        }
    )
    return JSONResponse(status_code=500, content=payload.model_dump(mode="json"))


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
    except (SQLAlchemyError, OSError):
        logger.exception("database readiness probe failed")
        return JSONResponse(status_code=503, content={"status": "unavailable"})
    return {"status": "ok"}
