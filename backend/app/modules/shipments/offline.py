"""Authenticated Courier snapshots and transactional offline command sync."""

import logging
from datetime import datetime, timedelta
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import API_ERROR_RESPONSES
from app.db.session import get_db_session
from app.modules.idempotency.models import IdempotencyRecord, OfflineCommandTombstone
from app.modules.idempotency.service import (
    OFFLINE_OPERATION,
    canonical_request_hash,
    lock_offline_command,
    persist_offline_result,
)
from app.modules.identity.access import Principal, get_current_principal
from app.modules.identity.models import Membership, Region, User
from app.modules.shipments.commands import (
    active_assigned_courier,
    confirm_pickup_command,
    initiate_container_delivery_command,
)
from app.modules.shipments.models import CourierManifestSnapshot, Shipment, TransportContainer
from app.modules.shipments.schemas import (
    CourierOfflineCommandBase,
    CourierOfflineContainerDeliveryCommand,
    CourierOfflineManifest,
    CourierOfflinePickupCommand,
    CourierSyncRequest,
    CourierSyncResponse,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/courier", tags=["CourierOffline"])
OFFLINE_API_ERROR_RESPONSES = {
    status: API_ERROR_RESPONSES[status] for status in (401, 403, 404, 409, 422, 503)
}
OFFLINE_MANIFEST_SUCCESS_RESPONSE = {
    "description": "Success",
    "headers": {"X-Request-ID": {"schema": {"type": "string", "format": "uuid"}}},
}


@router.get(
    "/shipments/{shipment_id}/offline-manifest",
    response_model=CourierOfflineManifest,
    responses={**OFFLINE_API_ERROR_RESPONSES, 200: OFFLINE_MANIFEST_SUCCESS_RESPONSE},
    operation_id="getCourierOfflineManifest",
    summary=(
        "Issue authenticated Courier snapshot with immutable server-issued ID/time and "
        "manifest/custody versions"
    ),
)
async def get_courier_offline_manifest(
    shipment_id: UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> CourierOfflineManifest:
    shipment = await session.scalar(
        select(Shipment).where(Shipment.id == shipment_id).with_for_update()
    )
    if (
        shipment is None
        or shipment.courier_user_id != principal.user_id
        or not principal.has_role("COURIER", region_id=shipment.region_id)
    ):
        raise HTTPException(404, "Resource not found")
    if not await active_assigned_courier(session, shipment):
        raise HTTPException(404, "Resource not found")
    if shipment.sealed_at is None:
        raise HTTPException(409, "Shipment must be sealed before an offline snapshot is issued")

    containers = list(
        await session.scalars(
            select(TransportContainer)
            .where(TransportContainer.shipment_id == shipment.id)
            .order_by(TransportContainer.id)
            .with_for_update()
        )
    )
    if not containers:
        raise HTTPException(409, "Sealed shipment has no containers")

    container_payload = [
        {
            "id": str(container.id),
            "code": container.code,
            "delivery_status": container.delivery_status,
            "state_version": container.state_version,
        }
        for container in containers
    ]
    snapshot = CourierManifestSnapshot(
        actor_user_id=principal.user_id,
        assigned_courier_user_id=shipment.courier_user_id,
        shipment_id=shipment.id,
        region_id=shipment.region_id,
        manifest_revision=shipment.manifest_revision,
        pickup_state_version=shipment.pickup_state_version,
        policy_version="courier-offline-v1",
        container_payload=container_payload,
    )
    session.add(snapshot)
    await session.flush()
    await session.refresh(snapshot, attribute_names=["issued_at"])

    response = CourierOfflineManifest(
        shipment_id=shipment.id,
        manifest_revision=shipment.manifest_revision,
        status=shipment.status,
        container_codes=[item["code"] for item in container_payload],
        cached_at=snapshot.issued_at,
        containers=container_payload,
        snapshot_id=snapshot.id,
        issued_at=snapshot.issued_at,
        pickup_state_version=shipment.pickup_state_version,
    )
    await session.commit()
    return response


async def server_now(session: AsyncSession) -> datetime:
    return await session.scalar(select(func.clock_timestamp()))


class CommandNotApplied(Exception):
    def __init__(self, code: str, message: str, outcome: str = "REJECTED"):
        self.code = code
        self.message = message
        self.outcome = outcome


def command_result(
    command: CourierOfflineCommandBase,
    outcome: str,
    *,
    server_status: str | None = None,
    code: str | None = None,
    message: str | None = None,
) -> dict:
    return {
        "client_command_id": str(command.client_command_id),
        "shipment_id": str(command.shipment_id),
        "container_id": str(command.container_id) if hasattr(command, "container_id") else None,
        "client_performed_at": command.client_performed_at.isoformat(),
        "outcome": outcome,
        "original_outcome": None,
        "server_status": server_status,
        "recorded_at": None,
        "error": {"code": code, "message": message} if code else None,
    }


async def authorize_sync_command(
    session: AsyncSession, principal: Principal, shipment_id: UUID
) -> Shipment | None:
    shipment = await session.scalar(
        select(Shipment)
        .where(Shipment.id == shipment_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if shipment is None or shipment.courier_user_id != principal.user_id:
        return None
    # Re-evaluate live scope on every command, including a replay within a batch.
    # Shared locks serialize revocation against this command's transaction.
    authorized = await session.scalar(
        select(Membership.id)
        .join(User, User.id == Membership.user_id)
        .join(Region, Region.id == Membership.region_id)
        .where(
            Membership.user_id == principal.user_id,
            Membership.region_id == shipment.region_id,
            Membership.organization_id.is_(None),
            Membership.role == "COURIER",
            Membership.is_active.is_(True),
            User.is_active.is_(True),
            Region.is_active.is_(True),
        )
        .with_for_update(read=True, of=(Membership, User, Region))
    )
    return (
        shipment
        if authorized and principal.has_role("COURIER", region_id=shipment.region_id)
        else None
    )


async def validate_offline_command(
    session: AsyncSession,
    principal: Principal,
    shipment: Shipment,
    command: CourierOfflinePickupCommand | CourierOfflineContainerDeliveryCommand,
) -> None:
    snapshot = await session.get(CourierManifestSnapshot, command.snapshot_id)
    if (
        snapshot is None
        or snapshot.actor_user_id != principal.user_id
        or snapshot.shipment_id != shipment.id
        or snapshot.assigned_courier_user_id != shipment.courier_user_id
        or snapshot.region_id != shipment.region_id
        or snapshot.policy_version != "courier-offline-v1"
    ):
        raise CommandNotApplied(
            "INVALID_SNAPSHOT", "Snapshot is not valid for this actor and Shipment"
        )
    if await server_now(session) - snapshot.issued_at > timedelta(hours=24):
        raise CommandNotApplied(
            "OFFLINE_SNAPSHOT_EXPIRED",
            "Snapshot is older than 24 hours; manual review and a new live action are required",
            "REVIEW_REQUIRED",
        )
    if (
        shipment.sealed_at is None
        or command.expected_manifest_version != snapshot.manifest_revision
        or shipment.manifest_revision != snapshot.manifest_revision
    ):
        raise CommandNotApplied("OFFLINE_COMMAND_STALE", "Sealed manifest revision changed")
    if isinstance(command, CourierOfflinePickupCommand):
        if (
            command.expected_target_state_version != snapshot.pickup_state_version
            or shipment.pickup_state_version != snapshot.pickup_state_version
        ):
            raise CommandNotApplied("OFFLINE_COMMAND_STALE", "Pickup custody phase changed")
        if sorted(command.container_codes) != sorted(c["code"] for c in snapshot.container_payload):
            raise CommandNotApplied(
                "COMMAND_REJECTED", "Codes must match the complete sealed manifest"
            )
        return

    target = next(
        (c for c in snapshot.container_payload if c["id"] == str(command.container_id)), None
    )
    if target is None or command.container_code != target["code"]:
        raise CommandNotApplied("COMMAND_REJECTED", "Container is not in the snapshot manifest")
    if command.expected_target_state_version != target["state_version"]:
        raise CommandNotApplied(
            "OFFLINE_COMMAND_STALE", "Submitted target version does not match snapshot"
        )
    container = await session.scalar(
        select(TransportContainer)
        .where(
            TransportContainer.id == command.container_id,
            TransportContainer.shipment_id == shipment.id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if container is None:
        raise CommandNotApplied(
            "COMMAND_REJECTED", "Container is not in the current sealed manifest"
        )
    expected_pickup = snapshot.pickup_state_version
    expected_target = target["state_version"]
    if command.depends_on_command_id is not None:
        if command.depends_on_command_id == command.client_command_id:
            raise CommandNotApplied("INVALID_DEPENDENCY", "Delivery cannot depend on itself")
        predecessor = await session.get(
            OfflineCommandTombstone, (principal.user_id, str(command.depends_on_command_id))
        )
        if predecessor is None:
            # Only test foreign identity existence; never read or return its replay data.
            foreign_identity = await session.scalar(
                select(OfflineCommandTombstone.client_command_id)
                .where(
                    OfflineCommandTombstone.client_command_id == str(command.depends_on_command_id),
                    OfflineCommandTombstone.actor_user_id != principal.user_id,
                )
                .limit(1)
            )
            if foreign_identity is not None:
                raise CommandNotApplied(
                    "INVALID_DEPENDENCY", "Predecessor belongs to another actor"
                )
            raise CommandNotApplied(
                "DEPENDENCY_NOT_AVAILABLE", "Pickup predecessor has not been recorded", "RETRYABLE"
            )
        detail = await session.scalar(
            select(IdempotencyRecord).where(
                IdempotencyRecord.actor_user_id == principal.user_id,
                IdempotencyRecord.operation == OFFLINE_OPERATION,
                IdempotencyRecord.key == str(command.depends_on_command_id),
            )
        )
        metadata = detail.offline_causal_metadata if detail else None
        if (
            predecessor.original_outcome != "APPLIED"
            or not metadata
            or metadata.get("action") != "COURIER_CONFIRM_PICKUP"
            or metadata.get("shipment_id") != str(shipment.id)
            or metadata.get("snapshot_id") != str(snapshot.id)
            or str(command.container_id) not in metadata.get("container_state_versions", {})
        ):
            raise CommandNotApplied(
                "INVALID_DEPENDENCY", "Predecessor must be an applied Pickup for this snapshot"
            )
        expected_pickup = metadata["pickup_state_version"]
        expected_target = metadata["container_state_versions"][str(container.id)]
    if (
        shipment.pickup_state_version != expected_pickup
        or container.state_version != expected_target
    ):
        raise CommandNotApplied("OFFLINE_COMMAND_STALE", "Pickup or target custody state changed")


async def apply_offline_command(
    session: AsyncSession,
    principal: Principal,
    shipment: Shipment,
    command: CourierOfflinePickupCommand | CourierOfflineContainerDeliveryCommand,
) -> dict | None:
    await validate_offline_command(session, principal, shipment, command)
    if isinstance(command, CourierOfflinePickupCommand):
        await confirm_pickup_command(session, principal, shipment.id, command.container_codes)
        # Pickup versions are advanced by database Handoff triggers, not the ORM.
        await session.refresh(shipment, attribute_names=["pickup_state_version", "status"])
        containers = list(
            await session.scalars(
                select(TransportContainer)
                .where(TransportContainer.shipment_id == shipment.id)
                .order_by(TransportContainer.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        return {
            "action": command.action,
            "shipment_id": str(shipment.id),
            "snapshot_id": str(command.snapshot_id),
            "pickup_state_version": shipment.pickup_state_version,
            "container_state_versions": {str(c.id): c.state_version for c in containers},
        }
    await initiate_container_delivery_command(
        session, principal, shipment.id, command.container_id, command.container_code
    )
    return None


async def sync_offline_command(
    session: AsyncSession,
    principal: Principal,
    command: CourierOfflinePickupCommand | CourierOfflineContainerDeliveryCommand,
) -> dict:
    try:
        shipment = await authorize_sync_command(session, principal, command.shipment_id)
        if shipment is None:
            # Do not expose stored outcomes or consume a key when current scope is absent.
            await session.rollback()
            return command_result(
                command, "REJECTED", code="RESOURCE_NOT_FOUND", message="Resource not found"
            )
        command_data = command.model_dump(mode="json")
        request_hash = canonical_request_hash(command_data)
        tombstone = await lock_offline_command(
            session, actor_id=principal.user_id, command_id=str(command.client_command_id)
        )
        if tombstone is not None:
            if tombstone.request_hash != request_hash:
                result = command_result(
                    command,
                    "REJECTED",
                    code="IDEMPOTENCY_KEY_REUSED",
                    message="Command ID was already used with another request",
                )
            else:
                detail = await session.scalar(
                    select(IdempotencyRecord).where(
                        IdempotencyRecord.actor_user_id == principal.user_id,
                        IdempotencyRecord.operation == OFFLINE_OPERATION,
                        IdempotencyRecord.key == str(command.client_command_id),
                    )
                )
                result = {
                    **command_result(command, "ALREADY_APPLIED"),
                    **(
                        detail.response_body
                        if detail and detail.response_body
                        else tombstone.replay_response
                    ),
                    "outcome": "ALREADY_APPLIED",
                    "original_outcome": tombstone.original_outcome,
                    "recorded_at": tombstone.recorded_at.isoformat(),
                }
            await session.commit()
            return result
        try:
            # Definite rejections may follow partially executed shared commands.
            # Roll that work back before recording the terminal outcome.
            async with session.begin_nested():
                causal_metadata = await apply_offline_command(session, principal, shipment, command)
            result = command_result(command, "APPLIED", server_status=shipment.status)
        except (CommandNotApplied, HTTPException) as error:
            await session.refresh(shipment)
            if isinstance(error, HTTPException):
                if error.status_code >= 500:
                    raise
                outcome, code, message = "REJECTED", "COMMAND_REJECTED", str(error.detail)
            else:
                outcome, code, message = error.outcome, error.code, error.message
            if outcome == "RETRYABLE":
                await session.rollback()
                return command_result(command, outcome, code=code, message=message)
            causal_metadata = None
            result = command_result(
                command, outcome, server_status=shipment.status, code=code, message=message
            )
        result = await persist_offline_result(
            session,
            actor_id=principal.user_id,
            command=command_data,
            request_hash=request_hash,
            result=result,
            causal_metadata=causal_metadata,
        )
        await session.commit()
        return result
    except Exception as error:
        await session.rollback()
        logger.error(
            "Offline command %s rolled back (%s)", command.client_command_id, type(error).__name__
        )
        return command_result(
            command,
            "RETRYABLE",
            code="SYNC_TEMPORARILY_UNAVAILABLE",
            message="Command was not recorded; retry the exact command",
        )


@router.post(
    "/sync",
    response_model=CourierSyncResponse,
    responses={**OFFLINE_API_ERROR_RESPONSES, 200: OFFLINE_MANIFEST_SUCCESS_RESPONSE},
    operation_id="syncCourierOfflineCommands",
    description=(
        "Strict dependency-based stale check: whole Pickup custody phase for Pickup, "
        "target Container/Handoff for Delivery; sealed manifest and live Courier auth always "
        "checked before replay. Unrelated Shipment.version increments do not cause rejection. "
        "Snapshots older than 24h yield stored REVIEW_REQUIRED without custody mutation. "
        "Delivery may name an applied same-actor/Shipment/snapshot Pickup using "
        "depends_on_command_id, only while current Pickup and target versions exactly equal "
        "that predecessor's recorded post-state. No general automatic rebase is allowed."
    ),
    summary=(
        "Per-command replay: full response 90 days then tombstone; >24h snapshot "
        "REVIEW_REQUIRED, new live action after manual review"
    ),
)
async def sync_courier_offline_commands(
    body: CourierSyncRequest,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict:
    # Pydantic validates the entire batch before any individual transaction starts.
    # Keep processing serial: prior acknowledgements survive every later rollback.
    results = []
    for command in body.commands:
        results.append(await sync_offline_command(session, principal, command))
    return {"results": results}
