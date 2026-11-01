"""Shipment and manifest API contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, RootModel, field_validator

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


class CompleteReceivingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReopenReceivingRequest(BaseModel):
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


class CreateContainerIssueRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    container_id: UUID
    kind: Literal["MISSING", "REJECTED"]
    reason_code: str = Field(min_length=1, max_length=80)
    comment: str | None = Field(default=None, max_length=500)


class ContainerIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    shipment_id: UUID
    container_id: UUID
    kind: Literal["MISSING", "REJECTED"]
    reason_code: str
    comment: str | None
    status: Literal["OPEN", "RESOLVED", "CLOSED_UNDELIVERED"]
    reported_by: UUID
    reported_at: datetime
    resolved_by: UUID | None
    resolved_at: datetime | None
    resolution: Literal["RETRY_DELIVERY", "CLOSE_UNDELIVERED"] | None


class CreateContainerIssueResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    issue: ContainerIssue
    shipment_id: UUID
    shipment_status: ShipmentStatus


class PagedContainerIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[ContainerIssue]
    total: int
    limit: int
    offset: int


class CreateContainerResolutionProposalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["RETRY_DELIVERY", "CLOSE_UNDELIVERED"]
    reason: str = Field(min_length=1, max_length=500)


class DecideContainerResolutionProposalApprove(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["APPROVE"]
    reason: str | None = Field(default=None, max_length=500)


class DecideContainerResolutionProposalReject(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["REJECT"]
    reason: str = Field(min_length=1, max_length=500)


DecideContainerResolutionProposalRequest = Annotated[
    DecideContainerResolutionProposalApprove | DecideContainerResolutionProposalReject,
    Field(discriminator="decision"),
]


class WithdrawResolutionProposalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=500)


class ContainerIssueResolutionProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    issue_id: UUID
    action: Literal["RETRY_DELIVERY", "CLOSE_UNDELIVERED"]
    reason: str
    status: Literal["PENDING", "APPROVED", "REJECTED", "WITHDRAWN"]
    proposed_by: UUID
    proposed_at: datetime
    decided_by: UUID | None
    decided_at: datetime | None
    decision_reason: str | None
    withdrawn_by: UUID | None
    withdrawn_at: datetime | None
    withdrawal_reason: str | None


class PagedContainerIssueResolutionProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[ContainerIssueResolutionProposal]
    total: int
    limit: int
    offset: int


class DecideContainerResolutionProposalResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposal: ContainerIssueResolutionProposal
    issue: ContainerIssue
    container: TransportContainerResponse
    shipment: ShipmentResponse


class CustodyIncidentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    phase: Literal["PICKUP", "DELIVERY"]
    kind: Literal[
        "COURIER_UNAVAILABLE_AFTER_PICKUP",
        "PICKUP_UNCONFIRMED",
        "COURIER_DELIVERY_CONFIRMATION_UNAVAILABLE",
        "HANDOFF_STALLED",
        "OFFLINE_CONFLICT",
    ]
    container_id: UUID | None = None
    reason: str = Field(min_length=1, max_length=500)


class CustodyIncidentResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    shipment_id: UUID
    container_id: UUID | None
    handoff_id: UUID | None
    phase: Literal["PICKUP", "DELIVERY"]
    kind: Literal[
        "COURIER_UNAVAILABLE_AFTER_PICKUP",
        "PICKUP_UNCONFIRMED",
        "COURIER_DELIVERY_CONFIRMATION_UNAVAILABLE",
        "HANDOFF_STALLED",
        "OFFLINE_CONFLICT",
    ]
    status: Literal["OPEN", "PENDING_EVIDENCE", "UNDER_REVIEW", "RESOLVED", "REJECTED"]
    reason: str
    opened_by: UUID
    opened_at: datetime
    resolved_by: UUID | None
    resolved_at: datetime | None
    resolution: (
        Literal[
            "EXCEPTIONAL_RECEIPT_VERIFIED",
            "NO_PHYSICAL_TRANSFER",
            "ESCALATED_NO_CUSTODY_CHANGE",
            "CONTAINER_ISSUE_REFERRED",
        ]
        | None
    )


class PagedCustodyIncident(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[CustodyIncidentResponse]
    total: int
    limit: int
    offset: int


class CustodyPickupAttestationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    statement: str = Field(min_length=1, max_length=500)
    physical_release_occurred: bool | None
    containers_at_origin: bool


class CustodyDeliveryAttestationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    container_code: str = Field(min_length=1)
    statement: str = Field(min_length=1, max_length=500)
    physically_present: Literal[True]


CustodyAttestationRequest = CustodyPickupAttestationRequest | CustodyDeliveryAttestationRequest


class CustodyAttestationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    incident_id: UUID
    user_id: UUID
    organization_id: UUID
    role_at_time: str
    container_code: str | None
    statement: str
    recorded_at: datetime
    physically_present: bool | None
    physical_release_occurred: bool | None
    containers_at_origin: bool | None


class ExceptionalReceiptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=500)


class CustodyIncidentResolutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resolution: Literal[
        "EXCEPTIONAL_RECEIPT_VERIFIED",
        "NO_PHYSICAL_TRANSFER",
        "ESCALATED_NO_CUSTODY_CHANGE",
        "CONTAINER_ISSUE_REFERRED",
    ]
    reason: str = Field(min_length=1, max_length=500)


class ContainerReceiptEvidenceResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    container_id: UUID
    method: Literal["NORMAL", "EXCEPTIONAL"]
    handoff_id: UUID | None
    incident_id: UUID | None
    verified_at: datetime
    verified_by: UUID


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
    updated_at: datetime
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
    receipt_result: Literal["ACCEPTED", "MISSING", "REJECTED"] | None
    receipt_reason_code: str | None
    receipt_comment: str | None


class ContainerResponse(BaseModel):
    id: UUID
    code: str
    delivery_status: ContainerStatus
    items: list[ContainerItemResponse] = Field(default_factory=list)
    receipt_evidence_method: str | None = None


class TransportContainerResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    code: str
    shipment_id: UUID
    created_at: datetime
    delivery_status: ContainerStatus
    received_at: datetime | None
    received_by: UUID | None
    state_version: int
    receipt_evidence: ContainerReceiptEvidenceResponse | None


class ContainerItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    container_id: UUID
    sample_id: UUID
    receipt_result: Literal["ACCEPTED", "MISSING", "REJECTED"] | None
    receipt_recorded_at: datetime | None
    receipt_reason_code: str | None
    receipt_comment: str | None


ContainerItemWriteResponse = ContainerItem


class ManifestResponse(BaseModel):
    shipment_id: UUID
    status: ShipmentStatus
    sealed_at: datetime | None
    manifest_revision: int
    containers: list[ContainerResponse]


class CourierOfflineManifestContainer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    code: str
    delivery_status: ContainerStatus
    state_version: int = Field(ge=1)


class CourierOfflineManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    shipment_id: UUID
    manifest_revision: int = Field(ge=0)
    status: ShipmentStatus
    container_codes: list[str] = Field(min_length=1, json_schema_extra={"uniqueItems": True})
    cached_at: AwareDatetime = Field(
        description="Legacy response field, not authoritative issuance timestamp; use issued_at"
    )
    containers: list[CourierOfflineManifestContainer]
    snapshot_id: UUID
    issued_at: AwareDatetime
    pickup_state_version: int = Field(ge=1)

    @field_validator("container_codes")
    @classmethod
    def unique_container_codes(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("container_codes must be unique")
        return value


class AcceptedReceiptResultRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    result: Literal["ACCEPTED"]
    reason_code: None = None
    comment: None = None


class MissingReceiptResultRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    result: Literal["MISSING"]
    reason_code: str = Field(min_length=1, max_length=80)
    comment: str | None = Field(default=None, max_length=500)


class RejectedReceiptResultRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    result: Literal["REJECTED"]
    reason_code: str = Field(min_length=1, max_length=80)
    comment: str | None = Field(default=None, max_length=500)


ReceiptResultRequestUnion = Annotated[
    AcceptedReceiptResultRequest | MissingReceiptResultRequest | RejectedReceiptResultRequest,
    Field(discriminator="result"),
]


class ReceiptResultRequest(RootModel[ReceiptResultRequestUnion]):
    pass


class ReceivingSession(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: UUID
    shipment_id: UUID
    sequence: int = Field(ge=1)
    status: Literal["OPEN", "CLOSED"]
    opened_by: UUID
    opened_at: datetime
    reopen_reason: str | None
    closed_by: UUID | None
    closed_at: datetime | None


class ReceiptRevision(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: UUID
    receiving_session_id: UUID
    container_item_id: UUID
    old_result: Literal["ACCEPTED", "MISSING", "REJECTED"] | None
    new_result: Literal["ACCEPTED", "MISSING", "REJECTED"]
    old_reason_code: str | None
    new_reason_code: str | None
    old_comment: str | None = Field(max_length=500)
    new_comment: str | None = Field(max_length=500)
    changed_by: UUID
    changed_at: datetime


class ReceivingHistory(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sessions: list[ReceivingSession]
    revisions: list[ReceiptRevision]
    total_revisions: int = Field(ge=0)
    limit: int = Field(ge=1, le=100)
    offset: int = Field(ge=0)
