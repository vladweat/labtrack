"""Authenticated operational cases queried directly from business state."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.errors import API_ERROR_RESPONSES
from app.db.session import get_db_session
from app.modules.identity.access import Principal, get_current_principal
from app.modules.operations.schemas import PagedStalledShipment
from app.modules.operations.service import list_stalled_cases

router = APIRouter(prefix="/operations", tags=["Operations"])


@router.get(
    "/stalled-shipments",
    response_model=PagedStalledShipment,
    operation_id="listStalledShipments",
    summary="List PostgreSQL-backed stalled operation cases",
    description="Active REGIONAL_OPERATIONS can view cases across authorized regions. "
    "SENDER_OPERATOR sees owned Shipment Pickup/overdue cases; the assigned COURIER sees "
    "their own Pickup/Delivery/overdue cases; destination LAB_OPERATOR sees Delivery/overdue "
    "cases; destination LAB_MANAGER additionally sees destination pending-proposal cases. "
    "REGIONAL_ANALYST receives no case rows. Optional region_id only narrows results. Each "
    "item represents one qualifying case; a Shipment may appear multiple times. The source "
    "is PostgreSQL business state, independent of notification delivery. Case age is measured "
    "from its threshold-driving event or expected-delivery deadline. Total counts cases before "
    "pagination; order oldest last_event_at first, then case type, Shipment ID, and nullable "
    "case IDs.",
    responses={
        **{status: API_ERROR_RESPONSES[status] for status in (401, 403, 404, 409, 422, 503)},
        200: {
            "description": "Success",
            "headers": {"X-Request-ID": {"schema": {"type": "string", "format": "uuid"}}},
        },
    },
)
async def list_stalled_shipments(
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    region_id: UUID | None = None,
    older_than_seconds: Annotated[int | None, Query(ge=0)] = None,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> PagedStalledShipment:
    return await list_stalled_cases(
        session,
        principal,
        settings,
        region_id=region_id,
        older_than_seconds=older_than_seconds,
        limit=limit,
        offset=offset,
    )
