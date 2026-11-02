"""Aggregate-only shipment analytics contract."""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class AnalyticsPeriodBucket(BaseModel):
    model_config = ConfigDict(extra="forbid")

    period_start: datetime
    period_end: datetime
    total_shipments: int = Field(
        ge=0, description="Distinct current Shipments in this Shipment.created_at bucket."
    )
    completed_shipments: int = Field(
        ge=0, description="Current Shipments in COMPLETED or COMPLETED_WITH_ISSUES status."
    )
    partially_delivered_shipments: int = Field(
        ge=0,
        description="Current Shipments with more than zero but fewer than all containers "
        "RECEIVED, including mixed finalized Shipments.",
    )
    sample_issues: int = Field(
        ge=0, description="Count of current receipt rows with MISSING or REJECTED result."
    )
    container_issues: int = Field(
        ge=0, description="Count of containers whose latest issue outcome is MISSING or REJECTED."
    )


class AnalyticsFacility(BaseModel):
    model_config = ConfigDict(extra="forbid")

    organization_id: UUID
    shipment_count: int = Field(
        ge=0,
        description="Distinct Shipments associated with this sender or destination organization; "
        "each Shipment counts once per organization.",
    )
    completed_count: int = Field(
        ge=0,
        description="Associated Shipments currently in COMPLETED or COMPLETED_WITH_ISSUES status.",
    )
    with_issues_count: int = Field(
        ge=0, description="Associated Shipments currently in COMPLETED_WITH_ISSUES status."
    )


class ShipmentSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    region_id: UUID
    from_: datetime | None = Field(alias="from")
    to: datetime | None
    total: int = Field(ge=0, description="Distinct Shipments in the Shipment.created_at cohort.")
    by_status: dict[str, Annotated[int, Field(ge=0)]] = Field(
        description="Distinct Shipments grouped by latest current status."
    )
    completed_with_issues: int = Field(ge=0)
    missing_samples: int = Field(
        ge=0, description="Current Sample receipt rows whose result is MISSING."
    )
    rejected_samples: int = Field(
        ge=0, description="Current Sample receipt rows whose result is REJECTED."
    )
    overdue_active: int = Field(ge=0)
    closed_undelivered: int = Field(ge=0)
    container_missing: int = Field(
        ge=0, description="Containers whose latest issue outcome is MISSING."
    )
    container_rejected: int = Field(
        ge=0, description="Containers whose latest issue outcome is REJECTED."
    )
    partially_delivered: int = Field(ge=0)
    partially_completed_with_issues: int = Field(
        ge=0,
        description="Current COMPLETED_WITH_ISSUES Shipments with more than zero but fewer "
        "than all containers RECEIVED.",
    )
    average_received_container_delivery_seconds: float | None = Field(
        ge=0,
        description="Numerator: sum of confirmed Pickup-to-container-receipt durations. "
        "Denominator: RECEIVED containers in partially delivered Shipments. Undelivered "
        "containers are not zero-duration observations.",
    )
    period_buckets: list[AnalyticsPeriodBucket]
    by_facility: list[AnalyticsFacility]
    average_full_shipment_delivery_seconds: float | None = Field(
        ge=0,
        description="Numerator: sum of confirmed Pickup-to-last-confirmed-receipt durations. "
        "Denominator: Shipments with every container RECEIVED and the required timestamps. "
        "Partially and wholly undelivered Shipments are excluded.",
    )
