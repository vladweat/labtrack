"""Add per-container Delivery custody and the first receiving session."""

# PostgreSQL trigger bodies are embedded as multiline SQL strings.
# ruff: noqa: E501

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0004_delivery"
down_revision: str | Sequence[str] | None = "0003_pickup"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(op.f("ck_shipments_manifest_status"), "shipments", type_="check")
    op.create_check_constraint(
        "manifest_status",
        "shipments",
        "status IN ('DRAFT', 'READY_FOR_PICKUP', 'PICKUP_PENDING', 'IN_TRANSIT', "
        "'DELIVERY_PENDING', 'DELIVERY_BLOCKED', 'PARTIALLY_DELIVERED', 'RECEIVING', "
        "'COMPLETED', 'COMPLETED_WITH_ISSUES', 'CANCELLED', 'CLOSED_UNDELIVERED')",
    )
    op.create_check_constraint(
        "delivery_status",
        "transport_containers",
        "delivery_status IN ('PREPARED', 'IN_TRANSIT', 'DELIVERY_PENDING', 'RECEIVED', "
        "'BLOCKED', 'UNDELIVERED_CLOSED')",
    )
    op.create_check_constraint(
        "receipt_fields",
        "transport_containers",
        "(delivery_status='RECEIVED' AND received_at IS NOT NULL AND "
        "receipt_evidence IS NOT NULL AND "
        "((receipt_evidence->>'method'='NORMAL' AND received_by IS NOT NULL) OR "
        "(receipt_evidence->>'method'='EXCEPTIONAL' AND received_by IS NULL))) OR "
        "(delivery_status<>'RECEIVED' AND received_at IS NULL AND received_by IS NULL "
        "AND receipt_evidence IS NULL)",
    )

    op.drop_index("uq_handoff_delivery_attempt", table_name="handoffs")
    op.create_index(
        "uq_handoff_delivery_attempt",
        "handoffs",
        ["container_id", "attempt_no"],
        unique=True,
        postgresql_where=sa.text("kind = 'DELIVERY'"),
    )
    op.create_index(
        "uq_handoff_pickup_active_or_confirmed",
        "handoffs",
        ["shipment_id"],
        unique=True,
        postgresql_where=sa.text("kind = 'PICKUP' AND status IN ('PENDING', 'CONFIRMED')"),
    )
    op.create_index(
        "uq_handoff_delivery_active_or_confirmed",
        "handoffs",
        ["container_id"],
        unique=True,
        postgresql_where=sa.text("kind = 'DELIVERY' AND status IN ('PENDING', 'CONFIRMED')"),
    )

    op.create_table(
        "receiving_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "shipment_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("shipments.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), server_default="OPEN", nullable=False),
        sa.Column(
            "opened_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "opened_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("reopen_reason", sa.String(500)),
        sa.Column(
            "closed_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
        ),
        sa.Column("closed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("shipment_id", "sequence", name="uq_receiving_session_sequence"),
        sa.CheckConstraint("sequence > 0", name="positive_sequence"),
        sa.CheckConstraint("status IN ('OPEN', 'CLOSED')", name="receiving_session_status"),
        sa.CheckConstraint(
            "(status='OPEN' AND closed_by IS NULL AND closed_at IS NULL) OR "
            "(status='CLOSED' AND closed_by IS NOT NULL AND closed_at IS NOT NULL)",
            name="receiving_session_close_fields",
        ),
    )
    op.create_index(
        "uq_receiving_session_open",
        "receiving_sessions",
        ["shipment_id"],
        unique=True,
        postgresql_where=sa.text("status = 'OPEN'"),
    )

    # The Stage C guard remains authoritative for Pickup. Delivery handoffs use a
    # dedicated sibling trigger, so the published Pickup implementation is intact.
    op.execute("""
    CREATE FUNCTION guard_delivery_handoff() RETURNS trigger AS $$
    DECLARE s shipments%ROWTYPE; c transport_containers%ROWTYPE;
    BEGIN
      IF TG_OP='DELETE' THEN RAISE EXCEPTION 'Handoff records cannot be deleted'; END IF;
      IF TG_OP='INSERT' THEN
        IF NEW.kind <> 'DELIVERY' THEN RETURN NEW; END IF;
        SELECT * INTO s FROM shipments WHERE id=NEW.shipment_id FOR UPDATE;
        SELECT * INTO c FROM transport_containers WHERE id=NEW.container_id FOR UPDATE;
        IF NEW.status <> 'PENDING' OR s.id IS NULL OR c.id IS NULL OR c.shipment_id <> s.id OR
           s.sealed_at IS NULL OR s.status NOT IN ('IN_TRANSIT','DELIVERY_PENDING','PARTIALLY_DELIVERED') OR
           c.delivery_status <> 'IN_TRANSIT' OR NEW.initiated_by IS DISTINCT FROM s.courier_user_id THEN
          RAISE EXCEPTION 'Delivery must start pending for the assigned Courier and an in-transit container';
        END IF;
        IF NOT EXISTS (SELECT 1 FROM handoffs WHERE shipment_id=s.id AND kind='PICKUP' AND status='CONFIRMED') OR
           NOT EXISTS (SELECT 1 FROM memberships m JOIN users u ON u.id=m.user_id JOIN regions r ON r.id=m.region_id
             WHERE m.user_id=NEW.initiated_by AND m.organization_id IS NULL AND m.region_id=s.region_id
               AND m.role='COURIER' AND m.is_active AND u.is_active AND r.is_active) THEN
          RAISE EXCEPTION 'Delivery requires confirmed Pickup and an active assigned Courier';
        END IF;
        IF NEW.attempt_no <> COALESCE((SELECT max(attempt_no)+1 FROM handoffs
             WHERE container_id=c.id AND kind='DELIVERY'),1) THEN
          RAISE EXCEPTION 'Delivery attempt number is not sequential for the container';
        END IF;
        IF EXISTS (SELECT 1 FROM handoffs WHERE container_id=c.id AND kind='DELIVERY'
             AND status IN ('PENDING','CONFIRMED')) THEN
          RAISE EXCEPTION 'Container already has an active Delivery handoff';
        END IF;
        RETURN NEW;
      END IF;
      IF OLD.kind <> 'DELIVERY' THEN RETURN NEW; END IF;
      SELECT * INTO s FROM shipments WHERE id=OLD.shipment_id FOR UPDATE;
      SELECT * INTO c FROM transport_containers WHERE id=OLD.container_id FOR UPDATE;
      IF NEW.id IS DISTINCT FROM OLD.id OR NEW.shipment_id IS DISTINCT FROM OLD.shipment_id OR
         NEW.kind IS DISTINCT FROM OLD.kind OR NEW.container_id IS DISTINCT FROM OLD.container_id OR
         NEW.attempt_no IS DISTINCT FROM OLD.attempt_no OR NEW.initiated_by IS DISTINCT FROM OLD.initiated_by OR
         NEW.initiated_at IS DISTINCT FROM OLD.initiated_at OR OLD.status <> 'PENDING' THEN
        RAISE EXCEPTION 'Terminal Delivery handoff is immutable';
      END IF;
      IF NEW.status='CONFIRMED' THEN
        IF c.delivery_status <> 'DELIVERY_PENDING' OR
           s.status NOT IN ('DELIVERY_PENDING','PARTIALLY_DELIVERED') OR
           NEW.confirmed_by IS NULL OR NEW.confirmed_at IS NULL OR
           NEW.confirmed_by=OLD.initiated_by OR NEW.withdrawn_by IS NOT NULL OR
           NEW.withdrawn_at IS NOT NULL OR NEW.withdrawal_reason IS NOT NULL OR
           NOT EXISTS (SELECT 1 FROM memberships m JOIN users u ON u.id=m.user_id
             JOIN organizations o ON o.id=m.organization_id JOIN regions r ON r.id=o.region_id
             WHERE m.user_id=NEW.confirmed_by AND m.organization_id=s.destination_organization_id
               AND m.role IN ('LAB_OPERATOR','LAB_MANAGER') AND m.is_active AND u.is_active
               AND o.is_active AND r.is_active) THEN
          RAISE EXCEPTION 'Delivery receipt requires a different active destination Lab operator';
        END IF;
      ELSIF NEW.status='WITHDRAWN' THEN
        IF c.delivery_status <> 'DELIVERY_PENDING' OR
           s.status NOT IN ('DELIVERY_PENDING','PARTIALLY_DELIVERED') OR
           NEW.withdrawn_by IS DISTINCT FROM OLD.initiated_by OR
           NEW.withdrawn_at IS NULL OR length(trim(COALESCE(NEW.withdrawal_reason,'')))=0 OR
           NEW.confirmed_by IS NOT NULL OR NEW.confirmed_at IS NOT NULL THEN
          RAISE EXCEPTION 'Only the initiating Courier can withdraw a pending Delivery with a reason';
        END IF;
      ELSE
        RAISE EXCEPTION 'Invalid Delivery handoff transition';
      END IF;
      RETURN NEW;
    END; $$ LANGUAGE plpgsql
    """)
    op.execute("DROP TRIGGER trg_guard_pickup_handoff ON handoffs")
    op.execute("""
    CREATE TRIGGER trg_guard_pickup_handoff_insert BEFORE INSERT ON handoffs
    FOR EACH ROW WHEN (NEW.kind='PICKUP') EXECUTE FUNCTION guard_pickup_handoff()
    """)
    op.execute("""
    CREATE TRIGGER trg_guard_pickup_handoff_update BEFORE UPDATE ON handoffs
    FOR EACH ROW WHEN (NEW.kind='PICKUP') EXECUTE FUNCTION guard_pickup_handoff()
    """)
    op.execute("""
    CREATE TRIGGER trg_guard_pickup_handoff_delete BEFORE DELETE ON handoffs
    FOR EACH ROW WHEN (OLD.kind='PICKUP') EXECUTE FUNCTION guard_pickup_handoff()
    """)
    op.execute("""
    CREATE TRIGGER trg_guard_delivery_handoff BEFORE INSERT OR UPDATE OR DELETE
    ON handoffs FOR EACH ROW EXECUTE FUNCTION guard_delivery_handoff()
    """)

    op.execute("""
    CREATE FUNCTION guard_delivery_shipment() RETURNS trigger AS $$
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
        ELSIF OLD.status IN ('IN_TRANSIT','DELIVERY_PENDING','DELIVERY_BLOCKED','PARTIALLY_DELIVERED','RECEIVING') AND
              NEW.status IN ('IN_TRANSIT','DELIVERY_PENDING','DELIVERY_BLOCKED','PARTIALLY_DELIVERED','RECEIVING') THEN
          IF NOT EXISTS (SELECT 1 FROM handoffs WHERE shipment_id=NEW.id AND kind='PICKUP' AND status='CONFIRMED') THEN
            RAISE EXCEPTION 'Delivery requires confirmed whole-Shipment Pickup';
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
    op.execute("DROP TRIGGER trg_guard_shipment_identity ON shipments")
    op.execute("""
    CREATE TRIGGER trg_guard_shipment_identity BEFORE INSERT OR UPDATE OR DELETE ON shipments
    FOR EACH ROW EXECUTE FUNCTION guard_delivery_shipment()
    """)

    op.execute("""
    CREATE FUNCTION guard_delivery_container() RETURNS trigger AS $$
    DECLARE shipment_key uuid;
    BEGIN
      IF NEW.shipment_id IS DISTINCT FROM OLD.shipment_id OR NEW.code IS DISTINCT FROM OLD.code THEN
        RAISE EXCEPTION 'Container identity is immutable';
      END IF;
      IF NEW.delivery_status IS DISTINCT FROM OLD.delivery_status THEN
        SELECT id INTO shipment_key FROM shipments WHERE id=NEW.shipment_id FOR UPDATE;
        IF OLD.delivery_status='PREPARED' AND NEW.delivery_status='IN_TRANSIT' THEN
          IF NOT EXISTS (SELECT 1 FROM handoffs WHERE shipment_id=shipment_key AND kind='PICKUP' AND status='CONFIRMED') OR
             NEW.state_version <> OLD.state_version + 1 THEN
            RAISE EXCEPTION 'confirmed Pickup is required before transit';
          END IF;
        ELSIF OLD.delivery_status='IN_TRANSIT' AND NEW.delivery_status='DELIVERY_PENDING' THEN
          IF NEW.state_version <> OLD.state_version + 1 OR
             NOT EXISTS (SELECT 1 FROM handoffs WHERE shipment_id=shipment_key AND container_id=NEW.id
               AND kind='DELIVERY' AND status='PENDING') THEN
            RAISE EXCEPTION 'Pending Delivery handoff is required for container';
          END IF;
        ELSIF OLD.delivery_status='DELIVERY_PENDING' AND NEW.delivery_status='IN_TRANSIT' THEN
          IF NEW.state_version <> OLD.state_version + 1 OR
             NOT EXISTS (SELECT 1 FROM handoffs WHERE shipment_id=shipment_key AND container_id=NEW.id
               AND kind='DELIVERY' AND status='WITHDRAWN') THEN
            RAISE EXCEPTION 'Withdrawn Delivery handoff is required to return to transit';
          END IF;
        ELSIF OLD.delivery_status='DELIVERY_PENDING' AND NEW.delivery_status='RECEIVED' THEN
          IF NEW.state_version <> OLD.state_version + 1 OR NEW.received_at IS NULL OR NEW.received_by IS NULL OR
             NEW.receipt_evidence->>'method' <> 'NORMAL' OR
             NOT EXISTS (SELECT 1 FROM handoffs h WHERE h.shipment_id=shipment_key AND h.container_id=NEW.id
               AND h.kind='DELIVERY' AND h.status='CONFIRMED' AND h.confirmed_by=NEW.received_by) THEN
            RAISE EXCEPTION 'Delivery receipt requires a confirmed handoff and matching Lab receiver';
          END IF;
        ELSE
          RAISE EXCEPTION 'Invalid container custody transition';
        END IF;
      ELSIF OLD.delivery_status='RECEIVED' AND (
        NEW.received_at IS DISTINCT FROM OLD.received_at OR NEW.received_by IS DISTINCT FROM OLD.received_by OR
        NEW.receipt_evidence IS DISTINCT FROM OLD.receipt_evidence
      ) THEN
        RAISE EXCEPTION 'Confirmed container receipt is immutable';
      ELSIF OLD.delivery_status <> 'RECEIVED' AND (
        NEW.received_at IS DISTINCT FROM OLD.received_at OR NEW.received_by IS DISTINCT FROM OLD.received_by OR
        NEW.receipt_evidence IS DISTINCT FROM OLD.receipt_evidence
      ) THEN
        RAISE EXCEPTION 'Receipt evidence can only be written with container receipt';
      END IF;
      RETURN NEW;
    END; $$ LANGUAGE plpgsql
    """)
    op.execute("DROP TRIGGER trg_guard_container_transit ON transport_containers")
    op.execute("""
    CREATE TRIGGER trg_guard_container_transit BEFORE UPDATE ON transport_containers
    FOR EACH ROW EXECUTE FUNCTION guard_delivery_container()
    """)

    op.execute("""
    CREATE FUNCTION validate_delivery_state_consistency() RETURNS trigger AS $$
    DECLARE shipment_key uuid; shipment_state text; pending_pickup integer; confirmed_pickup integer;
      container_count integer; prepared_count integer; transit_count integer; delivery_pending_count integer;
      received_count integer; blocked_count integer; undelivered_count integer; open_sessions integer;
    BEGIN
      IF TG_TABLE_NAME='shipments' THEN
        shipment_key := COALESCE(NEW.id, OLD.id);
      ELSIF TG_TABLE_NAME='receiving_sessions' THEN
        shipment_key := COALESCE(NEW.shipment_id, OLD.shipment_id);
      ELSE
        shipment_key := COALESCE(NEW.shipment_id, OLD.shipment_id);
      END IF;
      SELECT status INTO shipment_state FROM shipments WHERE id=shipment_key;
      IF shipment_state IS NULL THEN RETURN NULL; END IF;
      SELECT count(*) FILTER (WHERE status='PENDING'), count(*) FILTER (WHERE status='CONFIRMED')
        INTO pending_pickup, confirmed_pickup FROM handoffs WHERE shipment_id=shipment_key AND kind='PICKUP';
      SELECT count(*), count(*) FILTER (WHERE delivery_status='PREPARED'),
        count(*) FILTER (WHERE delivery_status='IN_TRANSIT'),
        count(*) FILTER (WHERE delivery_status='DELIVERY_PENDING'),
        count(*) FILTER (WHERE delivery_status='RECEIVED'),
        count(*) FILTER (WHERE delivery_status='BLOCKED'),
        count(*) FILTER (WHERE delivery_status='UNDELIVERED_CLOSED')
        INTO container_count, prepared_count, transit_count, delivery_pending_count,
          received_count, blocked_count, undelivered_count
        FROM transport_containers WHERE shipment_id=shipment_key;
      SELECT count(*) INTO open_sessions FROM receiving_sessions WHERE shipment_id=shipment_key AND status='OPEN';
      IF shipment_state='PICKUP_PENDING' AND pending_pickup <> 1 THEN
        RAISE EXCEPTION 'Pending Pickup must match PICKUP_PENDING shipment state';
      ELSIF shipment_state IN ('DRAFT','READY_FOR_PICKUP','CANCELLED') AND
        (pending_pickup > 0 OR confirmed_pickup > 0) THEN
        RAISE EXCEPTION 'Active Pickup handoff must match shipment state';
      ELSIF shipment_state IN ('IN_TRANSIT','DELIVERY_PENDING','DELIVERY_BLOCKED','PARTIALLY_DELIVERED',
                               'RECEIVING','COMPLETED','COMPLETED_WITH_ISSUES','CLOSED_UNDELIVERED') AND
        confirmed_pickup <> 1 THEN
        RAISE EXCEPTION 'Delivery state requires one confirmed whole-Shipment Pickup';
      END IF;
      IF shipment_state IN ('DRAFT','READY_FOR_PICKUP','PICKUP_PENDING','CANCELLED') THEN
        IF EXISTS (SELECT 1 FROM transport_containers WHERE shipment_id=shipment_key AND delivery_status <> 'PREPARED') OR
           EXISTS (SELECT 1 FROM handoffs WHERE shipment_id=shipment_key AND kind='DELIVERY') THEN
          RAISE EXCEPTION 'Pre-Pickup shipment cannot contain Delivery custody';
        END IF;
      ELSE
        IF container_count=0 OR prepared_count>0 THEN
          RAISE EXCEPTION 'Picked-up Shipment requires only initialized containers';
        END IF;
        IF EXISTS (SELECT 1 FROM transport_containers c WHERE c.shipment_id=shipment_key AND
          ((c.delivery_status='IN_TRANSIT' AND EXISTS (SELECT 1 FROM handoffs h WHERE h.container_id=c.id
             AND h.kind='DELIVERY' AND h.status IN ('PENDING','CONFIRMED'))) OR
           (c.delivery_status='DELIVERY_PENDING' AND 1 <> (SELECT count(*) FROM handoffs h WHERE h.container_id=c.id
             AND h.kind='DELIVERY' AND h.status='PENDING')) OR
           (c.delivery_status='RECEIVED' AND 1 <> (SELECT count(*) FROM handoffs h WHERE h.container_id=c.id
             AND h.kind='DELIVERY' AND h.status='CONFIRMED' AND h.confirmed_by=c.received_by)) OR
           (c.delivery_status IN ('BLOCKED','UNDELIVERED_CLOSED') AND EXISTS (SELECT 1 FROM handoffs h
             WHERE h.container_id=c.id AND h.kind='DELIVERY' AND h.status IN ('PENDING','CONFIRMED'))))) THEN
          RAISE EXCEPTION 'Container state must match its Delivery handoff';
        END IF;
        IF received_count=0 AND open_sessions <> 0 OR received_count>0 AND open_sessions <> 1 THEN
          RAISE EXCEPTION 'An OPEN receiving session must match received containers';
        END IF;
        IF shipment_state='IN_TRANSIT' AND
          (transit_count <> container_count OR delivery_pending_count>0 OR blocked_count>0 OR received_count>0) THEN
          RAISE EXCEPTION 'IN_TRANSIT shipment must have only transit containers';
        ELSIF shipment_state='DELIVERY_PENDING' AND
          (received_count>0 OR blocked_count>0 OR delivery_pending_count=0) THEN
          RAISE EXCEPTION 'DELIVERY_PENDING shipment must have a pending Delivery and no receipt or block';
        ELSIF shipment_state='DELIVERY_BLOCKED' AND (received_count>0 OR blocked_count=0) THEN
          RAISE EXCEPTION 'DELIVERY_BLOCKED shipment must have blocked containers and no receipt';
        ELSIF shipment_state='PARTIALLY_DELIVERED' AND
          (received_count=0 OR transit_count+delivery_pending_count+blocked_count+undelivered_count=0) THEN
          RAISE EXCEPTION 'PARTIALLY_DELIVERED shipment must have received and outstanding containers';
        ELSIF shipment_state='RECEIVING' AND
          (received_count=0 OR transit_count+delivery_pending_count+blocked_count>0) THEN
          RAISE EXCEPTION 'RECEIVING shipment must have receipts and no unresolved containers';
        ELSIF shipment_state='CLOSED_UNDELIVERED' AND undelivered_count <> container_count THEN
          RAISE EXCEPTION 'CLOSED_UNDELIVERED requires every container dispositioned undelivered';
        END IF;
      END IF;
      RETURN NULL;
    END; $$ LANGUAGE plpgsql
    """)
    op.execute("DROP TRIGGER trg_validate_pickup_container_consistency ON transport_containers")
    op.execute("DROP TRIGGER trg_validate_pickup_shipment_consistency ON shipments")
    op.execute("DROP TRIGGER trg_validate_pickup_handoff_consistency ON handoffs")
    op.execute("""
    CREATE CONSTRAINT TRIGGER trg_validate_delivery_handoff_consistency
    AFTER INSERT OR UPDATE ON handoffs DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION validate_delivery_state_consistency()
    """)
    op.execute("""
    CREATE CONSTRAINT TRIGGER trg_validate_delivery_shipment_consistency
    AFTER INSERT OR UPDATE ON shipments DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION validate_delivery_state_consistency()
    """)
    op.execute("""
    CREATE CONSTRAINT TRIGGER trg_validate_delivery_container_consistency
    AFTER INSERT OR UPDATE ON transport_containers DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION validate_delivery_state_consistency()
    """)
    op.execute("""
    CREATE CONSTRAINT TRIGGER trg_validate_delivery_session_consistency
    AFTER INSERT OR UPDATE ON receiving_sessions DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION validate_delivery_state_consistency()
    """)


def downgrade() -> None:
    op.execute("""
    DO $$ BEGIN
      IF EXISTS (SELECT 1 FROM handoffs WHERE kind='DELIVERY') OR
         EXISTS (SELECT 1 FROM receiving_sessions) OR
         EXISTS (SELECT 1 FROM transport_containers WHERE delivery_status NOT IN ('PREPARED','IN_TRANSIT')) THEN
        RAISE EXCEPTION 'Cannot downgrade Stage D while Delivery or receiving data exists';
      END IF;
    END $$
    """)
    op.execute(
        "DROP TRIGGER IF EXISTS trg_validate_delivery_session_consistency ON receiving_sessions"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_validate_delivery_container_consistency ON transport_containers"
    )
    op.execute("DROP TRIGGER IF EXISTS trg_validate_delivery_shipment_consistency ON shipments")
    op.execute("DROP TRIGGER IF EXISTS trg_validate_delivery_handoff_consistency ON handoffs")
    op.execute("DROP FUNCTION IF EXISTS validate_delivery_state_consistency()")
    op.execute("DROP TRIGGER IF EXISTS trg_guard_delivery_handoff ON handoffs")
    op.execute("DROP FUNCTION IF EXISTS guard_delivery_handoff()")
    op.execute("DROP TRIGGER IF EXISTS trg_guard_pickup_handoff_insert ON handoffs")
    op.execute("DROP TRIGGER IF EXISTS trg_guard_pickup_handoff_update ON handoffs")
    op.execute("DROP TRIGGER IF EXISTS trg_guard_pickup_handoff_delete ON handoffs")
    op.execute("""
    CREATE TRIGGER trg_guard_pickup_handoff BEFORE INSERT OR UPDATE OR DELETE ON handoffs
    FOR EACH ROW EXECUTE FUNCTION guard_pickup_handoff()
    """)
    op.execute("DROP TRIGGER IF EXISTS trg_guard_shipment_identity ON shipments")
    op.execute("DROP FUNCTION IF EXISTS guard_delivery_shipment()")
    op.execute("""
    CREATE TRIGGER trg_guard_shipment_identity BEFORE INSERT OR UPDATE OR DELETE ON shipments
    FOR EACH ROW EXECUTE FUNCTION guard_sealed_shipment()
    """)
    op.execute("DROP TRIGGER IF EXISTS trg_guard_container_transit ON transport_containers")
    op.execute("DROP FUNCTION IF EXISTS guard_delivery_container()")
    op.execute("""
    CREATE TRIGGER trg_guard_container_transit BEFORE UPDATE ON transport_containers
    FOR EACH ROW EXECUTE FUNCTION guard_container_transit()
    """)
    op.execute("DROP FUNCTION IF EXISTS validate_delivery_state_consistency()")
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
    op.drop_index("uq_receiving_session_open", table_name="receiving_sessions")
    op.drop_table("receiving_sessions")
    op.drop_index("uq_handoff_delivery_active_or_confirmed", table_name="handoffs")
    op.drop_index("uq_handoff_pickup_active_or_confirmed", table_name="handoffs")
    op.drop_index("uq_handoff_delivery_attempt", table_name="handoffs")
    op.create_index(
        "uq_handoff_delivery_attempt",
        "handoffs",
        ["shipment_id", "container_id", "attempt_no"],
        unique=True,
        postgresql_where=sa.text("kind = 'DELIVERY'"),
    )
    op.drop_constraint(
        op.f("ck_transport_containers_receipt_fields"), "transport_containers", type_="check"
    )
    op.drop_constraint(
        op.f("ck_transport_containers_delivery_status"), "transport_containers", type_="check"
    )
    op.drop_constraint(op.f("ck_shipments_manifest_status"), "shipments", type_="check")
    op.create_check_constraint(
        "manifest_status",
        "shipments",
        "status IN ('DRAFT', 'READY_FOR_PICKUP', 'PICKUP_PENDING', 'IN_TRANSIT', 'CANCELLED')",
    )
