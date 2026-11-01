"""Persist Courier offline snapshots and durable command identities."""

# PostgreSQL guards are embedded below as multiline SQL strings.
# ruff: noqa: E501

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0009_stage_g_offline"
down_revision: str | Sequence[str] | None = "0008_stage_f_integrity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "shipments",
        sa.Column("pickup_state_version", sa.BigInteger(), server_default="1", nullable=False),
    )
    op.create_check_constraint(
        op.f("ck_shipments_positive_pickup_state_version"),
        "shipments",
        "pickup_state_version > 0",
    )
    op.execute("""
    CREATE FUNCTION guard_pickup_state_version() RETURNS trigger AS $$
    BEGIN
      IF TG_OP = 'INSERT' THEN
        IF NEW.pickup_state_version <> 1 THEN
          RAISE EXCEPTION 'Pickup state version must start at 1';
        END IF;
        RETURN NEW;
      END IF;
      IF NEW.pickup_state_version IS DISTINCT FROM OLD.pickup_state_version THEN
        IF pg_trigger_depth() <> 2 THEN
          RAISE EXCEPTION 'Pickup state version is database-managed';
        END IF;
        IF NEW.pickup_state_version <> OLD.pickup_state_version + 1 THEN
          RAISE EXCEPTION 'Pickup state version must increment by exactly one';
        END IF;
      END IF;
      RETURN NEW;
    END; $$ LANGUAGE plpgsql
    """)
    op.execute("""
    CREATE TRIGGER trg_guard_pickup_state_version
    BEFORE INSERT OR UPDATE ON shipments
    FOR EACH ROW EXECUTE FUNCTION guard_pickup_state_version()
    """)
    op.alter_column(
        "transport_containers",
        "state_version",
        existing_type=sa.Integer(),
        type_=sa.BigInteger(),
        existing_nullable=False,
        existing_server_default=sa.text("1"),
    )

    op.add_column(
        "idempotency_records",
        sa.Column("offline_retention_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "idempotency_records",
        sa.Column(
            "offline_causal_metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
    )
    op.create_check_constraint(
        op.f("ck_idempotency_records_offline_metadata_namespace"),
        "idempotency_records",
        "(offline_retention_expires_at IS NULL AND offline_causal_metadata IS NULL) "
        "OR operation = 'courier_offline_command'",
    )

    op.execute("""
    CREATE FUNCTION is_phi_free_offline_container_payload(payload jsonb) RETURNS boolean AS $$
    SELECT CASE
      WHEN jsonb_typeof(payload) <> 'array' THEN false
      ELSE jsonb_array_length(payload) > 0 AND NOT EXISTS (
        SELECT 1 FROM jsonb_array_elements(payload) AS container(value)
        WHERE jsonb_typeof(value) <> 'object'
          OR NOT (value ?& ARRAY['id', 'code', 'delivery_status', 'state_version'])
          OR (value - ARRAY['id', 'code', 'delivery_status', 'state_version']) <> '{}'::jsonb
          OR jsonb_typeof(value->'id') <> 'string'
          OR jsonb_typeof(value->'code') <> 'string'
          OR jsonb_typeof(value->'delivery_status') <> 'string'
          OR COALESCE(value->>'delivery_status', '') NOT IN
            ('PREPARED', 'IN_TRANSIT', 'DELIVERY_PENDING', 'RECEIVED', 'BLOCKED', 'UNDELIVERED_CLOSED')
          OR CASE
            WHEN jsonb_typeof(value->'state_version') = 'number' THEN
              (value->>'state_version')::numeric < 1 OR
              (value->>'state_version')::numeric <> trunc((value->>'state_version')::numeric)
            ELSE true
          END
      )
    END
    $$ LANGUAGE sql IMMUTABLE STRICT
    """)

    op.create_table(
        "courier_manifest_snapshots",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("assigned_courier_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("shipment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("region_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("manifest_revision", sa.Integer(), nullable=False),
        sa.Column("pickup_state_version", sa.BigInteger(), nullable=False),
        sa.Column(
            "issued_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.clock_timestamp(),
            nullable=False,
        ),
        sa.Column("policy_version", sa.String(length=32), nullable=False),
        sa.Column("container_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.CheckConstraint(
            "manifest_revision > 0",
            name=op.f("ck_courier_manifest_snapshots_positive_manifest_revision"),
        ),
        sa.CheckConstraint(
            "pickup_state_version > 0",
            name=op.f("ck_courier_manifest_snapshots_positive_pickup_state_version"),
        ),
        sa.CheckConstraint(
            "is_phi_free_offline_container_payload(container_payload)",
            name=op.f("ck_courier_manifest_snapshots_phi_free_container_payload"),
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["users.id"],
            name="fk_courier_manifest_snapshots_actor_user_id_users",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["assigned_courier_user_id"],
            ["users.id"],
            name="fk_courier_manifest_snapshots_assigned_courier_user_id_users",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["shipment_id"],
            ["shipments.id"],
            name="fk_courier_manifest_snapshots_shipment_id_shipments",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["region_id"],
            ["regions.id"],
            name="fk_courier_manifest_snapshots_region_id_regions",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_courier_manifest_snapshots"),
    )
    op.create_index(
        "ix_courier_manifest_snapshots_shipment_issued",
        "courier_manifest_snapshots",
        ["shipment_id", "issued_at"],
    )
    op.execute("""
    CREATE FUNCTION guard_courier_manifest_snapshot() RETURNS trigger AS $$
    BEGIN
      IF TG_OP = 'INSERT' THEN
        NEW.issued_at := clock_timestamp();
        RETURN NEW;
      END IF;
      RAISE EXCEPTION 'Courier manifest snapshots are immutable';
    END; $$ LANGUAGE plpgsql
    """)
    op.execute("""
    CREATE TRIGGER trg_guard_courier_manifest_snapshot
    BEFORE INSERT OR UPDATE OR DELETE ON courier_manifest_snapshots
    FOR EACH ROW EXECUTE FUNCTION guard_courier_manifest_snapshot()
    """)

    op.create_table(
        "offline_command_tombstones",
        sa.Column("actor_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("client_command_id", sa.String(length=128), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("original_outcome", sa.String(length=32), nullable=False),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.clock_timestamp(),
            nullable=False,
        ),
        sa.Column("audit_event_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("replay_response", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.CheckConstraint(
            "length(client_command_id) > 0",
            name=op.f("ck_offline_command_tombstones_nonempty_client_command_id"),
        ),
        sa.CheckConstraint(
            "length(request_hash) = 64",
            name=op.f("ck_offline_command_tombstones_request_hash_length"),
        ),
        sa.CheckConstraint(
            "original_outcome IN ('APPLIED', 'REJECTED', 'REVIEW_REQUIRED')",
            name=op.f("ck_offline_command_tombstones_persisted_outcome"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(replay_response) = 'object'",
            name=op.f("ck_offline_command_tombstones_replay_response_object"),
        ),
        sa.ForeignKeyConstraint(
            ["audit_event_id"],
            ["audit_events.id"],
            name="fk_offline_command_tombstones_audit_event_id_audit_events",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "actor_user_id", "client_command_id", name="pk_offline_command_tombstones"
        ),
    )
    op.execute("""
    CREATE FUNCTION guard_offline_command_tombstone() RETURNS trigger AS $$
    BEGIN
      IF TG_OP = 'INSERT' THEN
        NEW.recorded_at := clock_timestamp();
        RETURN NEW;
      END IF;
      RAISE EXCEPTION 'Offline command tombstones are immutable';
    END; $$ LANGUAGE plpgsql
    """)
    op.execute("""
    CREATE TRIGGER trg_guard_offline_command_tombstone
    BEFORE INSERT OR UPDATE OR DELETE ON offline_command_tombstones
    FOR EACH ROW EXECUTE FUNCTION guard_offline_command_tombstone()
    """)

    op.execute("""
    CREATE FUNCTION increment_pickup_state_version() RETURNS trigger AS $$
    BEGIN
      IF TG_OP = 'INSERT' THEN
        IF NEW.kind = 'PICKUP' THEN
          UPDATE shipments
            SET pickup_state_version = pickup_state_version + 1
            WHERE id = NEW.shipment_id;
        END IF;
        RETURN NEW;
      END IF;
      IF (OLD.kind = 'PICKUP' OR NEW.kind = 'PICKUP') AND (
        NEW.kind IS DISTINCT FROM OLD.kind OR
        NEW.shipment_id IS DISTINCT FROM OLD.shipment_id OR
        NEW.attempt_no IS DISTINCT FROM OLD.attempt_no OR
        NEW.status IS DISTINCT FROM OLD.status
      ) THEN
        UPDATE shipments
          SET pickup_state_version = pickup_state_version + 1
          WHERE id = COALESCE(NEW.shipment_id, OLD.shipment_id);
      END IF;
      RETURN NEW;
    END; $$ LANGUAGE plpgsql
    """)
    op.execute("""
    CREATE TRIGGER trg_increment_pickup_state_version
    AFTER INSERT OR UPDATE ON handoffs
    FOR EACH ROW EXECUTE FUNCTION increment_pickup_state_version()
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_increment_pickup_state_version ON handoffs")
    op.execute("DROP FUNCTION IF EXISTS increment_pickup_state_version()")
    op.execute("DROP TRIGGER IF EXISTS trg_guard_pickup_state_version ON shipments")
    op.execute("DROP FUNCTION IF EXISTS guard_pickup_state_version()")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_guard_offline_command_tombstone ON offline_command_tombstones"
    )
    op.execute("DROP FUNCTION IF EXISTS guard_offline_command_tombstone()")
    op.drop_table("offline_command_tombstones")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_guard_courier_manifest_snapshot ON courier_manifest_snapshots"
    )
    op.execute("DROP FUNCTION IF EXISTS guard_courier_manifest_snapshot()")
    op.drop_index(
        "ix_courier_manifest_snapshots_shipment_issued", table_name="courier_manifest_snapshots"
    )
    op.drop_table("courier_manifest_snapshots")
    op.execute("DROP FUNCTION IF EXISTS is_phi_free_offline_container_payload(jsonb)")
    op.drop_constraint(
        op.f("ck_idempotency_records_offline_metadata_namespace"),
        "idempotency_records",
        type_="check",
    )
    op.drop_column("idempotency_records", "offline_causal_metadata")
    op.drop_column("idempotency_records", "offline_retention_expires_at")
    op.alter_column(
        "transport_containers",
        "state_version",
        existing_type=sa.BigInteger(),
        type_=sa.Integer(),
        existing_nullable=False,
        existing_server_default=sa.text("1"),
    )
    op.drop_constraint(
        op.f("ck_shipments_positive_pickup_state_version"), "shipments", type_="check"
    )
    op.drop_column("shipments", "pickup_state_version")
