"""Shipment and manifest API contracts."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

ShipmentStatus = Literal[
    "DRAFT",
    "READY_FOR_PICKUP",
    "PICKUP_PENDING",
    "IN_TRANSIT",
    "DELIVERY_PENDING",
    "DELIVERY_BLOCKED",
    "PARTIALLY_DELIVERED",
    "RECEIVING",
    "COMPLETED",
    "COMPLETED_WITH_ISSUES",
    "CANCELLED",
    "CLOSED_UNDELIVERED",
]
ContainerStatus = Literal[
    "PREPARED",
    "IN_TRANSIT",
    "DELIVERY_PENDING",
    "RECEIVED",
    "BLOCKED",
    "UNDELIVERED_CLOSED",
]
HandoffKind = Literal["PICKUP", "DELIVERY"]
HandoffStatus = Literal["PENDING", "CONFIRMED", "WITHDRAWN", "SUPERSEDED"]


class CreateShipmentRequest(BaseModel):
    sender_org_id: UUID
    destination_org_id: UUID
    expected_delivery_at: AwareDatetime | None = None


class UpdateDraftShipmentRequest(BaseModel):
    expected_delivery_at: AwareDatetime | None


class AssignCourierRequest(BaseModel):
    courier_user_id: UUID


class AddSampleRequest(BaseModel):
    sample_id: UUID


class ContainerCodesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    container_codes: list[str] = Field(min_length=1, json_schema_extra={"uniqueItems": True})

    @field_validator("container_codes")
    @classmethod
    def unique_nonempty_codes(cls, value: list[str]) -> list[str]:
        if any(not code.strip() for code in value) or len(value) != len(set(value)):
            raise ValueError("container_codes must contain unique non-empty codes")
        return value


class ReasonRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=500)

    @field_validator("reason")
    @classmethod
    def nonblank_reason(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("reason must not be blank")
        return value.strip()


class ContainerCodeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    container_code: str = Field(min_length=1)

    @field_validator("container_code")
    @classmethod
    def nonblank_container_code(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("container_code must not be blank")
        return value.strip()


class HandoffResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    kind: HandoffKind
    attempt_no: int
    status: HandoffStatus
    initiated_by: UUID
    initiated_at: datetime
    confirmed_by: UUID | None
    confirmed_at: datetime | None
    withdrawn_at: datetime | None
    withdrawn_by: UUID | None
    withdrawal_reason: str | None
    container_id: UUID | None
    superseded_by_incident_id: UUID | None
    superseded_at: datetime | None


class HandoffActionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    shipment_id: UUID
    shipment_status: ShipmentStatus
    version: int
    handoff: HandoffResponse
    container_id: UUID | None
    container_status: ContainerStatus | None


class DeliveryHandoffHistory(BaseModel):
    model_config = ConfigDict(extra="forbid")

    container_id: UUID
    items: list[HandoffResponse]


class ShipmentResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    code: str
    region_id: UUID
    sender_org_id: UUID
    destination_org_id: UUID
    courier_user_id: UUID | None
    status: ShipmentStatus
    version: int
    manifest_revision: int
    expected_delivery_at: datetime | None
    sealed_at: datetime | None
    created_at: datetime
    updated_at: datetime | None
    completed_at: datetime | None
    closed_at: datetime | None
    closed_by: UUID | None
    closure_reason: str | None


class PagedShipmentResponse(BaseModel):
    items: list[ShipmentResponse]
    total: int
    limit: int
    offset: int


class ContainerItemResponse(BaseModel):
    id: UUID
    sample_id: UUID
    sample_code: str
    material_type: str
    receipt_result: str | None = None
    receipt_reason_code: str | None = None
    receipt_comment: str | None = None


class ContainerResponse(BaseModel):
    id: UUID
    code: str
    delivery_status: ContainerStatus
    items: list[ContainerItemResponse] = Field(default_factory=list)
    receipt_evidence_method: str | None = None


class TransportContainerResponse(BaseModel):
    id: UUID
    code: str
    shipment_id: UUID
    created_at: datetime
    delivery_status: ContainerStatus
    received_at: datetime | None = None
    received_by: UUID | None = None
    state_version: int = 1
    receipt_evidence: dict | None = None


class ContainerItemWriteResponse(BaseModel):
    id: UUID
    container_id: UUID
    sample_id: UUID
    receipt_result: str | None = None
    receipt_recorded_at: datetime | None = None
    receipt_reason_code: str | None = None
    receipt_comment: str | None = None


class ManifestResponse(BaseModel):
    shipment_id: UUID
    status: ShipmentStatus
    sealed_at: datetime | None
    manifest_revision: int
    containers: list[ContainerResponse]
