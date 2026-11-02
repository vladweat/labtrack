"""Safe case metadata for operational monitoring; no sample data."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class StalledShipment(BaseModel):
    """One current PostgreSQL-backed stalled case. The same Shipment may have multiple cases.

    container_id is set for DELIVERY_PENDING and PROPOSAL_PENDING; proposal_id is set only
    for PROPOSAL_PENDING.
    """

    model_config = ConfigDict(extra="forbid")

    shipment_id: UUID
    status: Literal[
        "DRAFT",
        "READY_FOR_PICKUP",
        "PICKUP_PENDING",
        "IN_TRANSIT",
        "DELIVERY_PENDING",
        "DELIVERY_BLOCKED",
        "PARTIALLY_DELIVERED",
        "RECEIVING",
    ]
    region_id: UUID
    last_event_at: datetime = Field(
        description="Threshold-driving Handoff/proposal timestamp or Shipment expected_delivery_at "
        "deadline."
    )
    age_seconds: int = Field(
        ge=0, description="Whole seconds since last_event_at, using the database clock."
    )
    case_type: Literal[
        "PICKUP_PENDING", "DELIVERY_PENDING", "PROPOSAL_PENDING", "SHIPMENT_OVERDUE"
    ] = Field(description="Case threshold/category.")
    container_id: UUID | None
    proposal_id: UUID | None


class PagedStalledShipment(BaseModel):
    """Paged stalled cases; total counts cases, not distinct Shipments."""

    model_config = ConfigDict(extra="forbid")

    items: list[StalledShipment]
    total: int = Field(ge=0, description="Total qualifying case count before pagination.")
    limit: int = Field(ge=1, le=100)
    offset: int = Field(ge=0)
