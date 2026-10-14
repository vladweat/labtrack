"""Identity and authorization-scope persistence models."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship, synonym

from app.db.base import Base


class Region(Base):
    __tablename__ = "regions"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    organizations: Mapped[list["Organization"]] = relationship(back_populates="region")


class Organization(Base):
    __tablename__ = "organizations"
    __table_args__ = (
        UniqueConstraint("id", "region_id", name="uq_organizations_id_region"),
        CheckConstraint(
            "kind IN ('HEALTHCARE_FACILITY', 'LABORATORY', 'LOGISTICS')",
            name="valid_organization_kind",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    region_id: Mapped[UUID] = mapped_column(
        ForeignKey("regions.id", ondelete="RESTRICT"), nullable=False
    )
    code: Mapped[str] = mapped_column(String(48), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    organization_type: Mapped[str] = mapped_column("kind", String(24), nullable=False)
    kind = synonym("organization_type")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    region: Mapped[Region] = relationship(back_populates="organizations")
    memberships: Mapped[list["Membership"]] = relationship(back_populates="organization")


class User(Base):
    __tablename__ = "users"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(160), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(512), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    memberships: Mapped[list["Membership"]] = relationship(back_populates="user")


class Membership(Base):
    __tablename__ = "memberships"
    __table_args__ = (
        CheckConstraint(
            "(organization_id IS NOT NULL AND region_id IS NULL) OR "
            "(organization_id IS NULL AND region_id IS NOT NULL)",
            name="exactly_one_scope",
        ),
        Index("ix_memberships_user_id", "user_id"),
        Index(
            "uq_membership_org_grant",
            "user_id",
            "organization_id",
            "role",
            unique=True,
            postgresql_where=text("organization_id IS NOT NULL"),
        ),
        Index(
            "uq_membership_region_grant",
            "user_id",
            "region_id",
            "role",
            unique=True,
            postgresql_where=text("region_id IS NOT NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    organization_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE")
    )
    region_id: Mapped[UUID | None] = mapped_column(ForeignKey("regions.id", ondelete="CASCADE"))
    role: Mapped[str] = mapped_column(String(40), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    user: Mapped[User] = relationship(back_populates="memberships")
    organization: Mapped[Organization | None] = relationship(back_populates="memberships")
