"""Shared custody command mutations for online and offline Courier actions."""

from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.identity.access import Principal
from app.modules.identity.models import Membership, User
from app.modules.shipments.models import Handoff, Shipment, TransportContainer


def handoff_dict(row: Handoff) -> dict:
    return {
        "id": str(row.id),
        "kind": row.kind,
        "attempt_no": row.attempt_no,
        "status": row.status,
        "initiated_by": str(row.initiated_by),
        "initiated_at": row.initiated_at.isoformat(),
        "confirmed_by": str(row.confirmed_by) if row.confirmed_by else None,
        "confirmed_at": row.confirmed_at.isoformat() if row.confirmed_at else None,
        "withdrawn_at": row.withdrawn_at.isoformat() if row.withdrawn_at else None,
        "withdrawn_by": str(row.withdrawn_by) if row.withdrawn_by else None,
        "withdrawal_reason": row.withdrawal_reason,
        "container_id": str(row.container_id) if row.container_id else None,
        "superseded_by_incident_id": (
            str(row.superseded_by_incident_id) if row.superseded_by_incident_id else None
        ),
        "superseded_at": row.superseded_at.isoformat() if row.superseded_at else None,
    }


async def lock_pickup_shipment(
    session: AsyncSession,
    principal: Principal,
    shipment_id: UUID,
    *,
    role: str,
) -> Shipment:
    row = await session.scalar(select(Shipment).where(Shipment.id == shipment_id).with_for_update())
    if row is None:
        raise HTTPException(404, "Resource not found")
    allowed = (
        principal.has_role("SENDER_OPERATOR", organization_id=row.sender_organization_id)
        if role == "SENDER_OPERATOR"
        else row.courier_user_id == principal.user_id
        and principal.has_role("COURIER", region_id=row.region_id)
    )
    if not allowed:
        raise HTTPException(404, "Resource not found")
    if row.sealed_at is None:
        raise HTTPException(409, "Shipment must be sealed before Pickup")
    return row


async def active_assigned_courier(session: AsyncSession, row: Shipment) -> bool:
    return bool(
        await session.scalar(
            select(Membership.id)
            .join(User)
            .where(
                Membership.user_id == row.courier_user_id,
                Membership.region_id == row.region_id,
                Membership.organization_id.is_(None),
                Membership.role == "COURIER",
                Membership.is_active.is_(True),
                User.is_active.is_(True),
            )
        )
    )


async def lock_delivery_scope(
    session: AsyncSession,
    principal: Principal,
    shipment_id: UUID,
    container_id: UUID,
    *,
    actor: str,
) -> tuple[Shipment, TransportContainer]:
    shipment = await session.scalar(
        select(Shipment).where(Shipment.id == shipment_id).with_for_update()
    )
    if shipment is None:
        raise HTTPException(404, "Resource not found")
    if actor == "COURIER":
        authorized = shipment.courier_user_id == principal.user_id and principal.has_role(
            "COURIER", region_id=shipment.region_id
        )
    else:
        authorized = principal.has_role(
            "LAB_OPERATOR", organization_id=shipment.destination_organization_id
        ) or principal.has_role("LAB_MANAGER", organization_id=shipment.destination_organization_id)
    if not authorized:
        raise HTTPException(404, "Resource not found")
    container = await session.scalar(
        select(TransportContainer)
        .where(
            TransportContainer.id == container_id,
            TransportContainer.shipment_id == shipment.id,
        )
        .order_by(TransportContainer.id)
        .with_for_update()
    )
    if container is None:
        raise HTTPException(404, "Resource not found")
    return shipment, container


async def derive_delivery_shipment_status(session: AsyncSession, shipment_id: UUID) -> str:
    statuses = list(
        await session.scalars(
            select(TransportContainer.delivery_status).where(
                TransportContainer.shipment_id == shipment_id
            )
        )
    )
    if statuses and all(status == "UNDELIVERED_CLOSED" for status in statuses):
        return "CLOSED_UNDELIVERED"
    if all(status in {"RECEIVED", "UNDELIVERED_CLOSED"} for status in statuses) and any(
        status == "RECEIVED" for status in statuses
    ):
        return "RECEIVING"
    if any(status == "RECEIVED" for status in statuses):
        return "PARTIALLY_DELIVERED"
    if any(status == "UNDELIVERED_CLOSED" for status in statuses):
        return "DELIVERY_BLOCKED"
    if any(status == "BLOCKED" for status in statuses):
        return "DELIVERY_BLOCKED"
    if any(status == "DELIVERY_PENDING" for status in statuses):
        return "DELIVERY_PENDING"
    return "IN_TRANSIT"


