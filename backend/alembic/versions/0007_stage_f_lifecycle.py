"""Permit guarded receiving completion and manager reopen transitions."""

# PostgreSQL trigger bodies are embedded as multiline SQL strings.
# ruff: noqa: E501

from collections.abc import Sequence

from alembic import op

revision: str = "0007_stage_f_lifecycle"
down_revision: str | Sequence[str] | None = "0006_stage_f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
    CREATE OR REPLACE FUNCTION guard_delivery_shipment() RETURNS trigger AS $$
    DECLARE
      container_count integer;
      received_count integer;
      undelivered_count integer;
      unresolved_count integer;
      missing_received_items integer;
      nonaccepted_received_items integer;
      undelivered_outcome_items integer;
      open_sessions integer;
    BEGIN
      IF TG_OP='INSERT' THEN
        IF NEW.status <> 'DRAFT' OR NEW.sealed_at IS NOT NULL THEN
          RAISE EXCEPTION 'Shipment must be created in DRAFT';
        END IF;
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
        ELSIF OLD.status='RECEIVING' AND NEW.status IN ('COMPLETED','COMPLETED_WITH_ISSUES') THEN
          SELECT count(*), count(*) FILTER (WHERE delivery_status='RECEIVED'),
            count(*) FILTER (WHERE delivery_status='UNDELIVERED_CLOSED'),
            count(*) FILTER (WHERE delivery_status NOT IN ('RECEIVED','UNDELIVERED_CLOSED'))
            INTO container_count, received_count, undelivered_count, unresolved_count
            FROM transport_containers WHERE shipment_id=NEW.id;
          SELECT count(*) INTO missing_received_items
            FROM transport_containers c JOIN container_items i ON i.container_id=c.id
            WHERE c.shipment_id=NEW.id AND c.delivery_status='RECEIVED' AND
              (i.receipt_result IS NULL OR i.receipt_result NOT IN ('ACCEPTED','MISSING','REJECTED') OR
               i.receipt_recorded_at IS NULL);
          SELECT count(*) INTO nonaccepted_received_items
            FROM transport_containers c JOIN container_items i ON i.container_id=c.id
            WHERE c.shipment_id=NEW.id AND c.delivery_status='RECEIVED' AND
              i.receipt_result IS DISTINCT FROM 'ACCEPTED';
          SELECT count(*) INTO undelivered_outcome_items
            FROM transport_containers c JOIN container_items i ON i.container_id=c.id
            WHERE c.shipment_id=NEW.id AND c.delivery_status='UNDELIVERED_CLOSED' AND
              (i.receipt_result IS NOT NULL OR i.receipt_recorded_at IS NOT NULL OR
               i.receipt_reason_code IS NOT NULL OR i.receipt_comment IS NOT NULL);
          SELECT count(*) INTO open_sessions FROM receiving_sessions
            WHERE shipment_id=NEW.id AND status='OPEN';
          IF container_count=0 OR received_count=0 OR unresolved_count>0 THEN
            RAISE EXCEPTION 'Completion requires terminal containers and at least one received container';
          ELSIF missing_received_items>0 THEN
            RAISE EXCEPTION 'Every received item must have a result';
          ELSIF undelivered_outcome_items>0 THEN
            RAISE EXCEPTION 'Undelivered container items must not have outcomes';
          ELSIF open_sessions<>0 THEN
            RAISE EXCEPTION 'Completion requires no OPEN receiving session';
          END IF;
          IF NEW.status='COMPLETED' AND
            (received_count<>container_count OR undelivered_count>0 OR nonaccepted_received_items>0) THEN
            RAISE EXCEPTION 'COMPLETED requires all received item results to be ACCEPTED';
          ELSIF NEW.status='COMPLETED_WITH_ISSUES' AND
            received_count=container_count AND undelivered_count=0 AND nonaccepted_received_items=0 THEN
            RAISE EXCEPTION 'COMPLETED_WITH_ISSUES requires at least one receiving issue';
          END IF;
        ELSIF OLD.status IN ('COMPLETED','COMPLETED_WITH_ISSUES') AND NEW.status='RECEIVING' THEN
          IF (SELECT count(*) FROM receiving_sessions WHERE shipment_id=NEW.id AND status='OPEN') <> 1 OR
             NOT EXISTS (
               SELECT 1 FROM receiving_sessions rs
               WHERE rs.shipment_id=NEW.id AND rs.status='OPEN' AND
                 rs.sequence=(SELECT max(sequence) FROM receiving_sessions WHERE shipment_id=NEW.id) AND
                 length(trim(COALESCE(rs.reopen_reason,'')))>0
             ) THEN
            RAISE EXCEPTION 'Reopen requires exactly one OPEN receiving session with a reason';
          END IF;
          IF NOT EXISTS (
            SELECT 1 FROM receiving_sessions rs
              JOIN memberships m ON m.user_id=rs.opened_by
              JOIN users u ON u.id=m.user_id
              JOIN organizations o ON o.id=m.organization_id
              JOIN regions r ON r.id=o.region_id
            WHERE rs.shipment_id=NEW.id AND rs.status='OPEN' AND
              rs.sequence=(SELECT max(sequence) FROM receiving_sessions WHERE shipment_id=NEW.id) AND
              m.organization_id=NEW.destination_organization_id AND m.role='LAB_MANAGER' AND
              m.is_active AND u.is_active AND o.is_active AND r.is_active AND
              o.region_id=NEW.region_id
          ) THEN
            RAISE EXCEPTION 'Reopen requires an active destination LAB_MANAGER opener';
          END IF;
        ELSIF OLD.status IN ('IN_TRANSIT','DELIVERY_PENDING','DELIVERY_BLOCKED','PARTIALLY_DELIVERED','RECEIVING') AND
              NEW.status='CLOSED_UNDELIVERED' THEN
          IF NOT EXISTS (SELECT 1 FROM transport_containers WHERE shipment_id=NEW.id) OR
             EXISTS (SELECT 1 FROM transport_containers WHERE shipment_id=NEW.id AND delivery_status<>'UNDELIVERED_CLOSED') OR
             EXISTS (SELECT 1 FROM receiving_sessions WHERE shipment_id=NEW.id AND status='OPEN') THEN
            RAISE EXCEPTION 'CLOSED_UNDELIVERED requires every container dispositioned undelivered';
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

    op.execute("""
    CREATE OR REPLACE FUNCTION validate_delivery_state_consistency() RETURNS trigger AS $$
    DECLARE shipment_key uuid; shipment_state text; pending_pickup integer; confirmed_pickup integer;
      container_count integer; prepared_count integer; transit_count integer; delivery_pending_count integer;
      received_count integer; blocked_count integer; undelivered_count integer; open_sessions integer;
      missing_received_items integer; nonaccepted_received_items integer;
      undelivered_outcome_items integer; invalid_open_sessions integer;
    BEGIN
      IF TG_TABLE_NAME='shipments' THEN
        shipment_key := COALESCE(NEW.id, OLD.id);
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
      SELECT count(*) INTO open_sessions FROM receiving_sessions
        WHERE shipment_id=shipment_key AND status='OPEN';
      SELECT count(*) INTO invalid_open_sessions FROM receiving_sessions rs
        WHERE rs.shipment_id=shipment_key AND rs.status='OPEN' AND
          (rs.sequence<>(SELECT max(sequence) FROM receiving_sessions WHERE shipment_id=shipment_key) OR
           (rs.sequence>1 AND length(trim(COALESCE(rs.reopen_reason,'')))=0));
      SELECT count(*) INTO missing_received_items
        FROM transport_containers c JOIN container_items i ON i.container_id=c.id
        WHERE c.shipment_id=shipment_key AND c.delivery_status='RECEIVED' AND
          (i.receipt_result IS NULL OR i.receipt_result NOT IN ('ACCEPTED','MISSING','REJECTED') OR
           i.receipt_recorded_at IS NULL);
      SELECT count(*) INTO nonaccepted_received_items
        FROM transport_containers c JOIN container_items i ON i.container_id=c.id
        WHERE c.shipment_id=shipment_key AND c.delivery_status='RECEIVED' AND
          i.receipt_result IS DISTINCT FROM 'ACCEPTED';
      SELECT count(*) INTO undelivered_outcome_items
        FROM transport_containers c JOIN container_items i ON i.container_id=c.id
        WHERE c.shipment_id=shipment_key AND c.delivery_status='UNDELIVERED_CLOSED' AND
          (i.receipt_result IS NOT NULL OR i.receipt_recorded_at IS NOT NULL OR
           i.receipt_reason_code IS NOT NULL OR i.receipt_comment IS NOT NULL);
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
           (c.delivery_status='RECEIVED' AND NOT (
             (c.received_by IS NOT NULL AND 1 = (SELECT count(*) FROM handoffs h WHERE h.container_id=c.id
               AND h.kind='DELIVERY' AND h.status='CONFIRMED' AND h.confirmed_by=c.received_by)) OR
             (c.received_by IS NULL AND EXISTS (SELECT 1 FROM container_receipt_evidence e
               JOIN custody_incidents i ON i.id=e.resolved_incident_id WHERE e.container_id=c.id
                 AND e.method='EXCEPTIONAL' AND i.status='RESOLVED'
                 AND i.resolution='EXCEPTIONAL_RECEIPT_VERIFIED')))) OR
           (c.delivery_status IN ('BLOCKED','UNDELIVERED_CLOSED') AND EXISTS (SELECT 1 FROM handoffs h
             WHERE h.container_id=c.id AND h.kind='DELIVERY' AND h.status IN ('PENDING','CONFIRMED'))))) THEN
          RAISE EXCEPTION 'Container state must match its Delivery handoff';
        END IF;
        IF undelivered_outcome_items>0 THEN
          RAISE EXCEPTION 'Undelivered container items must not have outcomes';
        END IF;
        IF shipment_state IN ('COMPLETED','COMPLETED_WITH_ISSUES','CLOSED_UNDELIVERED') THEN
          IF open_sessions<>0 THEN
            RAISE EXCEPTION 'Terminal shipment must not have an OPEN receiving session';
          END IF;
        ELSIF received_count=0 AND open_sessions<>0 OR received_count>0 AND open_sessions<>1 THEN
          RAISE EXCEPTION 'An OPEN receiving session must match received containers';
        END IF;
        IF shipment_state='RECEIVING' AND invalid_open_sessions>0 THEN
          RAISE EXCEPTION 'Reopened RECEIVING requires the latest OPEN session to have a reason';
        END IF;
        IF shipment_state='IN_TRANSIT' AND
          (transit_count <> container_count OR delivery_pending_count>0 OR blocked_count>0 OR received_count>0 OR undelivered_count>0) THEN
          RAISE EXCEPTION 'IN_TRANSIT shipment must have only transit containers';
        ELSIF shipment_state='DELIVERY_PENDING' AND
          (received_count>0 OR blocked_count>0 OR delivery_pending_count=0 OR undelivered_count>0) THEN
          RAISE EXCEPTION 'DELIVERY_PENDING shipment must have a pending Delivery and no receipt or block';
        ELSIF shipment_state='DELIVERY_BLOCKED' AND
          (received_count>0 OR blocked_count+undelivered_count=0) THEN
          RAISE EXCEPTION 'DELIVERY_BLOCKED shipment must have blocked or closed containers and no receipt';
        ELSIF shipment_state='PARTIALLY_DELIVERED' AND
          (received_count=0 OR transit_count+delivery_pending_count+blocked_count+undelivered_count=0) THEN
          RAISE EXCEPTION 'PARTIALLY_DELIVERED shipment must have received and outstanding containers';
        ELSIF shipment_state='RECEIVING' AND
          (received_count=0 OR transit_count+delivery_pending_count+blocked_count>0) THEN
          RAISE EXCEPTION 'RECEIVING shipment must have receipts and no unresolved containers';
        ELSIF shipment_state='CLOSED_UNDELIVERED' AND
          (undelivered_count <> container_count OR received_count>0) THEN
          RAISE EXCEPTION 'CLOSED_UNDELIVERED requires every container dispositioned undelivered';
        ELSIF shipment_state='COMPLETED' AND
          (received_count<>container_count OR undelivered_count>0 OR transit_count>0 OR
           delivery_pending_count>0 OR blocked_count>0 OR missing_received_items>0 OR
           nonaccepted_received_items>0) THEN
          RAISE EXCEPTION 'COMPLETED requires every container received with ACCEPTED results';
        ELSIF shipment_state='COMPLETED_WITH_ISSUES' AND
          (received_count=0 OR received_count+undelivered_count<>container_count OR
           transit_count>0 OR delivery_pending_count>0 OR blocked_count>0 OR missing_received_items>0 OR
           (received_count=container_count AND undelivered_count=0 AND nonaccepted_received_items=0)) THEN
          RAISE EXCEPTION 'COMPLETED_WITH_ISSUES requires terminal containers and at least one receiving issue';
        END IF;
      END IF;
      RETURN NULL;
    END; $$ LANGUAGE plpgsql
    """)

    op.execute("""
    CREATE FUNCTION guard_receiving_session() RETURNS trigger AS $$
    DECLARE destination_id uuid; shipment_region_id uuid; expected_sequence integer;
    BEGIN
      IF TG_OP='DELETE' THEN
        RAISE EXCEPTION 'Receiving sessions cannot be deleted';
      END IF;
      IF TG_OP='UPDATE' THEN
        IF OLD.status='CLOSED' THEN
          RAISE EXCEPTION 'Closed receiving sessions are immutable';
        END IF;
        IF OLD.status<>'OPEN' OR NEW.status<>'CLOSED' THEN
          RAISE EXCEPTION 'Receiving sessions may only transition from OPEN to CLOSED';
        END IF;
        IF NEW.id IS DISTINCT FROM OLD.id OR NEW.shipment_id IS DISTINCT FROM OLD.shipment_id OR
           NEW.sequence IS DISTINCT FROM OLD.sequence OR NEW.opened_by IS DISTINCT FROM OLD.opened_by OR
           NEW.opened_at IS DISTINCT FROM OLD.opened_at OR
           NEW.reopen_reason IS DISTINCT FROM OLD.reopen_reason THEN
          RAISE EXCEPTION 'Receiving session opening fields are immutable';
        END IF;
        RETURN NEW;
      END IF;

      IF NEW.status<>'OPEN' OR NEW.closed_by IS NOT NULL OR NEW.closed_at IS NOT NULL THEN
        RAISE EXCEPTION 'Receiving sessions must be created OPEN';
      END IF;
      SELECT destination_organization_id, region_id
        INTO destination_id, shipment_region_id
        FROM shipments WHERE id=NEW.shipment_id;
      IF destination_id IS NULL THEN
        RAISE EXCEPTION 'Receiving session requires an existing Shipment';
      END IF;
      SELECT COALESCE(max(sequence), 0)+1 INTO expected_sequence
        FROM receiving_sessions WHERE shipment_id=NEW.shipment_id;
      IF NEW.sequence<>expected_sequence THEN
        RAISE EXCEPTION 'Receiving session sequence must be the next Shipment sequence';
      END IF;

      IF NEW.sequence=1 THEN
        IF NEW.reopen_reason IS NOT NULL THEN
          RAISE EXCEPTION 'Initial receiving session cannot have a reopen reason';
        END IF;
        IF NOT EXISTS (
          SELECT 1 FROM transport_containers c
            JOIN container_receipt_evidence e ON e.container_id=c.id
            JOIN handoffs h ON h.id=e.confirmed_delivery_handoff_id
            JOIN memberships m ON m.user_id=NEW.opened_by
            JOIN users u ON u.id=m.user_id
            JOIN organizations o ON o.id=m.organization_id
            JOIN regions r ON r.id=o.region_id
          WHERE c.shipment_id=NEW.shipment_id AND c.delivery_status='RECEIVED' AND
            c.received_by=NEW.opened_by AND c.receipt_evidence->>'method'='NORMAL' AND
            c.receipt_evidence->>'id'=e.id::text AND e.method='NORMAL' AND
            e.verified_by=NEW.opened_by AND c.receipt_evidence->>'verified_by'=NEW.opened_by::text AND
            e.confirmed_delivery_handoff_id=h.id AND h.shipment_id=NEW.shipment_id AND
            h.container_id=c.id AND h.kind='DELIVERY' AND h.status='CONFIRMED' AND
            h.confirmed_by=NEW.opened_by AND m.organization_id=destination_id AND
            m.role IN ('LAB_OPERATOR','LAB_MANAGER') AND m.is_active AND u.is_active AND
            o.is_active AND r.is_active AND o.region_id=shipment_region_id
        ) AND NOT EXISTS (
          SELECT 1 FROM transport_containers c
            JOIN container_receipt_evidence e ON e.container_id=c.id
            JOIN custody_incidents i ON i.id=e.resolved_incident_id
            JOIN memberships m ON m.user_id=i.resolved_by
            JOIN users u ON u.id=m.user_id
            JOIN regions r ON r.id=m.region_id
          WHERE c.shipment_id=NEW.shipment_id AND c.delivery_status='RECEIVED' AND
            c.received_by IS NULL AND c.receipt_evidence->>'method'='EXCEPTIONAL' AND
            c.receipt_evidence->>'id'=e.id::text AND
            c.receipt_evidence->>'incident_id'=i.id::text AND
            c.receipt_evidence->>'verified_by'=i.resolved_by::text AND e.method='EXCEPTIONAL' AND
            e.verified_by=i.resolved_by AND i.id=e.resolved_incident_id AND
            i.shipment_id=NEW.shipment_id AND i.container_id=c.id AND i.phase='DELIVERY' AND
            i.status='RESOLVED' AND i.resolution='EXCEPTIONAL_RECEIPT_VERIFIED' AND
            i.resolved_by=NEW.opened_by AND m.region_id=shipment_region_id AND
            m.organization_id IS NULL AND m.role='REGIONAL_OPERATIONS' AND
            m.is_active AND u.is_active AND r.is_active
        ) THEN
          RAISE EXCEPTION 'Initial receiving session opener must be a receipt confirmer or verified exceptional resolver';
        END IF;
      ELSE
        IF length(trim(COALESCE(NEW.reopen_reason,'')))=0 THEN
          RAISE EXCEPTION 'Reopened receiving session requires a nonblank reason';
        END IF;
        IF NOT EXISTS (
          SELECT 1 FROM memberships m
            JOIN users u ON u.id=m.user_id
            JOIN organizations o ON o.id=m.organization_id
            JOIN regions r ON r.id=o.region_id
          WHERE m.user_id=NEW.opened_by AND m.organization_id=destination_id AND
            m.role='LAB_MANAGER' AND m.is_active AND u.is_active AND o.is_active AND
            r.is_active AND o.region_id=shipment_region_id
        ) THEN
          RAISE EXCEPTION 'Reopened receiving session opener must be an active destination LAB_MANAGER';
        END IF;
      END IF;
      RETURN NEW;
    END; $$ LANGUAGE plpgsql
    """)
    op.execute("""
    CREATE TRIGGER trg_guard_receiving_session
    BEFORE INSERT OR UPDATE OR DELETE ON receiving_sessions
    FOR EACH ROW EXECUTE FUNCTION guard_receiving_session()
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_guard_receiving_session ON receiving_sessions")
    op.execute("DROP FUNCTION IF EXISTS guard_receiving_session()")
    op.execute("""
    CREATE OR REPLACE FUNCTION guard_delivery_shipment() RETURNS trigger AS $$
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
    op.execute("""
    CREATE OR REPLACE FUNCTION validate_delivery_state_consistency() RETURNS trigger AS $$
    DECLARE shipment_key uuid; shipment_state text; pending_pickup integer; confirmed_pickup integer;
      container_count integer; prepared_count integer; transit_count integer; delivery_pending_count integer;
      received_count integer; blocked_count integer; undelivered_count integer; open_sessions integer;
    BEGIN
      IF TG_TABLE_NAME='shipments' THEN
        shipment_key := COALESCE(NEW.id, OLD.id);
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
           (c.delivery_status='RECEIVED' AND NOT (
             (c.received_by IS NOT NULL AND 1 = (SELECT count(*) FROM handoffs h WHERE h.container_id=c.id
               AND h.kind='DELIVERY' AND h.status='CONFIRMED' AND h.confirmed_by=c.received_by)) OR
             (c.received_by IS NULL AND EXISTS (SELECT 1 FROM container_receipt_evidence e
               JOIN custody_incidents i ON i.id=e.resolved_incident_id WHERE e.container_id=c.id
                 AND e.method='EXCEPTIONAL' AND i.status='RESOLVED'
                 AND i.resolution='EXCEPTIONAL_RECEIPT_VERIFIED')))) OR
           (c.delivery_status IN ('BLOCKED','UNDELIVERED_CLOSED') AND EXISTS (SELECT 1 FROM handoffs h
             WHERE h.container_id=c.id AND h.kind='DELIVERY' AND h.status IN ('PENDING','CONFIRMED'))))) THEN
          RAISE EXCEPTION 'Container state must match its Delivery handoff';
        END IF;
        IF received_count=0 AND open_sessions <> 0 OR received_count>0 AND open_sessions <> 1 THEN
          RAISE EXCEPTION 'An OPEN receiving session must match received containers';
        END IF;
        IF shipment_state='IN_TRANSIT' AND
          (transit_count <> container_count OR delivery_pending_count>0 OR blocked_count>0 OR received_count>0 OR undelivered_count>0) THEN
          RAISE EXCEPTION 'IN_TRANSIT shipment must have only transit containers';
        ELSIF shipment_state='DELIVERY_PENDING' AND
          (received_count>0 OR blocked_count>0 OR delivery_pending_count=0 OR undelivered_count>0) THEN
          RAISE EXCEPTION 'DELIVERY_PENDING shipment must have a pending Delivery and no receipt or block';
        ELSIF shipment_state='DELIVERY_BLOCKED' AND
          (received_count>0 OR blocked_count+undelivered_count=0) THEN
          RAISE EXCEPTION 'DELIVERY_BLOCKED shipment must have blocked or closed containers and no receipt';
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
