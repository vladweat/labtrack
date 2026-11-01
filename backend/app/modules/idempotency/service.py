"""Atomic request replay for API mutations."""

import hashlib
import json
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.audit.models import AuditEvent
from app.modules.idempotency.models import IdempotencyRecord, OfflineCommandTombstone


async def execute_idempotently(
    session: AsyncSession,
    *,
    actor_id: UUID,
    operation: str,
    key: str | None,
    request_data: Any,
    action: Callable[[], Awaitable[tuple[int, dict[str, Any]]]],
    audit_details: dict[str, Any] | None = None,
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
                resource_id=(str(body.get("id") or body.get("handoff", {}).get("id", "")) or None),
                details=audit_details,
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


OFFLINE_OPERATION = "courier_offline_command"


def canonical_request_hash(request_data: Any) -> str:
    """Hash the complete validated command, including its explicit causal link."""
    return hashlib.sha256(
        json.dumps(request_data, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


async def lock_offline_command(
    session: AsyncSession, *, actor_id: UUID, command_id: str
) -> OfflineCommandTombstone | None:
    """Serialize a command identity across Shipments, including compacted commands.

    Call only after current resource authorization. The transaction-scoped lock
    covers lookup, custody mutation and both replay writes; no reservation survives
    rollback. Hash collisions only serialize unrelated commands, never merge them.
    """
    await session.execute(
        select(func.pg_advisory_xact_lock(func.hashtextextended(f"{actor_id}:{command_id}", 0)))
    )
    return await session.scalar(
        select(OfflineCommandTombstone).where(
            OfflineCommandTombstone.actor_user_id == actor_id,
            OfflineCommandTombstone.client_command_id == command_id,
        )
    )


async def persist_offline_result(
    session: AsyncSession,
    *,
    actor_id: UUID,
    command: dict[str, Any],
    request_hash: str,
    result: dict[str, Any],
    causal_metadata: dict[str, Any] | None,
) -> dict[str, Any]:
    """Write the terminal outcome, audit and immutable identity without committing."""
    audit = AuditEvent(
        actor_user_id=actor_id,
        operation=OFFLINE_OPERATION,
        request_key=command["client_command_id"],
        request_hash=request_hash,
        resource_id=command["shipment_id"],
        details={
            "action": command["action"],
            "snapshot_id": command["snapshot_id"],
            "client_performed_at": command["client_performed_at"],
            "outcome": result["outcome"],
        },
    )
    session.add(audit)
    await session.flush()
    tombstone = OfflineCommandTombstone(
        actor_user_id=actor_id,
        client_command_id=command["client_command_id"],
        request_hash=request_hash,
        original_outcome=result["outcome"],
        audit_event_id=audit.id,
        replay_response={
            name: result[name] for name in ("shipment_id", "container_id", "server_status", "error")
        },
    )
    session.add(tombstone)
    await session.flush()
    await session.refresh(tombstone, attribute_names=["recorded_at"])
    result = {**result, "recorded_at": tombstone.recorded_at.isoformat()}
    session.add(
        IdempotencyRecord(
            actor_user_id=actor_id,
            operation=OFFLINE_OPERATION,
            key=command["client_command_id"],
            request_hash=request_hash,
            response_status=200,
            response_body=result,
            offline_retention_expires_at=tombstone.recorded_at + timedelta(days=90),
            offline_causal_metadata=causal_metadata,
        )
    )
    await session.flush()
    return result


async def compact_offline_replays(session: AsyncSession, *, as_of: datetime | None = None) -> int:
    """Remove expired detail only when its immutable, matching tombstone is 90d old.

    Tombstones already contain the minimal replay facts. They are never rewritten
    or deleted here, so concurrent replay/reservation cannot observe a gap.
    The caller owns commit; the CLI always uses the database maintenance clock.
    """
    if as_of is None:
        as_of = await session.scalar(select(func.clock_timestamp()))
    assert as_of is not None
    matching_tombstone = (
        select(OfflineCommandTombstone.client_command_id)
        .where(
            OfflineCommandTombstone.actor_user_id == IdempotencyRecord.actor_user_id,
            OfflineCommandTombstone.client_command_id == IdempotencyRecord.key,
            OfflineCommandTombstone.request_hash == IdempotencyRecord.request_hash,
            OfflineCommandTombstone.recorded_at <= as_of - timedelta(days=90),
        )
        .exists()
    )
    result = await session.execute(
        delete(IdempotencyRecord).where(
            IdempotencyRecord.operation == OFFLINE_OPERATION,
            IdempotencyRecord.offline_retention_expires_at <= as_of,
            matching_tombstone,
        )
    )
    return result.rowcount
