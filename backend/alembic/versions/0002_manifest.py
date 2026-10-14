"""Add sample registration and immutable shipment manifests.

Revision ID: 0002_manifest
Revises: 0001_foundation_identity
"""

from collections.abc import Sequence
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0002_manifest"
down_revision: str | Sequence[str] | None = "0001_foundation_identity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("organizations", "organization_type", new_column_name="kind")
    op.execute("UPDATE organizations SET kind='LABORATORY' WHERE kind='LAB'")
    op.create_check_constraint(
        "valid_organization_kind",
        "organizations",
        "kind IN ('HEALTHCARE_FACILITY', 'LABORATORY', 'LOGISTICS')",
    )
    op.create_unique_constraint("uq_organizations_id_region", "organizations", ["id", "region_id"])

    op.create_table(
        "issue_reason_codes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("code", sa.String(48), nullable=False, unique=True),
        sa.Column("scope", sa.String(16), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("label", sa.String(160), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.CheckConstraint("scope IN ('SAMPLE', 'CONTAINER')", name="subject_type"),
        sa.CheckConstraint("kind IN ('MISSING', 'REJECTED')", name="outcome"),
    )
    reasons = [
        ("SAMPLE_NOT_FOUND", "SAMPLE", "MISSING", "Пробирка не обнаружена"),
        ("SAMPLE_LABEL_MISMATCH", "SAMPLE", "REJECTED", "Маркировка пробирки не соответствует"),
        (
            "SAMPLE_CONTAINER_COMPROMISED",
            "SAMPLE",
            "REJECTED",
            "Нарушена целостность контейнера с пробиркой",
        ),
        ("CONTAINER_NOT_PRESENT", "CONTAINER", "MISSING", "Контейнер не предъявлен"),
        ("CONTAINER_SEAL_BROKEN", "CONTAINER", "REJECTED", "Пломба контейнера нарушена"),
        (
            "CONTAINER_LABEL_MISMATCH",
            "CONTAINER",
            "REJECTED",
            "Маркировка контейнера не соответствует",
        ),
    ]
    op.bulk_insert(
        sa.table(
            "issue_reason_codes",
            sa.column("id", postgresql.UUID(as_uuid=True)),
            sa.column("code", sa.String),
            sa.column("scope", sa.String),
            sa.column("kind", sa.String),
            sa.column("label", sa.String),
            sa.column("is_active", sa.Boolean),
        ),
        [
            dict(
                zip(
                    ("id", "code", "scope", "kind", "label", "is_active"),
                    (uuid4(), *row, True),
                    strict=True,
                )
            )
            for row in reasons
        ],
    )
    op.execute("""
    CREATE FUNCTION reject_issue_reason_mutation() RETURNS trigger AS $$
    BEGIN RAISE EXCEPTION 'Issue reason catalog is read-only'; END;
    $$ LANGUAGE plpgsql
    """)
    op.execute("""
    CREATE TRIGGER trg_issue_reason_read_only
    BEFORE INSERT OR UPDATE OR DELETE ON issue_reason_codes
    FOR EACH ROW EXECUTE FUNCTION reject_issue_reason_mutation()
    """)
    op.execute(
        "INSERT INTO regions (id, code, name, is_active) "
        "VALUES (gen_random_uuid(), 'PILOT', 'Пилотный регион', true) ON CONFLICT (code) DO NOTHING"
    )
    op.execute(
        "INSERT INTO organizations (id, region_id, code, name, kind, is_active) "
        "SELECT gen_random_uuid(), id, 'CENTRAL_LAB', 'Центральная лаборатория', "
        "'LABORATORY', true "
        "FROM regions WHERE code='PILOT' ON CONFLICT (code) DO NOTHING"
    )

    op.create_table(
        "samples",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("code", sa.String(40), nullable=False, unique=True),
        sa.Column("origin_organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("material_type", sa.String(16), nullable=False),
        sa.Column("collected_at", sa.DateTime(timezone=True)),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["origin_organization_id"], ["organizations.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.CheckConstraint("material_type IN ('BLOOD', 'URINE', 'OTHER')", name="material_type"),
    )
    op.create_index(
        "ix_samples_origin_created", "samples", ["origin_organization_id", "created_at"]
    )
    op.create_table(
        "sample_external_references",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("sample_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("system_code", sa.String(48), nullable=False),
        sa.Column("reference_kind", sa.String(32), nullable=False),
        sa.Column("external_id", sa.String(128), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["sample_id"], ["samples.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "sample_id",
            "system_code",
            "reference_kind",
            "external_id",
            name="uq_sample_external_reference",
        ),
    )
    op.create_table(
        "shipments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("code", sa.String(40), nullable=False, unique=True),
        sa.Column("region_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sender_organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("destination_organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("courier_user_id", postgresql.UUID(as_uuid=True)),
        sa.Column("status", sa.String(24), server_default="DRAFT", nullable=False),
        sa.Column("version", sa.Integer, server_default="1", nullable=False),
        sa.Column("manifest_revision", sa.Integer, server_default="1", nullable=False),
        sa.Column("expected_delivery_at", sa.DateTime(timezone=True)),
        sa.Column("sealed_at", sa.DateTime(timezone=True)),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["region_id"], ["regions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["sender_organization_id", "region_id"],
            ["organizations.id", "organizations.region_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["destination_organization_id", "region_id"],
            ["organizations.id", "organizations.region_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["courier_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.CheckConstraint("status IN ('DRAFT', 'READY_FOR_PICKUP')", name="manifest_status"),
    )
    op.create_table(
        "transport_containers",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("shipment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("code", sa.String(40), nullable=False, unique=True),
        sa.Column("delivery_status", sa.String(24), server_default="PREPARED", nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True)),
        sa.Column("received_by", postgresql.UUID(as_uuid=True)),
        sa.Column("state_version", sa.Integer, server_default="1", nullable=False),
        sa.Column("receipt_evidence", postgresql.JSONB),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["shipment_id"], ["shipments.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["received_by"], ["users.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("shipment_id", "id", name="uq_container_shipment_id"),
    )
    op.create_index("ix_containers_shipment", "transport_containers", ["shipment_id"])
    op.create_table(
        "container_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("container_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sample_id", postgresql.UUID(as_uuid=True), nullable=False, unique=True),
        sa.Column("receipt_result", sa.String(16)),
        sa.Column("receipt_recorded_at", sa.DateTime(timezone=True)),
        sa.Column("receipt_reason_code", sa.String(48)),
        sa.Column("receipt_comment", sa.String(1000)),
        sa.Column(
            "added_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["container_id"], ["transport_containers.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["sample_id"], ["samples.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["receipt_reason_code"], ["issue_reason_codes.code"], ondelete="RESTRICT"
        ),
    )
    op.create_index("ix_items_container", "container_items", ["container_id"])
    op.create_table(
        "audit_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("actor_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("operation", sa.String(160), nullable=False),
        sa.Column("request_key", sa.String(128), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("resource_id", sa.String(64)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="RESTRICT"),
    )
    op.create_index("ix_audit_events_resource_id", "audit_events", ["resource_id"])
    op.execute("""
    CREATE FUNCTION reject_audit_event_mutation() RETURNS trigger AS $$
    BEGIN RAISE EXCEPTION 'Audit events are append-only'; END;
    $$ LANGUAGE plpgsql
    """)
    op.execute("""
    CREATE TRIGGER trg_audit_events_append_only
    BEFORE UPDATE OR DELETE ON audit_events
    FOR EACH ROW EXECUTE FUNCTION reject_audit_event_mutation()
    """)
    op.create_table(
        "idempotency_records",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("actor_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("operation", sa.String(160), nullable=False),
        sa.Column("key", sa.String(128), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("response_status", sa.Integer),
        sa.Column("response_body", postgresql.JSONB),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "actor_user_id", "operation", "key", name="uq_idempotency_actor_operation_key"
        ),
        sa.CheckConstraint("length(key) > 0", name="nonempty_key"),
    )
    op.execute("""
    CREATE FUNCTION guard_manifest_membership() RETURNS trigger AS $$
    DECLARE shipment_key uuid; shipment_state text; shipment_sender uuid;
    BEGIN
      IF TG_TABLE_NAME = 'transport_containers' THEN
        IF TG_OP = 'UPDATE' AND NEW.shipment_id <> OLD.shipment_id THEN
          RAISE EXCEPTION 'Container cannot be moved to another shipment';
        END IF;
        shipment_key := COALESCE(NEW.shipment_id, OLD.shipment_id);
      ELSE
        SELECT shipment_id INTO shipment_key FROM transport_containers
          WHERE id = COALESCE(NEW.container_id, OLD.container_id);
        IF TG_OP = 'UPDATE' AND NEW.container_id <> OLD.container_id THEN
          RAISE EXCEPTION 'Container item reassignment is not allowed';
        END IF;
      END IF;
      SELECT status, sender_organization_id INTO shipment_state, shipment_sender
        FROM shipments WHERE id = shipment_key FOR UPDATE;
      IF shipment_state IS DISTINCT FROM 'DRAFT' THEN
        IF TG_TABLE_NAME = 'transport_containers' THEN
          IF TG_OP = 'UPDATE' AND NEW.shipment_id = OLD.shipment_id AND NEW.code = OLD.code THEN
            RETURN NEW;
          END IF;
        ELSE
          IF TG_OP = 'UPDATE' AND NEW.container_id = OLD.container_id AND
            NEW.sample_id = OLD.sample_id THEN
            RETURN NEW;
          END IF;
        END IF;
        RAISE EXCEPTION 'Manifest is immutable after shipment seal';
      END IF;
      IF TG_TABLE_NAME = 'container_items' AND TG_OP <> 'DELETE' THEN
        IF NOT EXISTS (
          SELECT 1 FROM samples WHERE id = NEW.sample_id
            AND origin_organization_id = shipment_sender
        ) THEN
          RAISE EXCEPTION 'Sample must belong to the shipment sender';
        END IF;
      END IF;
      IF TG_OP = 'DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
    END; $$ LANGUAGE plpgsql
    """)
    op.execute("""
    CREATE TRIGGER trg_guard_containers BEFORE INSERT OR UPDATE OR DELETE ON transport_containers
    FOR EACH ROW EXECUTE FUNCTION guard_manifest_membership()
    """)
    op.execute("""
    CREATE TRIGGER trg_guard_items BEFORE INSERT OR UPDATE OR DELETE ON container_items
    FOR EACH ROW EXECUTE FUNCTION guard_manifest_membership()
    """)
    op.execute("""
    CREATE FUNCTION guard_sealed_shipment() RETURNS trigger AS $$
    BEGIN
      IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'DRAFT' OR NEW.sealed_at IS NOT NULL THEN
          RAISE EXCEPTION 'Shipment must be created in DRAFT';
        END IF;
        RETURN NEW;
      END IF;
      IF TG_OP = 'DELETE' THEN
        IF OLD.status <> 'DRAFT' THEN RAISE EXCEPTION 'Sealed shipment cannot be deleted'; END IF;
        RETURN OLD;
      END IF;
      IF OLD.status <> 'DRAFT' AND (
        NEW.sender_organization_id IS DISTINCT FROM OLD.sender_organization_id OR
        NEW.destination_organization_id IS DISTINCT FROM OLD.destination_organization_id OR
        NEW.courier_user_id IS DISTINCT FROM OLD.courier_user_id OR
        NEW.expected_delivery_at IS DISTINCT FROM OLD.expected_delivery_at OR
        NEW.manifest_revision IS DISTINCT FROM OLD.manifest_revision OR
        NEW.code IS DISTINCT FROM OLD.code OR NEW.region_id IS DISTINCT FROM OLD.region_id OR
        NEW.status IS DISTINCT FROM OLD.status OR NEW.sealed_at IS DISTINCT FROM OLD.sealed_at
      ) THEN RAISE EXCEPTION 'Sealed shipment identity is immutable'; END IF;
      IF NEW.status = 'READY_FOR_PICKUP' AND OLD.status = 'DRAFT' THEN
        IF NEW.expected_delivery_at IS NULL OR NEW.expected_delivery_at <= now() OR
          NOT EXISTS (SELECT 1 FROM organizations WHERE id=NEW.destination_organization_id
            AND code='CENTRAL_LAB' AND kind='LABORATORY' AND is_active) OR
          NOT EXISTS (SELECT 1 FROM transport_containers WHERE shipment_id=NEW.id) OR
          EXISTS (SELECT 1 FROM transport_containers c WHERE c.shipment_id=NEW.id AND
            NOT EXISTS (SELECT 1 FROM container_items i WHERE i.container_id=c.id)) THEN
          RAISE EXCEPTION 'Shipment does not meet seal requirements';
        END IF;
      END IF;
      RETURN NEW;
    END; $$ LANGUAGE plpgsql
    """)
    op.execute("""
    CREATE TRIGGER trg_guard_shipment_identity BEFORE INSERT OR UPDATE OR DELETE ON shipments
    FOR EACH ROW EXECUTE FUNCTION guard_sealed_shipment()
    """)
    op.execute("""
    CREATE FUNCTION guard_sample_identity() RETURNS trigger AS $$
    BEGIN
      IF NEW.code IS DISTINCT FROM OLD.code OR
        NEW.origin_organization_id IS DISTINCT FROM OLD.origin_organization_id THEN
        RAISE EXCEPTION 'Sample identity is immutable';
      END IF;
      RETURN NEW;
    END; $$ LANGUAGE plpgsql
    """)
    op.execute("""
    CREATE TRIGGER trg_guard_sample_identity BEFORE UPDATE ON samples
    FOR EACH ROW EXECUTE FUNCTION guard_sample_identity()
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_audit_events_append_only ON audit_events")
    op.execute("DROP FUNCTION IF EXISTS reject_audit_event_mutation()")
    op.execute("DROP TRIGGER IF EXISTS trg_issue_reason_read_only ON issue_reason_codes")
    op.execute("DROP FUNCTION IF EXISTS reject_issue_reason_mutation()")
    op.execute("DROP TRIGGER IF EXISTS trg_guard_sample_identity ON samples")
    op.execute("DROP FUNCTION IF EXISTS guard_sample_identity()")
    op.execute("DROP TRIGGER IF EXISTS trg_guard_shipment_identity ON shipments")
    op.execute("DROP FUNCTION IF EXISTS guard_sealed_shipment()")
    op.execute("DROP TRIGGER IF EXISTS trg_guard_items ON container_items")
    op.execute("DROP TRIGGER IF EXISTS trg_guard_containers ON transport_containers")
    op.execute("DROP FUNCTION IF EXISTS guard_manifest_membership()")
    op.execute("DROP TABLE IF EXISTS audit_events")
    for table in (
        "idempotency_records",
        "container_items",
        "transport_containers",
        "shipments",
        "sample_external_references",
        "samples",
        "issue_reason_codes",
    ):
        op.drop_table(table)
    op.drop_constraint("uq_organizations_id_region", "organizations", type_="unique")
    op.execute(
        "ALTER TABLE organizations DROP CONSTRAINT IF EXISTS "
        "ck_organizations_valid_organization_kind"
    )
    op.alter_column("organizations", "kind", new_column_name="organization_type")
    # Keep pilot reference data: the seed may have pre-existed this revision.
