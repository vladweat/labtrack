"""Add bilateral Pickup custody and shipment closure.

Revision ID: 0003_pickup
Revises: 0002_manifest
"""

# PostgreSQL trigger bodies are embedded as multiline SQL strings.
# ruff: noqa: E501

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0003_pickup"
down_revision: str | Sequence[str] | None = "0002_manifest"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("audit_events", sa.Column("details", postgresql.JSONB()))
    op.add_column("shipments", sa.Column("closed_at", sa.DateTime(timezone=True)))
    op.add_column(
        "shipments",
        sa.Column(
            "closed_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
        ),
    )
    op.add_column("shipments", sa.Column("closure_reason", sa.String(500)))
    op.drop_constraint(op.f("ck_shipments_manifest_status"), "shipments", type_="check")
    op.create_check_constraint(
        "manifest_status",
        "shipments",
        "status IN ('DRAFT', 'READY_FOR_PICKUP', 'PICKUP_PENDING', 'IN_TRANSIT', 'CANCELLED')",
    )
    op.create_check_constraint(
        "closure_fields",
        "shipments",
        "(status = 'CANCELLED' AND closed_at IS NOT NULL AND closed_by IS NOT NULL "
        "AND length(trim(closure_reason)) > 0) OR "
        "(status <> 'CANCELLED' AND closed_at IS NULL AND closed_by IS NULL AND closure_reason IS NULL)",
    )
    op.create_table(
        "handoffs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "shipment_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("shipments.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column(
            "container_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("transport_containers.id", ondelete="RESTRICT"),
        ),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), server_default="PENDING", nullable=False),
        sa.Column(
            "initiated_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "initiated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "confirmed_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
        ),
        sa.Column("confirmed_at", sa.DateTime(timezone=True)),
        sa.Column(
            "withdrawn_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
        ),
        sa.Column("withdrawn_at", sa.DateTime(timezone=True)),
        sa.Column("withdrawal_reason", sa.String(500)),
        sa.Column("superseded_by_incident_id", postgresql.UUID(as_uuid=True)),
        sa.Column("superseded_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("kind IN ('PICKUP', 'DELIVERY')", name="handoff_kind"),
        sa.CheckConstraint(
            "status IN ('PENDING', 'CONFIRMED', 'WITHDRAWN', 'SUPERSEDED')", name="handoff_status"
        ),
        sa.CheckConstraint("attempt_no > 0", name="positive_attempt"),
        sa.CheckConstraint(
            "(kind = 'PICKUP' AND container_id IS NULL) OR (kind = 'DELIVERY' AND container_id IS NOT NULL)",
            name="handoff_container_scope",
        ),
        sa.CheckConstraint(
            "(status = 'PENDING' AND confirmed_by IS NULL AND confirmed_at IS NULL AND withdrawn_by IS NULL AND withdrawn_at IS NULL AND withdrawal_reason IS NULL AND superseded_by_incident_id IS NULL AND superseded_at IS NULL) OR (status = 'CONFIRMED' AND confirmed_by IS NOT NULL AND confirmed_at IS NOT NULL AND withdrawn_by IS NULL AND withdrawn_at IS NULL AND withdrawal_reason IS NULL AND superseded_by_incident_id IS NULL AND superseded_at IS NULL) OR (status = 'WITHDRAWN' AND confirmed_by IS NULL AND confirmed_at IS NULL AND withdrawn_by IS NOT NULL AND withdrawn_at IS NOT NULL AND length(trim(withdrawal_reason)) > 0 AND superseded_by_incident_id IS NULL AND superseded_at IS NULL) OR (status = 'SUPERSEDED' AND superseded_by_incident_id IS NOT NULL AND superseded_at IS NOT NULL)",
            name="handoff_terminal_fields",
        ),
    )
    op.create_index(
        "ix_handoffs_shipment_kind_status", "handoffs", ["shipment_id", "kind", "status"]
    )
    op.create_index(
        "uq_handoff_pickup_attempt",
        "handoffs",
        ["shipment_id", "attempt_no"],
        unique=True,
        postgresql_where=sa.text("kind = 'PICKUP'"),
    )
    op.create_index(
        "uq_handoff_delivery_attempt",
        "handoffs",
        ["shipment_id", "container_id", "attempt_no"],
        unique=True,
        postgresql_where=sa.text("kind = 'DELIVERY'"),
    )

    op.execute("""
    CREATE FUNCTION guard_pickup_handoff() RETURNS trigger AS $$
    DECLARE s shipments%ROWTYPE;
    BEGIN
      IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'Handoff records cannot be deleted'; END IF;
      SELECT * INTO s FROM shipments WHERE id = COALESCE(NEW.shipment_id, OLD.shipment_id) FOR UPDATE;
      IF TG_OP = 'INSERT' THEN
        IF NEW.kind <> 'PICKUP' OR NEW.status <> 'PENDING' OR s.status <> 'READY_FOR_PICKUP' OR s.sealed_at IS NULL THEN
          RAISE EXCEPTION 'Pickup must start pending on a sealed ready shipment';
        END IF;
        IF NOT EXISTS (SELECT 1 FROM memberships m JOIN users u ON u.id=m.user_id
          JOIN organizations o ON o.id=m.organization_id JOIN regions r ON r.id=o.region_id
          WHERE m.user_id=NEW.initiated_by AND m.organization_id=s.sender_organization_id
          AND m.role='SENDER_OPERATOR' AND m.is_active AND u.is_active AND o.is_active AND r.is_active) THEN
          RAISE EXCEPTION 'Pickup initiation requires an active Sender';
        END IF;
        IF NEW.attempt_no <> COALESCE((SELECT max(attempt_no)+1 FROM handoffs WHERE shipment_id=s.id AND kind='PICKUP'),1) THEN
          RAISE EXCEPTION 'Pickup attempt number is not sequential';
        END IF;
        IF EXISTS (SELECT 1 FROM handoffs WHERE shipment_id=s.id AND kind='PICKUP' AND status='PENDING') THEN
          RAISE EXCEPTION 'Shipment already has a pending Pickup';
        END IF;
        RETURN NEW;
      END IF;
      IF NEW.id IS DISTINCT FROM OLD.id OR NEW.shipment_id IS DISTINCT FROM OLD.shipment_id OR
         NEW.kind IS DISTINCT FROM OLD.kind OR NEW.container_id IS DISTINCT FROM OLD.container_id OR
         NEW.attempt_no IS DISTINCT FROM OLD.attempt_no OR NEW.initiated_by IS DISTINCT FROM OLD.initiated_by OR
         NEW.initiated_at IS DISTINCT FROM OLD.initiated_at OR OLD.status <> 'PENDING' THEN
        RAISE EXCEPTION 'Terminal handoff is immutable';
      END IF;
      IF OLD.kind <> 'PICKUP' OR NEW.status NOT IN ('CONFIRMED', 'WITHDRAWN') THEN
        RAISE EXCEPTION 'Invalid Pickup handoff transition';
      END IF;
      IF NEW.status='CONFIRMED' THEN
        IF s.status <> 'PICKUP_PENDING' OR NEW.withdrawn_by IS NOT NULL OR
          NEW.confirmed_by IS NULL OR NEW.confirmed_at IS NULL OR NEW.confirmed_by=OLD.initiated_by OR
          NEW.withdrawal_reason IS NOT NULL OR
          NOT EXISTS (SELECT 1 FROM memberships m JOIN users u ON u.id=m.user_id
            JOIN regions r ON r.id=m.region_id WHERE m.user_id=NEW.confirmed_by
            AND m.organization_id IS NULL AND m.region_id=s.region_id AND m.role='COURIER'
            AND m.is_active AND u.is_active AND r.is_active) OR NEW.confirmed_by IS DISTINCT FROM s.courier_user_id THEN
          RAISE EXCEPTION 'Pickup confirmation requires a different assigned Courier';
        END IF;
      ELSE
        IF s.status <> 'PICKUP_PENDING' OR NEW.withdrawn_by IS DISTINCT FROM OLD.initiated_by OR
           NEW.withdrawn_at IS NULL OR length(trim(COALESCE(NEW.withdrawal_reason,'')))=0 OR
           NEW.confirmed_by IS NOT NULL OR NEW.confirmed_at IS NOT NULL THEN
          RAISE EXCEPTION 'Pickup withdrawal requires its initiating Sender and a reason';
        END IF;
      END IF;
      RETURN NEW;
    END; $$ LANGUAGE plpgsql
    """)
    op.execute(
        "CREATE TRIGGER trg_guard_pickup_handoff BEFORE INSERT OR UPDATE OR DELETE ON handoffs FOR EACH ROW EXECUTE FUNCTION guard_pickup_handoff()"
    )

    op.execute("""
    CREATE OR REPLACE FUNCTION guard_sealed_shipment() RETURNS trigger AS $$
    BEGIN
      IF TG_OP='INSERT' THEN
        IF NEW.status <> 'DRAFT' OR NEW.sealed_at IS NOT NULL THEN RAISE EXCEPTION 'Shipment must be created in DRAFT'; END IF;
        RETURN NEW;
      END IF;
      IF TG_OP='DELETE' THEN
        IF OLD.status <> 'DRAFT' THEN RAISE EXCEPTION 'Sealed shipment cannot be deleted'; END IF;
        RETURN OLD;
      END IF;
      IF OLD.status <> 'DRAFT' AND (
        NEW.sender_organization_id IS DISTINCT FROM OLD.sender_organization_id OR
        NEW.destination_organization_id IS DISTINCT FROM OLD.destination_organization_id OR
        NEW.courier_user_id IS DISTINCT FROM OLD.courier_user_id OR
        NEW.expected_delivery_at IS DISTINCT FROM OLD.expected_delivery_at OR
        NEW.manifest_revision IS DISTINCT FROM OLD.manifest_revision OR NEW.code IS DISTINCT FROM OLD.code OR
        NEW.region_id IS DISTINCT FROM OLD.region_id OR NEW.sealed_at IS DISTINCT FROM OLD.sealed_at
      ) THEN RAISE EXCEPTION 'Sealed shipment identity is immutable'; END IF;
      IF NEW.status IS DISTINCT FROM OLD.status THEN
        IF OLD.status='DRAFT' AND NEW.status='READY_FOR_PICKUP' THEN
          IF NEW.expected_delivery_at IS NULL OR NEW.expected_delivery_at <= now() OR
            NOT EXISTS (SELECT 1 FROM organizations WHERE id=NEW.destination_organization_id AND code='CENTRAL_LAB' AND kind='LABORATORY' AND is_active) OR
            NOT EXISTS (SELECT 1 FROM transport_containers WHERE shipment_id=NEW.id) OR
            EXISTS (SELECT 1 FROM transport_containers c WHERE c.shipment_id=NEW.id AND NOT EXISTS (SELECT 1 FROM container_items i WHERE i.container_id=c.id)) THEN
            RAISE EXCEPTION 'Shipment does not meet seal requirements';
          END IF;
        ELSIF OLD.status='READY_FOR_PICKUP' AND NEW.status='PICKUP_PENDING' THEN
          IF NOT EXISTS (SELECT 1 FROM handoffs WHERE shipment_id=NEW.id AND kind='PICKUP' AND status='PENDING') THEN
            RAISE EXCEPTION 'shipment status transition requires a Pickup handoff';
          END IF;
        ELSIF OLD.status='PICKUP_PENDING' AND NEW.status='READY_FOR_PICKUP' THEN
          IF NOT EXISTS (SELECT 1 FROM handoffs WHERE shipment_id=NEW.id AND kind='PICKUP' AND status='WITHDRAWN') THEN
            RAISE EXCEPTION 'Pickup withdrawal is required before returning to ready';
          END IF;
        ELSIF OLD.status='PICKUP_PENDING' AND NEW.status='IN_TRANSIT' THEN
          IF NOT EXISTS (SELECT 1 FROM handoffs WHERE shipment_id=NEW.id AND kind='PICKUP' AND status='CONFIRMED') OR
            EXISTS (SELECT 1 FROM transport_containers WHERE shipment_id=NEW.id AND delivery_status <> 'IN_TRANSIT') THEN
            RAISE EXCEPTION 'confirmed Pickup and transit containers are required';
          END IF;
        ELSIF OLD.status IN ('DRAFT','READY_FOR_PICKUP') AND NEW.status='CANCELLED' THEN
          IF NEW.closed_at IS NULL OR NEW.closed_by IS NULL OR length(trim(COALESCE(NEW.closure_reason,'')))=0 OR
             EXISTS (SELECT 1 FROM handoffs WHERE shipment_id=NEW.id AND kind='PICKUP' AND status IN ('PENDING','CONFIRMED')) THEN
            RAISE EXCEPTION 'Shipment cancellation requires no active or confirmed Pickup';
          END IF;
          IF NOT EXISTS (SELECT 1 FROM memberships m JOIN users u ON u.id=m.user_id
            JOIN organizations o ON o.id=m.organization_id JOIN regions r ON r.id=o.region_id
            WHERE m.user_id=NEW.closed_by AND m.organization_id=NEW.sender_organization_id
            AND m.role='SENDER_OPERATOR' AND m.is_active AND u.is_active AND o.is_active AND r.is_active) THEN
            RAISE EXCEPTION 'Shipment cancellation requires an active Sender';
          END IF;
        ELSE
          RAISE EXCEPTION 'Invalid shipment status transition';
        END IF;
      END IF;
      IF OLD.status='CANCELLED' AND (
        NEW.closed_at IS DISTINCT FROM OLD.closed_at OR NEW.closed_by IS DISTINCT FROM OLD.closed_by OR
        NEW.closure_reason IS DISTINCT FROM OLD.closure_reason
      ) THEN RAISE EXCEPTION 'Cancelled shipment closure is immutable'; END IF;
      RETURN NEW;
    END; $$ LANGUAGE plpgsql
    """)

    op.execute("""
    CREATE FUNCTION guard_container_transit() RETURNS trigger AS $$
    DECLARE shipment_key uuid;
    BEGIN
      IF NEW.shipment_id IS DISTINCT FROM OLD.shipment_id OR NEW.code IS DISTINCT FROM OLD.code THEN
        RAISE EXCEPTION 'Container identity is immutable';
      END IF;
      IF NEW.delivery_status IS DISTINCT FROM OLD.delivery_status THEN
        IF OLD.delivery_status='PREPARED' AND NEW.delivery_status='IN_TRANSIT' THEN
          SELECT id INTO shipment_key FROM shipments WHERE id=NEW.shipment_id FOR UPDATE;
          IF NOT EXISTS (SELECT 1 FROM handoffs WHERE shipment_id=shipment_key AND kind='PICKUP' AND status='CONFIRMED') OR
             NEW.state_version <> OLD.state_version + 1 THEN
            RAISE EXCEPTION 'confirmed Pickup is required before transit';
          END IF;
        ELSE
          RAISE EXCEPTION 'Invalid container custody transition';
        END IF;
      END IF;
      RETURN NEW;
    END; $$ LANGUAGE plpgsql
    """)
    op.execute(
        "CREATE TRIGGER trg_guard_container_transit BEFORE UPDATE ON transport_containers FOR EACH ROW EXECUTE FUNCTION guard_container_transit()"
    )
    op.execute("""
    CREATE FUNCTION validate_pickup_state_consistency() RETURNS trigger AS $$
    DECLARE shipment_key uuid; shipment_state text; pending_count integer;
      confirmed_count integer; transit_count integer; container_count integer;
    BEGIN
      IF TG_TABLE_NAME='shipments' THEN
        shipment_key := COALESCE(NEW.id, OLD.id);
      ELSE
        shipment_key := COALESCE(NEW.shipment_id, OLD.shipment_id);
      END IF;
      SELECT status INTO shipment_state FROM shipments WHERE id=shipment_key;
      IF shipment_state IS NULL THEN RETURN NULL; END IF;
      SELECT count(*) FILTER (WHERE status='PENDING'),
        count(*) FILTER (WHERE status='CONFIRMED')
        INTO pending_count, confirmed_count FROM handoffs
        WHERE shipment_id=shipment_key AND kind='PICKUP';
      SELECT count(*), count(*) FILTER (WHERE delivery_status='IN_TRANSIT')
        INTO container_count, transit_count FROM transport_containers
        WHERE shipment_id=shipment_key;
      IF shipment_state='PICKUP_PENDING' AND pending_count <> 1 THEN
        RAISE EXCEPTION 'Pending Pickup must match PICKUP_PENDING shipment state';
      ELSIF shipment_state='IN_TRANSIT' AND
        (confirmed_count <> 1 OR container_count=0 OR transit_count <> container_count) THEN
        RAISE EXCEPTION 'Confirmed Pickup and all transit containers must match IN_TRANSIT shipment state';
      ELSIF shipment_state IN ('DRAFT','READY_FOR_PICKUP','CANCELLED') AND
        (pending_count > 0 OR confirmed_count > 0) THEN
        RAISE EXCEPTION 'Active Pickup handoff must match shipment state';
      ELSIF transit_count > 0 AND shipment_state <> 'IN_TRANSIT' THEN
        RAISE EXCEPTION 'Transit containers require IN_TRANSIT shipment state';
      END IF;
      RETURN NULL;
    END; $$ LANGUAGE plpgsql
    """)
    op.execute("""
    CREATE CONSTRAINT TRIGGER trg_validate_pickup_handoff_consistency
    AFTER INSERT OR UPDATE ON handoffs DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION validate_pickup_state_consistency()
    """)
    op.execute("""
    CREATE CONSTRAINT TRIGGER trg_validate_pickup_shipment_consistency
    AFTER INSERT OR UPDATE ON shipments DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION validate_pickup_state_consistency()
    """)
    op.execute("""
    CREATE CONSTRAINT TRIGGER trg_validate_pickup_container_consistency
    AFTER INSERT OR UPDATE ON transport_containers DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION validate_pickup_state_consistency()
    """)


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_validate_pickup_container_consistency ON transport_containers"
    )
    op.execute("DROP TRIGGER IF EXISTS trg_validate_pickup_shipment_consistency ON shipments")
    op.execute("DROP TRIGGER IF EXISTS trg_validate_pickup_handoff_consistency ON handoffs")
    op.execute("DROP FUNCTION IF EXISTS validate_pickup_state_consistency()")
    op.execute("DROP TRIGGER IF EXISTS trg_guard_container_transit ON transport_containers")
    op.execute("DROP FUNCTION IF EXISTS guard_container_transit()")
    op.execute("DROP TRIGGER IF EXISTS trg_guard_shipment_identity ON shipments")
    op.execute("DROP FUNCTION IF EXISTS guard_sealed_shipment()")
    op.execute("DROP TRIGGER IF EXISTS trg_guard_pickup_handoff ON handoffs")
    op.execute("DROP FUNCTION IF EXISTS guard_pickup_handoff()")
    op.drop_table("handoffs")
    op.drop_constraint(op.f("ck_shipments_manifest_status"), "shipments", type_="check")
    op.drop_constraint(op.f("ck_shipments_closure_fields"), "shipments", type_="check")
    op.create_check_constraint(
        "manifest_status", "shipments", "status IN ('DRAFT', 'READY_FOR_PICKUP')"
    )
    op.drop_column("shipments", "closure_reason")
    op.drop_column("shipments", "closed_by")
    op.drop_column("shipments", "closed_at")
    op.drop_column("audit_events", "details")
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
      IF NEW.status='READY_FOR_PICKUP' AND OLD.status='DRAFT' THEN
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
