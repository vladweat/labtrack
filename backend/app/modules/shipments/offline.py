"""Authenticated Courier offline snapshot endpoints."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import API_ERROR_RESPONSES
from app.db.session import get_db_session
from app.modules.identity.access import Principal, get_current_principal
from app.modules.shipments.commands import active_assigned_courier
from app.modules.shipments.models import CourierManifestSnapshot, Shipment, TransportContainer
from app.modules.shipments.schemas import CourierOfflineManifest

router = APIRouter(prefix="/courier/shipments", tags=["CourierOffline"])
OFFLINE_API_ERROR_RESPONSES = {
    status: API_ERROR_RESPONSES[status] for status in (401, 403, 404, 409, 422, 503)
}
OFFLINE_MANIFEST_SUCCESS_RESPONSE = {
    "description": "Success",
    "headers": {"X-Request-ID": {"schema": {"type": "string", "format": "uuid"}}},
}


@router.get(
    "/{shipment_id}/offline-manifest",
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
