"""Region-scoped aggregate analytics; never exposes Shipment or Sample IDs."""

from datetime import UTC
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import AwareDatetime
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import API_ERROR_RESPONSES
from app.db.session import get_db_session
from app.modules.analytics.schemas import ShipmentSummary
from app.modules.analytics.service import shipment_summary
from app.modules.identity.access import Principal, get_current_principal

router = APIRouter(prefix="/analytics", tags=["Analytics"])


@router.get(
    "/shipments/summary",
    response_model=ShipmentSummary,
    operation_id="getShipmentSummary",
    summary="UTC calendar day/week analytics, full Shipment delivery mean excludes partial "
    "deliveries, latest snapshot without reopening double-count",
    description="from inclusive; to exclusive; range selects Shipment.created_at; UTC day "
    "boundaries and Monday-start UTC weeks. Available as aggregates only to active "
    "REGIONAL_ANALYST and REGIONAL_OPERATIONS members in the selected region. Infer one "
    "active region; require region_id for multi-region users. organization_id only narrows "
    "the selected region.",
    responses={
        **{status: API_ERROR_RESPONSES[status] for status in (401, 403, 404, 409, 422, 503)},
        200: {
            "description": "Success",
            "headers": {
                "X-Request-ID": {
                    "description": "Correlates server logs and audit events",
                    "schema": {"type": "string", "format": "uuid"},
                }
            },
        },
    },
)
async def get_shipment_summary(
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    region_id: UUID | None = None,
    organization_id: UUID | None = None,
    from_at: Annotated[AwareDatetime | None, Query(alias="from")] = None,
    to_at: Annotated[AwareDatetime | None, Query(alias="to")] = None,
    granularity: Literal["day", "week"] = "day",
) -> ShipmentSummary:
    return await shipment_summary(
        session,
        principal,
        region_id=region_id,
        organization_id=organization_id,
        from_at=from_at.astimezone(UTC) if from_at is not None else None,
        to_at=to_at.astimezone(UTC) if to_at is not None else None,
        granularity=granularity,
    )
