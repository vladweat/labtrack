"""Shipment aggregate and immutable manifest persistence."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
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
    state_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
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
