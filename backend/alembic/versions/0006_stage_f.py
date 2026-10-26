"""Add append-only Sample receipt revisions and database guards."""

# PostgreSQL trigger bodies are embedded as multiline SQL strings.
# ruff: noqa: E501

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.config import get_settings

revision: str = "0006_stage_f"
down_revision: str | Sequence[str] | None = "0005_stage_e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "receipt_revisions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("receiving_session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("container_item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("old_result", sa.String(16)),
        sa.Column("new_result", sa.String(16), nullable=False),
        sa.Column("old_reason_code", sa.String(48)),
        sa.Column("new_reason_code", sa.String(48)),
        sa.Column("old_comment", sa.String(1000)),
        sa.Column("new_comment", sa.String(1000)),
        sa.Column("changed_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "changed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_receipt_revisions")),
        sa.ForeignKeyConstraint(
            ["receiving_session_id"],
            ["receiving_sessions.id"],
            name=op.f("fk_receipt_revisions_receiving_session_id_receiving_sessions"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["container_item_id"],
            ["container_items.id"],
            name=op.f("fk_receipt_revisions_container_item_id_container_items"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["old_reason_code"],
            ["issue_reason_codes.code"],
            name=op.f("fk_receipt_revisions_old_reason_code_issue_reason_codes"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["new_reason_code"],
            ["issue_reason_codes.code"],
            name=op.f("fk_receipt_revisions_new_reason_code_issue_reason_codes"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["changed_by"],
            ["users.id"],
            name=op.f("fk_receipt_revisions_changed_by_users"),
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "old_result IS NULL OR old_result IN ('ACCEPTED', 'MISSING', 'REJECTED')",
            name=op.f("ck_receipt_revisions_old_result"),
        ),
        sa.CheckConstraint(
            "new_result IN ('ACCEPTED', 'MISSING', 'REJECTED')",
            name=op.f("ck_receipt_revisions_new_result"),
        ),
    )
    op.create_index(
        "ix_receipt_revisions_item_changed_at",
        "receipt_revisions",
        ["container_item_id", "changed_at"],
    )

    op.execute("""
    CREATE FUNCTION guard_receipt_revision() RETURNS trigger AS $$
    BEGIN
      IF TG_OP='INSERT' THEN
        NEW.changed_at := clock_timestamp();
        RETURN NEW;
      END IF;
      RAISE EXCEPTION 'Receipt revisions are append-only';
    END; $$ LANGUAGE plpgsql
    """)
    op.execute("""
    CREATE TRIGGER trg_guard_receipt_revision BEFORE INSERT OR UPDATE OR DELETE
    ON receipt_revisions FOR EACH ROW EXECUTE FUNCTION guard_receipt_revision()
    """)
    op.execute("""
    CREATE FUNCTION guard_container_item_receipt_result() RETURNS trigger AS $$
    BEGIN
      IF TG_OP='INSERT' THEN
        IF NEW.receipt_result IS NULL AND NEW.receipt_recorded_at IS NULL AND
           NEW.receipt_reason_code IS NULL AND NEW.receipt_comment IS NULL THEN
          RETURN NEW;
        END IF;
      ELSIF NEW.receipt_result IS NOT DISTINCT FROM OLD.receipt_result AND
            NEW.receipt_recorded_at IS NOT DISTINCT FROM OLD.receipt_recorded_at AND
            NEW.receipt_reason_code IS NOT DISTINCT FROM OLD.receipt_reason_code AND
            NEW.receipt_comment IS NOT DISTINCT FROM OLD.receipt_comment THEN
        RETURN NEW;
      END IF;
      IF NOT EXISTS (
        SELECT 1
        FROM transport_containers c
        WHERE c.id=NEW.container_id
          AND c.delivery_status='RECEIVED'
          AND c.receipt_evidence IS NOT NULL
          AND EXISTS (
            SELECT 1 FROM container_receipt_evidence e WHERE e.container_id=c.id
          )
          AND EXISTS (
            SELECT 1 FROM receiving_sessions rs
            WHERE rs.shipment_id=c.shipment_id AND rs.status='OPEN'
          )
      ) THEN
        RAISE EXCEPTION 'Receipt result requires a received container with verified receipt evidence and an OPEN receiving session';
      END IF;
      RETURN NEW;
    END; $$ LANGUAGE plpgsql
    """)
    op.execute("""
    CREATE TRIGGER trg_guard_container_item_receipt_result BEFORE INSERT OR UPDATE
    ON container_items FOR EACH ROW EXECUTE FUNCTION guard_container_item_receipt_result()
    """)

    app_role = op.get_bind().dialect.identifier_preparer.quote(get_settings().app_postgres_user)
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE receipt_revisions TO {app_role}")


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_guard_container_item_receipt_result ON container_items")
    op.execute("DROP FUNCTION IF EXISTS guard_container_item_receipt_result()")
    op.execute("DROP TRIGGER IF EXISTS trg_guard_receipt_revision ON receipt_revisions")
    op.execute("DROP FUNCTION IF EXISTS guard_receipt_revision()")
    op.drop_index("ix_receipt_revisions_item_changed_at", table_name="receipt_revisions")
    op.drop_table("receipt_revisions")
