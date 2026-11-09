"""Best-effort scheduled scan of current stalled business cases."""

import asyncio
import logging
import time

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.core.logging import emit_telemetry_event
from app.modules.notifications.service import persist_stalled_notifications
from app.modules.operations.service import (
    count_stalled_pickup_delivery,
    list_notification_candidates,
)
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


async def _scan_stalled_notifications() -> int:
    started_at = time.monotonic()
    candidate_case_count = 0
    candidate_notification_count = 0
    inserted_notification_count = 0
    stalled_pickup_count: int | None = None
    stalled_delivery_count: int | None = None
    engine = None
    try:
        settings = get_settings()
        engine = create_async_engine(settings.database_url, pool_pre_ping=True)
        session_factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
        try:
            async with session_factory.begin() as session:
                candidates = await list_notification_candidates(session, settings)
                candidate_case_count = len(candidates)
                candidate_notification_count = sum(
                    len(candidate.recipient_user_ids) for candidate in candidates
                )
                inserted_notification_count = await persist_stalled_notifications(
                    session, candidates
                )
            try:
                async with session_factory() as telemetry_session:
                    (
                        stalled_pickup_count,
                        stalled_delivery_count,
                    ) = await count_stalled_pickup_delivery(telemetry_session, settings)
            except Exception as exc:
                emit_telemetry_event(
                    "stalled_work_count_failed",
                    logger_name=logger.name,
                    error_type=type(exc).__name__,
                )
        finally:
            await engine.dispose()
    except Exception as exc:
        try:
            logger.error(
                "stalled_notification_scan_failed",
                extra={"outcome": "failure", "error_type": type(exc).__name__},
            )
        except Exception:
            pass
        raise

    stalled_count_fields = {}
    if stalled_pickup_count is not None and stalled_delivery_count is not None:
        stalled_count_fields = {
            "stalled_pickup_count": stalled_pickup_count,
            "stalled_delivery_count": stalled_delivery_count,
        }
    emit_telemetry_event(
        "stalled_notification_scan_completed",
        logger_name=logger.name,
        outcome="success",
        candidate_case_count=candidate_case_count,
        notification_insert_count=inserted_notification_count,
        notification_dedup_count=max(candidate_notification_count - inserted_notification_count, 0),
        duration_ms=round((time.monotonic() - started_at) * 1000, 3),
        **stalled_count_fields,
    )
    return inserted_notification_count


@celery_app.task(
    name="app.workers.notifications.scan_stalled_notifications",
    ignore_result=True,
)
def scan_stalled_notifications() -> int:
    """Insert current qualified notifications without changing business state."""

    return asyncio.run(_scan_stalled_notifications())
