"""Sample and opaque external-system reference persistence."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, Index, String, UniqueConstraint, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class Sample(Base):
    __tablename__ = "samples"
    __table_args__ = (Index("ix_samples_origin_created", "origin_organization_id", "created_at"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    code: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    origin_organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    material_type: Mapped[str] = mapped_column(String(16), nullable=False)
    collected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    external_references: Mapped[list["SampleExternalReference"]] = relationship(
        cascade="all, delete-orphan"
    )


class SampleExternalReference(Base):
    __tablename__ = "sample_external_references"
    __table_args__ = (
        UniqueConstraint(
            "sample_id",
            "system_code",
            "reference_kind",
            "external_id",
            name="uq_sample_external_reference",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    sample_id: Mapped[UUID] = mapped_column(
        ForeignKey("samples.id", ondelete="CASCADE"), nullable=False
    )
    system_code: Mapped[str] = mapped_column(String(48), nullable=False)
    reference_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    external_id: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
