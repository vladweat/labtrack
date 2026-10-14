"""Shipment and manifest API contracts."""

from datetime import datetime
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, Field


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


class ShipmentResponse(BaseModel):
    id: UUID
    code: str
    region_id: UUID
    sender_org_id: UUID
    destination_org_id: UUID
    courier_user_id: UUID | None
    status: str
    version: int
    manifest_revision: int
    expected_delivery_at: datetime | None
    sealed_at: datetime | None
    created_at: datetime
    updated_at: datetime | None = None
    completed_at: datetime | None = None
    closed_at: datetime | None = None
    closed_by: UUID | None = None
    closure_reason: str | None = None


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
    delivery_status: str
    items: list[ContainerItemResponse] = Field(default_factory=list)
    receipt_evidence_method: str | None = None


class TransportContainerResponse(BaseModel):
    id: UUID
    code: str
    shipment_id: UUID
    created_at: datetime
    delivery_status: str
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
    status: str
    sealed_at: datetime | None
    manifest_revision: int
    containers: list[ContainerResponse]
