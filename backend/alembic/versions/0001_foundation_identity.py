"""Create identity and authorization-scope tables.

Revision ID: 0001_foundation_identity
Revises:
Create Date: 2026-11-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0001_foundation_identity"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "regions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_regions")),
        sa.UniqueConstraint("code", name=op.f("uq_regions_code")),
    )
    op.create_table(
        "organizations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("region_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("code", sa.String(length=48), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("organization_type", sa.String(length=24), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.ForeignKeyConstraint(
            ["region_id"],
            ["regions.id"],
            name=op.f("fk_organizations_region_id_regions"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_organizations")),
        sa.UniqueConstraint("code", name=op.f("uq_organizations_code")),
    )
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("display_name", sa.String(length=160), nullable=False),
        sa.Column("password_hash", sa.String(length=512), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("email", name=op.f("uq_users_email")),
    )
    op.create_table(
        "memberships",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("region_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("role", sa.String(length=40), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "(organization_id IS NOT NULL AND region_id IS NULL) OR "
            "(organization_id IS NULL AND region_id IS NOT NULL)",
            name=op.f("ck_memberships_exactly_one_scope"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_memberships_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["region_id"],
            ["regions.id"],
            name=op.f("fk_memberships_region_id_regions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_memberships_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_memberships")),
    )
    op.create_index(
        "uq_membership_org_grant",
        "memberships",
        ["user_id", "organization_id", "role"],
        unique=True,
        postgresql_where=sa.text("organization_id IS NOT NULL"),
    )
    op.create_index(
        "uq_membership_region_grant",
        "memberships",
        ["user_id", "region_id", "role"],
        unique=True,
        postgresql_where=sa.text("region_id IS NOT NULL"),
    )
    op.create_index("ix_memberships_user_id", "memberships", ["user_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_memberships_user_id", table_name="memberships")
    op.drop_index("uq_membership_region_grant", table_name="memberships")
    op.drop_index("uq_membership_org_grant", table_name="memberships")
    op.drop_table("memberships")
    op.drop_table("users")
    op.drop_table("organizations")
    op.drop_table("regions")
