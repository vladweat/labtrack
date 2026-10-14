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
        CheckConstraint("status IN ('DRAFT', 'READY_FOR_PICKUP')", name="manifest_status"),
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
    created_by_user_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    containers: Mapped[list["TransportContainer"]] = relationship(
        back_populates="shipment", cascade="all, delete-orphan"
    )


class TransportContainer(Base):
    __tablename__ = "transport_containers"
    __table_args__ = (
        Index("ix_containers_shipment", "shipment_id"),
        UniqueConstraint("shipment_id", "id", name="uq_container_shipment_id"),
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
