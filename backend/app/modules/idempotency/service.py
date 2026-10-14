"""Atomic request replay for API mutations."""

import hashlib
import json
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.audit.models import AuditEvent
from app.modules.idempotency.models import IdempotencyRecord


async def execute_idempotently(
    session: AsyncSession,
    *,
    actor_id: UUID,
    operation: str,
    key: str | None,
    request_data: Any,
    action: Callable[[], Awaitable[tuple[int, dict[str, Any]]]],
) -> tuple[int, dict[str, Any]]:
    key = key.strip() if key is not None else None
    if not key:
        raise HTTPException(status_code=400, detail="Idempotency-Key header is required")
    if len(key) > 128:
        raise HTTPException(
            status_code=400, detail="Idempotency-Key must be at most 128 characters"
        )
    body_hash = hashlib.sha256(
        json.dumps(request_data, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()
    await session.execute(
        insert(IdempotencyRecord)
        .values(
            actor_user_id=actor_id,
            operation=operation,
            key=key,
            request_hash=body_hash,
        )
        .on_conflict_do_nothing(index_elements=["actor_user_id", "operation", "key"])
    )
    record = await session.scalar(
        select(IdempotencyRecord)
        .where(
            IdempotencyRecord.actor_user_id == actor_id,
            IdempotencyRecord.operation == operation,
            IdempotencyRecord.key == key,
        )
        .with_for_update()
    )
    assert record is not None
    if record.request_hash != body_hash:
        await session.rollback()
        raise HTTPException(
            status_code=409, detail="Idempotency-Key was already used with another request"
        )
    if record.response_status is not None:
        result = (record.response_status, record.response_body or {})
        await session.commit()
        return result
    try:
        status_code, body = await action()
        record.response_status = status_code
        record.response_body = body
        session.add(
            AuditEvent(
                actor_user_id=actor_id,
                operation=operation,
                request_key=key,
                request_hash=body_hash,
                resource_id=str(body.get("id", "")) or None,
            )
        )
        await session.commit()
        return status_code, body
    except IntegrityError as error:
        await session.rollback()
        raise HTTPException(
            status_code=409, detail="Request conflicts with existing data"
        ) from error
    except Exception:
        await session.rollback()
        raise
