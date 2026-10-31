"""Shipment aggregate and immutable manifest persistence."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class Shipment(Base):
    __tablename__ = "shipments"
    __table_args__ = (
        ForeignKeyConstraint(
            ["sender_organization_id", "region_id"],
            ["organizations.id", "organizations.region_id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(["region_id"], ["regions.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(
            ["destination_organization_id", "region_id"],
            ["organizations.id", "organizations.region_id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "status IN ('DRAFT', 'READY_FOR_PICKUP', 'PICKUP_PENDING', 'IN_TRANSIT', "
            "'DELIVERY_PENDING', 'DELIVERY_BLOCKED', 'PARTIALLY_DELIVERED', 'RECEIVING', "
            "'COMPLETED', 'COMPLETED_WITH_ISSUES', 'CANCELLED', 'CLOSED_UNDELIVERED')",
            name="manifest_status",
        ),
        CheckConstraint(
            "(status = 'CANCELLED' AND closed_at IS NOT NULL AND closed_by IS NOT NULL "
            "AND length(trim(closure_reason)) > 0) OR "
            "(status <> 'CANCELLED' AND closed_at IS NULL AND closed_by IS NULL "
            "AND closure_reason IS NULL)",
            name="closure_fields",
        ),
        CheckConstraint("pickup_state_version > 0", name="positive_pickup_state_version"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    code: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    region_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    sender_organization_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    destination_organization_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    courier_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )
    status: Mapped[str] = mapped_column(String(24), nullable=False, server_default=text("'DRAFT'"))
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    manifest_revision: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1")
    )
    pickup_state_version: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("1")
    )
    expected_delivery_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sealed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    closure_reason: Mapped[str | None] = mapped_column(String(500))
    created_by_user_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    containers: Mapped[list["TransportContainer"]] = relationship(
        back_populates="shipment", cascade="all, delete-orphan"
    )
    receiving_sessions: Mapped[list["ReceivingSession"]] = relationship(
        back_populates="shipment", cascade="all, delete-orphan"
    )


class TransportContainer(Base):
    __tablename__ = "transport_containers"
    __table_args__ = (
        Index("ix_containers_shipment", "shipment_id"),
        UniqueConstraint("shipment_id", "id", name="uq_container_shipment_id"),
        CheckConstraint(
            "delivery_status IN ('PREPARED', 'IN_TRANSIT', 'DELIVERY_PENDING', 'RECEIVED', "
            "'BLOCKED', 'UNDELIVERED_CLOSED')",
            name="delivery_status",
        ),
        CheckConstraint(
            "(delivery_status='RECEIVED' AND received_at IS NOT NULL AND "
            "receipt_evidence IS NOT NULL AND "
            "((receipt_evidence->>'method'='NORMAL' AND received_by IS NOT NULL) OR "
            "(receipt_evidence->>'method'='EXCEPTIONAL' AND received_by IS NULL))) OR "
            "(delivery_status<>'RECEIVED' AND received_at IS NULL AND received_by IS NULL "
            "AND receipt_evidence IS NULL)",
            name="receipt_fields",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    shipment_id: Mapped[UUID] = mapped_column(
        ForeignKey("shipments.id", ondelete="CASCADE"), nullable=False
    )
    code: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    delivery_status: Mapped[str] = mapped_column(
        String(24), nullable=False, server_default=text("'PREPARED'")
    )
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    received_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    state_version: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("1"))
    receipt_evidence: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    shipment: Mapped[Shipment] = relationship(back_populates="containers")
    items: Mapped[list["ContainerItem"]] = relationship(cascade="all, delete-orphan")


class ContainerItem(Base):
    __tablename__ = "container_items"
    __table_args__ = (Index("ix_items_container", "container_id"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    container_id: Mapped[UUID] = mapped_column(
        ForeignKey("transport_containers.id", ondelete="CASCADE"), nullable=False
    )
    sample_id: Mapped[UUID] = mapped_column(
        ForeignKey("samples.id", ondelete="RESTRICT"), unique=True, nullable=False
    )
    receipt_result: Mapped[str | None] = mapped_column(String(16))
    receipt_recorded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    receipt_reason_code: Mapped[str | None] = mapped_column(
        ForeignKey("issue_reason_codes.code", ondelete="RESTRICT")
    )
    receipt_comment: Mapped[str | None] = mapped_column(String(1000))
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Handoff(Base):
    """Append-only custody attempt attached to a shipment aggregate."""

    __tablename__ = "handoffs"
    __table_args__ = (
        CheckConstraint("kind IN ('PICKUP', 'DELIVERY')", name="handoff_kind"),
        CheckConstraint(
            "status IN ('PENDING', 'CONFIRMED', 'WITHDRAWN', 'SUPERSEDED')",
            name="handoff_status",
        ),
        CheckConstraint("attempt_no > 0", name="positive_attempt"),
        CheckConstraint(
            "(kind = 'PICKUP' AND container_id IS NULL) OR "
            "(kind = 'DELIVERY' AND container_id IS NOT NULL)",
            name="handoff_container_scope",
        ),
        CheckConstraint(
            "(status = 'PENDING' AND confirmed_by IS NULL AND confirmed_at IS NULL "
            "AND withdrawn_by IS NULL AND withdrawn_at IS NULL AND withdrawal_reason IS NULL "
            "AND superseded_by_incident_id IS NULL AND superseded_at IS NULL) OR "
            "(status = 'CONFIRMED' AND confirmed_by IS NOT NULL AND confirmed_at IS NOT NULL "
            "AND withdrawn_by IS NULL AND withdrawn_at IS NULL AND withdrawal_reason IS NULL "
            "AND superseded_by_incident_id IS NULL AND superseded_at IS NULL) OR "
            "(status = 'WITHDRAWN' AND confirmed_by IS NULL AND confirmed_at IS NULL "
            "AND withdrawn_by IS NOT NULL AND withdrawn_at IS NOT NULL "
            "AND length(trim(withdrawal_reason)) > 0 AND superseded_by_incident_id IS NULL "
            "AND superseded_at IS NULL) OR "
            "(status = 'SUPERSEDED' AND superseded_by_incident_id IS NOT NULL "
            "AND superseded_at IS NOT NULL)",
            name="handoff_terminal_fields",
        ),
        Index("ix_handoffs_shipment_kind_status", "shipment_id", "kind", "status"),
        Index(
            "uq_handoff_pickup_attempt",
            "shipment_id",
            "attempt_no",
            unique=True,
            postgresql_where=text("kind = 'PICKUP'"),
        ),
        Index(
            "uq_handoff_delivery_attempt",
            "container_id",
            "attempt_no",
            unique=True,
            postgresql_where=text("kind = 'DELIVERY'"),
        ),
        Index(
            "uq_handoff_pickup_active_or_confirmed",
            "shipment_id",
            unique=True,
            postgresql_where=text("kind = 'PICKUP' AND status IN ('PENDING', 'CONFIRMED')"),
        ),
        Index(
            "uq_handoff_delivery_active_or_confirmed",
            "container_id",
            unique=True,
            postgresql_where=text("kind = 'DELIVERY' AND status IN ('PENDING', 'CONFIRMED')"),
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    shipment_id: Mapped[UUID] = mapped_column(
        ForeignKey("shipments.id", ondelete="RESTRICT"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    container_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("transport_containers.id", ondelete="RESTRICT")
    )
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'PENDING'")
    )
    initiated_by: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    initiated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    confirmed_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    withdrawn_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    withdrawn_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    withdrawal_reason: Mapped[str | None] = mapped_column(String(500))
    superseded_by_incident_id: Mapped[UUID | None] = mapped_column(Uuid)
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CourierManifestSnapshot(Base):
    """Server-issued, immutable PHI-free state cut for a Courier manifest."""

    __tablename__ = "courier_manifest_snapshots"
    __table_args__ = (
        CheckConstraint("manifest_revision > 0", name="positive_manifest_revision"),
        CheckConstraint("pickup_state_version > 0", name="positive_pickup_state_version"),
        CheckConstraint(
            "is_phi_free_offline_container_payload(container_payload)",
            name="phi_free_container_payload",
        ),
        Index("ix_courier_manifest_snapshots_shipment_issued", "shipment_id", "issued_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    actor_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    assigned_courier_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    shipment_id: Mapped[UUID] = mapped_column(
        ForeignKey("shipments.id", ondelete="RESTRICT"), nullable=False
    )
    region_id: Mapped[UUID] = mapped_column(
        ForeignKey("regions.id", ondelete="RESTRICT"), nullable=False
    )
    manifest_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    pickup_state_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp(), nullable=False
    )
    policy_version: Mapped[str] = mapped_column(String(32), nullable=False)
    container_payload: Mapped[list[dict]] = mapped_column(JSONB, nullable=False)


class ReceivingSession(Base):
    """One receiving cycle, opened with the first container receipt."""

    __tablename__ = "receiving_sessions"
    __table_args__ = (
        UniqueConstraint("shipment_id", "sequence", name="uq_receiving_session_sequence"),
        CheckConstraint("sequence > 0", name="positive_sequence"),
        CheckConstraint("status IN ('OPEN', 'CLOSED')", name="receiving_session_status"),
        CheckConstraint(
            "(status='OPEN' AND closed_by IS NULL AND closed_at IS NULL) OR "
            "(status='CLOSED' AND closed_by IS NOT NULL AND closed_at IS NOT NULL)",
            name="receiving_session_close_fields",
        ),
        Index(
            "uq_receiving_session_open",
            "shipment_id",
            unique=True,
            postgresql_where=text("status = 'OPEN'"),
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    shipment_id: Mapped[UUID] = mapped_column(
        ForeignKey("shipments.id", ondelete="RESTRICT"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'OPEN'"))
    opened_by: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    opened_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    reopen_reason: Mapped[str | None] = mapped_column(String(500))
    closed_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    shipment: Mapped[Shipment] = relationship(back_populates="receiving_sessions")


class ContainerIssue(Base):
    """A reported discrepancy that blocks one transport container."""

    __tablename__ = "container_issues"
    __table_args__ = (
        ForeignKeyConstraint(
            ["shipment_id", "container_id"],
            ["transport_containers.shipment_id", "transport_containers.id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint("kind IN ('MISSING', 'REJECTED')", name="container_issue_kind"),
        CheckConstraint("status IN ('OPEN', 'RESOLVED', 'CLOSED_UNDELIVERED')", name="status"),
        CheckConstraint(
            "(status='OPEN' AND resolved_by IS NULL AND resolved_at IS NULL "
            "AND resolution IS NULL) OR "
            "(status IN ('RESOLVED','CLOSED_UNDELIVERED') AND resolved_by IS NOT NULL "
            "AND resolved_at IS NOT NULL AND resolution IS NOT NULL)",
            name="resolution_fields",
        ),
        Index("ix_container_issues_shipment_created", "shipment_id", "reported_at"),
        Index(
            "uq_container_issue_open",
            "container_id",
            unique=True,
            postgresql_where=text("status = 'OPEN'"),
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    shipment_id: Mapped[UUID] = mapped_column(
        ForeignKey("shipments.id", ondelete="RESTRICT"), nullable=False
    )
    container_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    reason_code: Mapped[str] = mapped_column(
        ForeignKey("issue_reason_codes.code", ondelete="RESTRICT"), nullable=False
    )
    comment: Mapped[str | None] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(24), nullable=False, server_default=text("'OPEN'"))
    reported_by: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    reported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    resolved_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolution: Mapped[str | None] = mapped_column(String(24))


class ContainerIssueResolutionProposal(Base):
    """A regional proposal and independent laboratory decision for an issue."""

    __tablename__ = "container_issue_resolution_proposals"
    __table_args__ = (
        CheckConstraint("action IN ('RETRY_DELIVERY', 'CLOSE_UNDELIVERED')", name="action"),
        CheckConstraint(
            "status IN ('PENDING', 'APPROVED', 'REJECTED', 'WITHDRAWN')", name="status"
        ),
        CheckConstraint(
            "(status='PENDING' AND decided_by IS NULL AND decided_at IS NULL "
            "AND decision_reason IS NULL AND withdrawn_by IS NULL AND withdrawn_at IS NULL "
            "AND withdrawal_reason IS NULL) OR "
            "(status IN ('APPROVED','REJECTED') AND decided_by IS NOT NULL "
            "AND decided_at IS NOT NULL "
            "AND proposed_by <> decided_by AND withdrawn_by IS NULL AND withdrawn_at IS NULL "
            "AND withdrawal_reason IS NULL AND (status <> 'REJECTED' "
            "OR length(trim(decision_reason)) > 0)) OR "
            "(status='WITHDRAWN' AND decided_by IS NULL AND decided_at IS NULL "
            "AND withdrawn_by=proposed_by AND withdrawn_at IS NOT NULL "
            "AND length(trim(withdrawal_reason)) > 0)",
            name="transition_fields",
        ),
        Index("ix_issue_proposals_issue_created", "issue_id", "proposed_at"),
        Index(
            "uq_issue_pending_proposal",
            "issue_id",
            unique=True,
            postgresql_where=text("status = 'PENDING'"),
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    issue_id: Mapped[UUID] = mapped_column(
        ForeignKey("container_issues.id", ondelete="RESTRICT"), nullable=False
    )
    action: Mapped[str] = mapped_column(String(24), nullable=False)
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'PENDING'")
    )
    proposed_by: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    proposed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    decided_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_reason: Mapped[str | None] = mapped_column(String(500))
    withdrawn_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    withdrawn_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    withdrawal_reason: Mapped[str | None] = mapped_column(String(500))


class CustodyIncident(Base):
    """Administrative review record, separate from custody handoff actors."""

    __tablename__ = "custody_incidents"
    __table_args__ = (
        CheckConstraint("phase IN ('PICKUP', 'DELIVERY')", name="phase"),
        CheckConstraint(
            "kind IN ('COURIER_UNAVAILABLE_AFTER_PICKUP', 'PICKUP_UNCONFIRMED', "
            "'COURIER_DELIVERY_CONFIRMATION_UNAVAILABLE', 'HANDOFF_STALLED', 'OFFLINE_CONFLICT')",
            name="kind",
        ),
        CheckConstraint(
            "status IN ('OPEN', 'PENDING_EVIDENCE', 'UNDER_REVIEW', 'RESOLVED', 'REJECTED')",
            name="status",
        ),
        CheckConstraint(
            "(phase='PICKUP' AND container_id IS NULL) OR "
            "(phase='DELIVERY' AND container_id IS NOT NULL)",
            name="phase_container_scope",
        ),
        CheckConstraint(
            "(status IN ('OPEN','PENDING_EVIDENCE','UNDER_REVIEW') AND resolved_by IS NULL "
            "AND resolved_at IS NULL AND resolution IS NULL) OR "
            "(status IN ('RESOLVED','REJECTED') AND resolved_by IS NOT NULL "
            "AND resolved_at IS NOT NULL AND resolution IS NOT NULL)",
            name="resolution_fields",
        ),
        ForeignKeyConstraint(
            ["shipment_id", "container_id"],
            ["transport_containers.shipment_id", "transport_containers.id"],
            ondelete="RESTRICT",
        ),
        Index("ix_custody_incidents_shipment_opened", "shipment_id", "opened_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    shipment_id: Mapped[UUID] = mapped_column(
        ForeignKey("shipments.id", ondelete="RESTRICT"), nullable=False
    )
    container_id: Mapped[UUID | None] = mapped_column(Uuid)
    handoff_id: Mapped[UUID | None] = mapped_column(ForeignKey("handoffs.id", ondelete="RESTRICT"))
    phase: Mapped[str] = mapped_column(String(16), nullable=False)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, server_default=text("'OPEN'"))
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    opened_by: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    resolved_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolution: Mapped[str | None] = mapped_column(String(40))


class CustodyAttestation(Base):
    """Append-only first-party custody observation under the actor's active scope."""

    __tablename__ = "custody_attestations"
    __table_args__ = (
        UniqueConstraint("incident_id", "user_id", name="uq_custody_attestation_actor"),
        CheckConstraint("length(trim(statement)) > 0", name="statement_nonblank"),
        CheckConstraint(
            "(role_at_time='SENDER_OPERATOR' AND organization_id IS NOT NULL AND "
            "physically_present IS NULL AND container_code IS NULL) OR "
            "(role_at_time IN ('LAB_OPERATOR','LAB_MANAGER') AND organization_id IS NOT NULL "
            "AND physically_present IS TRUE AND container_code IS NOT NULL)",
            name="role_assertion_fields",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    incident_id: Mapped[UUID] = mapped_column(
        ForeignKey("custody_incidents.id", ondelete="RESTRICT"), nullable=False
    )
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    role_at_time: Mapped[str] = mapped_column(String(32), nullable=False)
    container_code: Mapped[str | None] = mapped_column(String(40))
    statement: Mapped[str] = mapped_column(String(500), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    physically_present: Mapped[bool | None] = mapped_column(Boolean)
    physical_release_occurred: Mapped[bool | None] = mapped_column(Boolean)
    containers_at_origin: Mapped[bool | None] = mapped_column(Boolean)


class ContainerReceiptEvidence(Base):
    """Immutable physical receipt evidence, normal handoff or exceptional review."""

    __tablename__ = "container_receipt_evidence"
    __table_args__ = (
        UniqueConstraint("container_id", name="uq_container_receipt_evidence_container_id"),
        CheckConstraint("method IN ('NORMAL','EXCEPTIONAL')", name="method"),
        CheckConstraint(
            "(method='NORMAL' AND confirmed_delivery_handoff_id IS NOT NULL "
            "AND resolved_incident_id IS NULL) OR "
            "(method='EXCEPTIONAL' AND confirmed_delivery_handoff_id IS NULL "
            "AND resolved_incident_id IS NOT NULL)",
            name="method_reference",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    container_id: Mapped[UUID] = mapped_column(
        ForeignKey("transport_containers.id", ondelete="RESTRICT"), nullable=False
    )
    method: Mapped[str] = mapped_column(String(16), nullable=False)
    confirmed_delivery_handoff_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("handoffs.id", ondelete="RESTRICT")
    )
    resolved_incident_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("custody_incidents.id", ondelete="RESTRICT")
    )
    verified_by: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    verified_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ReceiptRevision(Base):
    """Append-only record of a Sample receipt classification change."""

    __tablename__ = "receipt_revisions"
    __table_args__ = (
        CheckConstraint(
            "old_result IS NULL OR old_result IN ('ACCEPTED', 'MISSING', 'REJECTED')",
            name="old_result",
        ),
        CheckConstraint(
            "new_result IN ('ACCEPTED', 'MISSING', 'REJECTED')",
            name="new_result",
        ),
        Index("ix_receipt_revisions_item_changed_at", "container_item_id", "changed_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    receiving_session_id: Mapped[UUID] = mapped_column(
        ForeignKey("receiving_sessions.id", ondelete="RESTRICT"), nullable=False
    )
    container_item_id: Mapped[UUID] = mapped_column(
        ForeignKey("container_items.id", ondelete="RESTRICT"), nullable=False
    )
    old_result: Mapped[str | None] = mapped_column(String(16))
    new_result: Mapped[str] = mapped_column(String(16), nullable=False)
    old_reason_code: Mapped[str | None] = mapped_column(
        ForeignKey("issue_reason_codes.code", ondelete="RESTRICT")
    )
    new_reason_code: Mapped[str | None] = mapped_column(
        ForeignKey("issue_reason_codes.code", ondelete="RESTRICT")
    )
    old_comment: Mapped[str | None] = mapped_column(String(1000))
    new_comment: Mapped[str | None] = mapped_column(String(1000))
    changed_by: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