async def confirm_pickup_command(
    session: AsyncSession,
    principal: Principal,
    shipment_id: UUID,
    container_codes: list[str],
) -> tuple[int, dict]:
    row = await lock_pickup_shipment(session, principal, shipment_id, role="COURIER")
    if not await active_assigned_courier(session, row):
        raise HTTPException(404, "Resource not found")
    if row.status != "PICKUP_PENDING":
        raise HTTPException(409, "Shipment has no pending Pickup")
    handoff = await session.scalar(
        select(Handoff)
        .where(
            Handoff.shipment_id == row.id,
            Handoff.kind == "PICKUP",
            Handoff.status == "PENDING",
        )
        .with_for_update()
    )
    if handoff is None:
        raise HTTPException(409, "Shipment has no pending Pickup")
    if handoff.initiated_by == principal.user_id:
        raise HTTPException(409, "Pickup requires confirmation by a different actor")
    containers = list(
        await session.scalars(
            select(TransportContainer)
            .where(TransportContainer.shipment_id == row.id)
            .order_by(TransportContainer.id)
            .with_for_update()
        )
    )
    if sorted(container_codes) != sorted(item.code for item in containers):
        raise HTTPException(409, "Container codes must match the complete sealed manifest")
    now = await session.scalar(select(func.now()))
    handoff.status = "CONFIRMED"
    handoff.confirmed_by = principal.user_id
    handoff.confirmed_at = now
    await session.flush()
    for container in containers:
        container.delivery_status = "IN_TRANSIT"
        container.state_version += 1
    await session.flush()
    row.status = "IN_TRANSIT"
    row.version += 1
    await session.flush()
    await session.refresh(row, attribute_names=["updated_at"])
    return 200, {
        "shipment_id": str(row.id),
        "shipment_status": row.status,
        "version": row.version,
        "handoff": handoff_dict(handoff),
        "container_id": None,
        "container_status": None,
    }


async def initiate_container_delivery_command(
    session: AsyncSession,
    principal: Principal,
    shipment_id: UUID,
    container_id: UUID,
    container_code: str,
) -> tuple[int, dict]:
    shipment, container = await lock_delivery_scope(
        session, principal, shipment_id, container_id, actor="COURIER"
    )
    if not await active_assigned_courier(session, shipment):
        raise HTTPException(404, "Resource not found")
    if shipment.sealed_at is None or shipment.status not in {
        "IN_TRANSIT",
        "DELIVERY_PENDING",
        "PARTIALLY_DELIVERED",
    }:
        raise HTTPException(409, "Shipment is not in a Delivery phase")
    pickup_confirmed = await session.scalar(
        select(Handoff.id).where(
            Handoff.shipment_id == shipment.id,
            Handoff.kind == "PICKUP",
            Handoff.status == "CONFIRMED",
        )
    )
    if pickup_confirmed is None:
        raise HTTPException(409, "Whole-Shipment Pickup must be confirmed first")
    if container.delivery_status != "IN_TRANSIT":
        raise HTTPException(409, "Container is not in transit")
    if container_code != container.code:
        raise HTTPException(409, "Container code does not match the sealed manifest")
    previous_attempt = await session.scalar(
        select(func.max(Handoff.attempt_no)).where(
            Handoff.container_id == container.id,
            Handoff.kind == "DELIVERY",
        )
    )
    handoff = Handoff(
        shipment_id=shipment.id,
        kind="DELIVERY",
        container_id=container.id,
        attempt_no=(previous_attempt or 0) + 1,
        status="PENDING",
        initiated_by=principal.user_id,
    )
    session.add(handoff)
    await session.flush()
    container.delivery_status = "DELIVERY_PENDING"
    container.state_version += 1
    await session.flush()
    shipment.status = await derive_delivery_shipment_status(session, shipment.id)
    shipment.version += 1
    await session.flush()
    await session.refresh(shipment, attribute_names=["updated_at"])
    return 200, {
        "shipment_id": str(shipment.id),
        "shipment_status": shipment.status,
        "version": shipment.version,
        "handoff": handoff_dict(handoff),
        "container_id": str(container.id),
        "container_status": container.delivery_status,
    }
