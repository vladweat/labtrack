"""Add ContainerIssue records and independently reviewed resolution proposals."""

# PostgreSQL trigger bodies and constraint expressions are embedded as SQL strings.
# ruff: noqa: E501

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.config import get_settings

revision: str = "0005_stage_e"
down_revision: str | Sequence[str] | None = "0004_delivery"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "container_issues",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("shipment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("container_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("reason_code", sa.String(48), nullable=False),
        sa.Column("comment", sa.String(500)),
        sa.Column("status", sa.String(24), server_default="OPEN", nullable=False),
        sa.Column("reported_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "reported_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("resolved_by", postgresql.UUID(as_uuid=True)),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.Column("resolution", sa.String(24)),
        sa.ForeignKeyConstraint(["shipment_id"], ["shipments.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["shipment_id", "container_id"],
            ["transport_containers.shipment_id", "transport_containers.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["reason_code"], ["issue_reason_codes.code"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["reported_by"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["resolved_by"], ["users.id"], ondelete="RESTRICT"),
        sa.CheckConstraint(
            "kind IN ('MISSING', 'REJECTED')", name="ck_container_issues_container_issue_kind"
        ),
        sa.CheckConstraint(
            "status IN ('OPEN', 'RESOLVED', 'CLOSED_UNDELIVERED')",
            name="ck_container_issues_status",
        ),
        sa.CheckConstraint(
            "(status='OPEN' AND resolved_by IS NULL AND resolved_at IS NULL AND resolution IS NULL) OR (status IN ('RESOLVED','CLOSED_UNDELIVERED') AND resolved_by IS NOT NULL AND resolved_at IS NOT NULL AND resolution IS NOT NULL)",
            name="ck_container_issues_resolution_fields",
        ),
    )
    op.create_index(
        "ix_container_issues_shipment_created", "container_issues", ["shipment_id", "reported_at"]
    )
    op.create_index(
        "uq_container_issue_open",
        "container_issues",
        ["container_id"],
        unique=True,
        postgresql_where=sa.text("status = 'OPEN'"),
    )
    op.create_table(
        "container_issue_resolution_proposals",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("issue_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("action", sa.String(24), nullable=False),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column("status", sa.String(16), server_default="PENDING", nullable=False),
        sa.Column("proposed_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "proposed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("decided_by", postgresql.UUID(as_uuid=True)),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        sa.Column("decision_reason", sa.String(500)),
        sa.Column("withdrawn_by", postgresql.UUID(as_uuid=True)),
        sa.Column("withdrawn_at", sa.DateTime(timezone=True)),
        sa.Column("withdrawal_reason", sa.String(500)),
        sa.ForeignKeyConstraint(["issue_id"], ["container_issues.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["proposed_by"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["decided_by"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["withdrawn_by"], ["users.id"], ondelete="RESTRICT"),
        sa.CheckConstraint(
            "action IN ('RETRY_DELIVERY', 'CLOSE_UNDELIVERED')",
            name="ck_container_issue_resolution_proposals_action",
        ),
        sa.CheckConstraint(
            "status IN ('PENDING', 'APPROVED', 'REJECTED', 'WITHDRAWN')",
            name="ck_container_issue_resolution_proposals_status",
        ),
        sa.CheckConstraint(
            "(status='PENDING' AND decided_by IS NULL AND decided_at IS NULL AND decision_reason IS NULL AND withdrawn_by IS NULL AND withdrawn_at IS NULL AND withdrawal_reason IS NULL) OR (status IN ('APPROVED','REJECTED') AND decided_by IS NOT NULL AND decided_at IS NOT NULL AND proposed_by <> decided_by AND withdrawn_by IS NULL AND withdrawn_at IS NULL AND withdrawal_reason IS NULL AND (status <> 'REJECTED' OR length(trim(decision_reason)) > 0)) OR (status='WITHDRAWN' AND decided_by IS NULL AND decided_at IS NULL AND decision_reason IS NULL AND withdrawn_by=proposed_by AND withdrawn_at IS NOT NULL AND length(trim(withdrawal_reason)) > 0)",
            name="ck_container_issue_resolution_proposals_transition_fields",
        ),
    )
    op.create_index(
        "ix_issue_proposals_issue_created",
        "container_issue_resolution_proposals",
        ["issue_id", "proposed_at"],
    )
    op.create_index(
        "uq_issue_pending_proposal",
        "container_issue_resolution_proposals",
        ["issue_id"],
        unique=True,
        postgresql_where=sa.text("status = 'PENDING'"),
    )
    op.execute("""
    CREATE FUNCTION guard_container_issue() RETURNS trigger AS $$
    DECLARE c transport_containers%ROWTYPE; s shipments%ROWTYPE;
    BEGIN
      IF TG_OP='DELETE' THEN RAISE EXCEPTION 'Container issues cannot be deleted'; END IF;
      IF TG_OP='INSERT' THEN
        IF NEW.status <> 'OPEN' OR NEW.resolved_by IS NOT NULL OR NEW.resolved_at IS NOT NULL OR NEW.resolution IS NOT NULL THEN RAISE EXCEPTION 'New container issue must be OPEN'; END IF;
        SELECT * INTO s FROM shipments WHERE id=NEW.shipment_id FOR UPDATE;
        SELECT * INTO c FROM transport_containers WHERE id=NEW.container_id FOR UPDATE;
        IF s.id IS NULL OR c.id IS NULL OR c.shipment_id <> s.id OR s.sealed_at IS NULL OR s.status NOT IN ('IN_TRANSIT','DELIVERY_PENDING','DELIVERY_BLOCKED','PARTIALLY_DELIVERED') OR c.delivery_status NOT IN ('IN_TRANSIT','DELIVERY_PENDING','BLOCKED') OR NOT EXISTS (SELECT 1 FROM issue_reason_codes r WHERE r.code=NEW.reason_code AND r.scope='CONTAINER' AND r.kind=NEW.kind AND r.is_active) THEN RAISE EXCEPTION 'Container issue scope, status, or reason is invalid'; END IF;
        RETURN NEW;
      END IF;
      SELECT * INTO s FROM shipments WHERE id=OLD.shipment_id FOR UPDATE;
      SELECT * INTO c FROM transport_containers WHERE id=OLD.container_id FOR UPDATE;
      IF NEW.id IS DISTINCT FROM OLD.id OR NEW.shipment_id IS DISTINCT FROM OLD.shipment_id OR NEW.container_id IS DISTINCT FROM OLD.container_id OR NEW.kind IS DISTINCT FROM OLD.kind OR NEW.reason_code IS DISTINCT FROM OLD.reason_code OR NEW.comment IS DISTINCT FROM OLD.comment OR NEW.reported_by IS DISTINCT FROM OLD.reported_by OR NEW.reported_at IS DISTINCT FROM OLD.reported_at OR OLD.status <> 'OPEN' OR NEW.status NOT IN ('RESOLVED','CLOSED_UNDELIVERED') OR NEW.resolved_by IS NULL OR NEW.resolved_at IS NULL OR NOT EXISTS (SELECT 1 FROM container_issue_resolution_proposals p WHERE p.issue_id=OLD.id AND p.status='APPROVED' AND p.decided_by=NEW.resolved_by AND p.action=NEW.resolution) OR (NEW.resolution='RETRY_DELIVERY' AND NEW.status <> 'RESOLVED') OR (NEW.resolution='CLOSE_UNDELIVERED' AND NEW.status <> 'CLOSED_UNDELIVERED') THEN RAISE EXCEPTION 'Container issue can only be resolved by its approved proposal'; END IF;
      RETURN NEW;
    END; $$ LANGUAGE plpgsql
    """)
    op.execute("""
    CREATE FUNCTION guard_issue_resolution_proposal() RETURNS trigger AS $$
    DECLARE i container_issues%ROWTYPE; s shipments%ROWTYPE;
    BEGIN
      IF TG_OP='DELETE' THEN RAISE EXCEPTION 'Issue proposals cannot be deleted'; END IF;
      SELECT * INTO i FROM container_issues WHERE id=COALESCE(NEW.issue_id, OLD.issue_id);
      SELECT * INTO s FROM shipments WHERE id=i.shipment_id FOR UPDATE;
      IF TG_OP='INSERT' THEN
        IF NEW.status <> 'PENDING' OR NEW.decided_by IS NOT NULL OR NEW.withdrawn_by IS NOT NULL OR i.status <> 'OPEN' OR NOT EXISTS (SELECT 1 FROM transport_containers c WHERE c.id=i.container_id AND c.delivery_status='BLOCKED') OR NOT EXISTS (SELECT 1 FROM memberships m JOIN users u ON u.id=m.user_id JOIN regions r ON r.id=m.region_id WHERE m.user_id=NEW.proposed_by AND m.organization_id IS NULL AND m.region_id=s.region_id AND m.role='REGIONAL_OPERATIONS' AND m.is_active AND u.is_active AND r.is_active) THEN RAISE EXCEPTION 'Only active regional operations can propose resolution for an open blocked issue'; END IF;
        RETURN NEW;
      END IF;
      IF OLD.status <> 'PENDING' OR NEW.id IS DISTINCT FROM OLD.id OR NEW.issue_id IS DISTINCT FROM OLD.issue_id OR NEW.action IS DISTINCT FROM OLD.action OR NEW.reason IS DISTINCT FROM OLD.reason OR NEW.proposed_by IS DISTINCT FROM OLD.proposed_by OR NEW.proposed_at IS DISTINCT FROM OLD.proposed_at THEN RAISE EXCEPTION 'Terminal issue proposal is immutable'; END IF;
      IF NEW.status IN ('APPROVED','REJECTED') THEN
        IF NEW.decided_by IS NULL OR NEW.decided_at IS NULL OR NEW.decided_by=OLD.proposed_by OR NEW.withdrawn_by IS NOT NULL OR i.status <> 'OPEN' OR NOT EXISTS (SELECT 1 FROM memberships m JOIN users u ON u.id=m.user_id JOIN organizations o ON o.id=m.organization_id JOIN regions r ON r.id=o.region_id WHERE m.user_id=NEW.decided_by AND m.organization_id=s.destination_organization_id AND m.role='LAB_MANAGER' AND m.is_active AND u.is_active AND o.is_active AND r.is_active) THEN RAISE EXCEPTION 'Issue resolution requires a distinct active destination Lab Manager'; END IF;
      ELSIF NEW.status='WITHDRAWN' THEN
        IF NEW.withdrawn_by IS DISTINCT FROM OLD.proposed_by OR NEW.withdrawn_at IS NULL OR length(trim(COALESCE(NEW.withdrawal_reason,'')))=0 OR NEW.decided_by IS NOT NULL OR NOT EXISTS (SELECT 1 FROM memberships m JOIN users u ON u.id=m.user_id JOIN regions r ON r.id=m.region_id WHERE m.user_id=NEW.withdrawn_by AND m.organization_id IS NULL AND m.region_id=s.region_id AND m.role='REGIONAL_OPERATIONS' AND m.is_active AND u.is_active AND r.is_active) THEN RAISE EXCEPTION 'Only the active regional proposer may withdraw with a reason'; END IF;
      ELSE RAISE EXCEPTION 'Invalid issue proposal transition'; END IF;
      RETURN NEW;
    END; $$ LANGUAGE plpgsql
    """)
    op.execute(
        "CREATE TRIGGER trg_guard_container_issue BEFORE INSERT OR UPDATE OR DELETE ON container_issues FOR EACH ROW EXECUTE FUNCTION guard_container_issue()"
    )
    op.execute(
        "CREATE TRIGGER trg_guard_issue_proposal BEFORE INSERT OR UPDATE OR DELETE ON container_issue_resolution_proposals FOR EACH ROW EXECUTE FUNCTION guard_issue_resolution_proposal()"
    )
    op.execute("""
    CREATE FUNCTION validate_issue_container_consistency() RETURNS trigger AS $$
    DECLARE cid uuid; shipment_key uuid; c_status text; i_status text;
    BEGIN
      IF TG_TABLE_NAME='transport_containers' THEN cid := COALESCE(NEW.id, OLD.id);
      ELSE cid := COALESCE(NEW.container_id, OLD.container_id); END IF;
      SELECT delivery_status, shipment_id INTO c_status, shipment_key FROM transport_containers WHERE id=cid;
      IF c_status IS NULL THEN RETURN NULL; END IF;
      SELECT status INTO i_status FROM container_issues WHERE container_id=cid AND status='OPEN';
      IF (c_status='BLOCKED') <> (i_status='OPEN') THEN RAISE EXCEPTION 'BLOCKED container must have exactly one OPEN issue'; END IF;
      RETURN NULL;
    END; $$ LANGUAGE plpgsql
    """)
    op.execute(
        "CREATE CONSTRAINT TRIGGER trg_validate_issue_container AFTER INSERT OR UPDATE ON container_issues DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION validate_issue_container_consistency()"
    )
    op.execute(
        "CREATE CONSTRAINT TRIGGER trg_validate_issue_container_state AFTER UPDATE ON transport_containers DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION validate_issue_container_consistency()"
    )
    op.execute("""
    CREATE OR REPLACE FUNCTION guard_delivery_handoff() RETURNS trigger AS $$
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
        IF NEW.attempt_no <> COALESCE((SELECT max(attempt_no)+1 FROM handoffs WHERE container_id=c.id AND kind='DELIVERY'),1) OR
           EXISTS (SELECT 1 FROM handoffs WHERE container_id=c.id AND kind='DELIVERY' AND status IN ('PENDING','CONFIRMED')) THEN
          RAISE EXCEPTION 'Container already has an active Delivery handoff or invalid attempt number';
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
        IF c.delivery_status <> 'DELIVERY_PENDING' OR s.status NOT IN ('DELIVERY_PENDING','PARTIALLY_DELIVERED') OR
           NEW.confirmed_by IS NULL OR NEW.confirmed_at IS NULL OR NEW.confirmed_by=OLD.initiated_by OR
           NEW.withdrawn_by IS NOT NULL OR NEW.withdrawn_at IS NOT NULL OR NEW.withdrawal_reason IS NOT NULL OR
           NOT EXISTS (SELECT 1 FROM memberships m JOIN users u ON u.id=m.user_id
             JOIN organizations o ON o.id=m.organization_id JOIN regions r ON r.id=o.region_id
             WHERE m.user_id=NEW.confirmed_by AND m.organization_id=s.destination_organization_id
               AND m.role IN ('LAB_OPERATOR','LAB_MANAGER') AND m.is_active AND u.is_active AND o.is_active AND r.is_active) THEN
          RAISE EXCEPTION 'Delivery receipt requires a different active destination Lab operator';
        END IF;
      ELSIF NEW.status='WITHDRAWN' THEN
        IF c.delivery_status <> 'DELIVERY_PENDING' OR s.status NOT IN ('DELIVERY_PENDING','PARTIALLY_DELIVERED') OR
           NEW.withdrawn_by IS NULL OR NEW.withdrawn_at IS NULL OR length(trim(COALESCE(NEW.withdrawal_reason,'')))=0 OR
           NEW.confirmed_by IS NOT NULL OR NEW.confirmed_at IS NOT NULL OR
           NOT (NEW.withdrawn_by=OLD.initiated_by OR EXISTS (
             SELECT 1 FROM container_issues i JOIN memberships m ON m.user_id=NEW.withdrawn_by
               JOIN users u ON u.id=m.user_id JOIN organizations o ON o.id=m.organization_id
               JOIN regions r ON r.id=o.region_id
             WHERE i.container_id=c.id AND i.shipment_id=s.id AND i.status='OPEN' AND i.reported_by=NEW.withdrawn_by
               AND m.organization_id=s.destination_organization_id AND m.role='LAB_OPERATOR'
               AND m.is_active AND u.is_active AND o.is_active AND r.is_active)) THEN
          RAISE EXCEPTION 'Only the initiating Courier or reporting destination Lab Operator can withdraw Delivery';
        END IF;
      ELSIF NEW.status='SUPERSEDED' THEN
        IF c.delivery_status <> 'DELIVERY_PENDING' OR NEW.superseded_at IS NULL OR
           NEW.superseded_by_incident_id IS NULL OR NEW.confirmed_by IS NOT NULL OR
           NEW.confirmed_at IS NOT NULL OR NEW.withdrawn_by IS NOT NULL OR NEW.withdrawn_at IS NOT NULL OR
           NOT EXISTS (SELECT 1 FROM custody_incidents i WHERE i.id=NEW.superseded_by_incident_id
             AND i.handoff_id=OLD.id AND i.shipment_id=s.id AND i.container_id=c.id
             AND i.phase='DELIVERY' AND i.status='RESOLVED'
             AND i.resolution='EXCEPTIONAL_RECEIPT_VERIFIED') THEN
          RAISE EXCEPTION 'Only a resolved exceptional receipt may supersede a pending Delivery';
        END IF;
      ELSE
        RAISE EXCEPTION 'Invalid Delivery handoff transition';
      END IF;
      RETURN NEW;
    END; $$ LANGUAGE plpgsql
    """)
    op.execute("""
    CREATE OR REPLACE FUNCTION guard_delivery_container() RETURNS trigger AS $$
    DECLARE shipment_key uuid;
    BEGIN
      IF NEW.shipment_id IS DISTINCT FROM OLD.shipment_id OR NEW.code IS DISTINCT FROM OLD.code THEN
        RAISE EXCEPTION 'Container identity is immutable';
      END IF;
      IF NEW.delivery_status IS DISTINCT FROM OLD.delivery_status THEN
        SELECT id INTO shipment_key FROM shipments WHERE id=NEW.shipment_id FOR UPDATE;
        IF NEW.state_version <> OLD.state_version + 1 THEN RAISE EXCEPTION 'Container state version must increment'; END IF;
        IF OLD.delivery_status='PREPARED' AND NEW.delivery_status='IN_TRANSIT' THEN
          IF NOT EXISTS (SELECT 1 FROM handoffs WHERE shipment_id=shipment_key AND kind='PICKUP' AND status='CONFIRMED') THEN
            RAISE EXCEPTION 'confirmed Pickup is required before transit';
          END IF;
        ELSIF OLD.delivery_status='IN_TRANSIT' AND NEW.delivery_status='DELIVERY_PENDING' THEN
          IF NOT EXISTS (SELECT 1 FROM handoffs WHERE shipment_id=shipment_key AND container_id=NEW.id AND kind='DELIVERY' AND status='PENDING') THEN
            RAISE EXCEPTION 'Pending Delivery handoff is required for container';
          END IF;
        ELSIF OLD.delivery_status='DELIVERY_PENDING' AND NEW.delivery_status='IN_TRANSIT' THEN
          IF NOT EXISTS (SELECT 1 FROM handoffs WHERE shipment_id=shipment_key AND container_id=NEW.id AND kind='DELIVERY' AND status='WITHDRAWN') THEN
            RAISE EXCEPTION 'Withdrawn Delivery handoff is required to return to transit';
          END IF;
        ELSIF OLD.delivery_status IN ('IN_TRANSIT','DELIVERY_PENDING') AND NEW.delivery_status='BLOCKED' THEN
          IF NOT EXISTS (SELECT 1 FROM container_issues i WHERE i.container_id=NEW.id AND i.shipment_id=shipment_key AND i.status='OPEN') OR
             EXISTS (SELECT 1 FROM handoffs h WHERE h.container_id=NEW.id AND h.kind='DELIVERY' AND h.status IN ('PENDING','CONFIRMED')) THEN
            RAISE EXCEPTION 'Open issue and inactive Delivery handoff are required to block container';
          END IF;
        ELSIF OLD.delivery_status='BLOCKED' AND NEW.delivery_status IN ('IN_TRANSIT','UNDELIVERED_CLOSED') THEN
          IF NOT EXISTS (SELECT 1 FROM container_issues i WHERE i.container_id=NEW.id AND i.shipment_id=shipment_key
              AND i.status IN ('RESOLVED','CLOSED_UNDELIVERED')) THEN
            RAISE EXCEPTION 'Approved issue resolution is required to unblock or close container';
          END IF;
        ELSIF OLD.delivery_status IN ('IN_TRANSIT','DELIVERY_PENDING') AND NEW.delivery_status='RECEIVED' THEN
          IF NEW.received_at IS NULL OR NOT (
            (NEW.received_by IS NOT NULL AND NEW.receipt_evidence->>'method'='NORMAL' AND
             EXISTS (SELECT 1 FROM handoffs h WHERE h.shipment_id=shipment_key AND h.container_id=NEW.id
               AND h.kind='DELIVERY' AND h.status='CONFIRMED' AND h.confirmed_by=NEW.received_by) AND
             EXISTS (SELECT 1 FROM container_receipt_evidence e WHERE e.container_id=NEW.id
               AND e.method='NORMAL' AND e.confirmed_delivery_handoff_id IN
                 (SELECT h.id FROM handoffs h WHERE h.container_id=NEW.id AND h.status='CONFIRMED')))
            OR
            (NEW.received_by IS NULL AND NEW.receipt_evidence->>'method'='EXCEPTIONAL' AND
             EXISTS (SELECT 1 FROM container_receipt_evidence e JOIN custody_incidents i
               ON i.id=e.resolved_incident_id WHERE e.container_id=NEW.id AND e.method='EXCEPTIONAL'
               AND i.status='RESOLVED' AND i.resolution='EXCEPTIONAL_RECEIPT_VERIFIED'
               AND i.shipment_id=shipment_key AND i.container_id=NEW.id))) THEN
            RAISE EXCEPTION 'Container receipt requires verified normal or exceptional evidence';
          END IF;
        ELSE
          RAISE EXCEPTION 'Invalid container custody transition';
        END IF;
      ELSIF OLD.delivery_status='RECEIVED' AND (NEW.received_at IS DISTINCT FROM OLD.received_at OR
         NEW.received_by IS DISTINCT FROM OLD.received_by OR NEW.receipt_evidence IS DISTINCT FROM OLD.receipt_evidence) THEN
        RAISE EXCEPTION 'Confirmed container receipt is immutable';
      ELSIF OLD.delivery_status <> 'RECEIVED' AND (NEW.received_at IS DISTINCT FROM OLD.received_at OR
         NEW.received_by IS DISTINCT FROM OLD.received_by OR NEW.receipt_evidence IS DISTINCT FROM OLD.receipt_evidence) THEN
        RAISE EXCEPTION 'Receipt evidence can only be written with container receipt';
      END IF;
      RETURN NEW;
    END; $$ LANGUAGE plpgsql
    """)

    op.create_table(
        "custody_incidents",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "shipment_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("shipments.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("container_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "handoff_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("handoffs.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("phase", sa.String(16), nullable=False),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("status", sa.String(24), server_default="OPEN", nullable=False),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column(
            "opened_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "opened_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "resolved_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolution", sa.String(40), nullable=True),
        sa.ForeignKeyConstraint(
            ["shipment_id", "container_id"],
            ["transport_containers.shipment_id", "transport_containers.id"],
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("phase IN ('PICKUP','DELIVERY')", name="ck_custody_incidents_phase"),
        sa.CheckConstraint(
            "kind IN ('COURIER_UNAVAILABLE_AFTER_PICKUP','PICKUP_UNCONFIRMED',"
            "'COURIER_DELIVERY_CONFIRMATION_UNAVAILABLE','HANDOFF_STALLED','OFFLINE_CONFLICT')",
            name="ck_custody_incidents_kind",
        ),
        sa.CheckConstraint(
            "status IN ('OPEN','PENDING_EVIDENCE','UNDER_REVIEW','RESOLVED','REJECTED')",
            name="ck_custody_incidents_status",
        ),
        sa.CheckConstraint(
            "(phase='PICKUP' AND container_id IS NULL) OR "
            "(phase='DELIVERY' AND container_id IS NOT NULL)",
            name="ck_custody_incidents_phase_container_scope",
        ),
        sa.CheckConstraint(
            "(status IN ('OPEN','PENDING_EVIDENCE','UNDER_REVIEW') AND resolved_by IS NULL "
            "AND resolved_at IS NULL AND resolution IS NULL) OR "
            "(status IN ('RESOLVED','REJECTED') AND resolved_by IS NOT NULL "
            "AND resolved_at IS NOT NULL AND resolution IS NOT NULL)",
            name="ck_custody_incidents_resolution_fields",
        ),
    )
    op.create_index(
        "ix_custody_incidents_shipment_opened", "custody_incidents", ["shipment_id", "opened_at"]
    )
    op.create_table(
        "custody_attestations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "incident_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("custody_incidents.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("role_at_time", sa.String(32), nullable=False),
        sa.Column("container_code", sa.String(40), nullable=True),
        sa.Column("statement", sa.String(500), nullable=False),
        sa.Column(
            "recorded_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("physically_present", sa.Boolean(), nullable=True),
        sa.Column("physical_release_occurred", sa.Boolean(), nullable=True),
        sa.Column("containers_at_origin", sa.Boolean(), nullable=True),
        sa.UniqueConstraint("incident_id", "user_id", name="uq_custody_attestation_actor"),
        sa.CheckConstraint(
            "length(trim(statement)) > 0", name="ck_custody_attestations_statement_nonblank"
        ),
        sa.CheckConstraint(
            "(role_at_time='SENDER_OPERATOR' AND physically_present IS NULL AND container_code IS NULL) OR "
            "(role_at_time IN ('LAB_OPERATOR','LAB_MANAGER') AND physically_present IS TRUE AND container_code IS NOT NULL)",
            name="ck_custody_attestations_role_assertion_fields",
        ),
    )
    op.create_table(
        "container_receipt_evidence",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "container_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("transport_containers.id", ondelete="RESTRICT"),
            nullable=False,
            unique=True,
        ),
        sa.Column("method", sa.String(16), nullable=False),
        sa.Column(
            "confirmed_delivery_handoff_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("handoffs.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "resolved_incident_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("custody_incidents.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "verified_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "verified_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "method IN ('NORMAL','EXCEPTIONAL')", name="ck_container_receipt_evidence_method"
        ),
        sa.CheckConstraint(
            "(method='NORMAL' AND confirmed_delivery_handoff_id IS NOT NULL AND resolved_incident_id IS NULL) OR "
            "(method='EXCEPTIONAL' AND confirmed_delivery_handoff_id IS NULL AND resolved_incident_id IS NOT NULL)",
            name="ck_container_receipt_evidence_method_reference",
        ),
    )
    op.execute("""
    CREATE FUNCTION guard_custody_incident() RETURNS trigger AS $$
    DECLARE s shipments%ROWTYPE; c transport_containers%ROWTYPE;
      witness_count integer; has_manager boolean;
    BEGIN
      IF TG_OP='DELETE' THEN RAISE EXCEPTION 'Custody incident history is immutable'; END IF;
      IF TG_OP='INSERT' THEN
        SELECT * INTO s FROM shipments WHERE id=NEW.shipment_id;
        IF s.id IS NULL OR NEW.status <> 'OPEN' OR length(trim(NEW.reason))=0 OR
          NOT EXISTS (SELECT 1 FROM memberships m JOIN users u ON u.id=m.user_id
            JOIN regions r ON r.id=m.region_id WHERE m.user_id=NEW.opened_by AND m.organization_id IS NULL
            AND m.region_id=s.region_id AND m.role='REGIONAL_OPERATIONS' AND m.is_active AND u.is_active AND r.is_active) THEN
          RAISE EXCEPTION 'Custody incident requires an active regional operator and a valid Shipment';
        END IF;
        IF NEW.phase='PICKUP' THEN
          IF NEW.container_id IS NOT NULL OR NEW.kind NOT IN ('PICKUP_UNCONFIRMED','COURIER_UNAVAILABLE_AFTER_PICKUP','HANDOFF_STALLED','OFFLINE_CONFLICT') THEN
            RAISE EXCEPTION 'Invalid Pickup incident phase, kind, or container';
          END IF;
        ELSE
          SELECT * INTO c FROM transport_containers WHERE id=NEW.container_id AND shipment_id=s.id;
          IF c.id IS NULL OR NEW.kind NOT IN ('COURIER_UNAVAILABLE_AFTER_PICKUP','COURIER_DELIVERY_CONFIRMATION_UNAVAILABLE','HANDOFF_STALLED','OFFLINE_CONFLICT') OR
             c.delivery_status NOT IN ('IN_TRANSIT','DELIVERY_PENDING') THEN
            RAISE EXCEPTION 'Invalid Delivery incident scope, kind, or container state';
          END IF;
        END IF;
        IF NEW.handoff_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM handoffs h WHERE h.id=NEW.handoff_id
          AND h.shipment_id=s.id AND ((NEW.phase='PICKUP' AND h.kind='PICKUP') OR (NEW.phase='DELIVERY' AND h.kind='DELIVERY' AND h.container_id=NEW.container_id))) THEN
          RAISE EXCEPTION 'Incident handoff does not match its phase and scope';
        END IF;
        RETURN NEW;
      END IF;
      IF NEW.id IS DISTINCT FROM OLD.id OR NEW.shipment_id IS DISTINCT FROM OLD.shipment_id OR
         NEW.container_id IS DISTINCT FROM OLD.container_id OR NEW.handoff_id IS DISTINCT FROM OLD.handoff_id OR
         NEW.phase IS DISTINCT FROM OLD.phase OR NEW.kind IS DISTINCT FROM OLD.kind OR
         NEW.reason IS DISTINCT FROM OLD.reason OR NEW.opened_by IS DISTINCT FROM OLD.opened_by OR
         NEW.opened_at IS DISTINCT FROM OLD.opened_at OR OLD.status NOT IN ('OPEN','PENDING_EVIDENCE','UNDER_REVIEW') OR
         NEW.status NOT IN ('PENDING_EVIDENCE','UNDER_REVIEW','RESOLVED','REJECTED') THEN
        RAISE EXCEPTION 'Custody incident identity and resolution history are immutable';
      END IF;
      IF NEW.status IN ('RESOLVED','REJECTED') THEN
        IF NEW.resolved_by IS NULL OR NEW.resolved_at IS NULL OR length(trim(COALESCE(NEW.resolution,'')))=0 OR
          NOT EXISTS (SELECT 1 FROM shipments sh JOIN memberships m ON m.user_id=NEW.resolved_by
            JOIN users u ON u.id=m.user_id JOIN regions r ON r.id=m.region_id WHERE sh.id=NEW.shipment_id
            AND m.organization_id IS NULL AND m.region_id=sh.region_id AND m.role='REGIONAL_OPERATIONS'
            AND m.is_active AND u.is_active AND r.is_active) THEN
          RAISE EXCEPTION 'Incident resolution requires current regional authority and complete decision';
        END IF;
      END IF;
      IF NEW.status='RESOLVED' THEN
        SELECT * INTO s FROM shipments WHERE id=NEW.shipment_id;
        IF NEW.resolution NOT IN ('EXCEPTIONAL_RECEIPT_VERIFIED','NO_PHYSICAL_TRANSFER',
                                  'ESCALATED_NO_CUSTODY_CHANGE','CONTAINER_ISSUE_REFERRED') THEN
          RAISE EXCEPTION 'Invalid custody incident resolution';
        ELSIF NEW.resolution='EXCEPTIONAL_RECEIPT_VERIFIED' THEN
          SELECT * INTO c FROM transport_containers WHERE id=NEW.container_id AND shipment_id=NEW.shipment_id;
          IF NEW.phase <> 'DELIVERY' OR NEW.kind NOT IN
               ('COURIER_UNAVAILABLE_AFTER_PICKUP','COURIER_DELIVERY_CONFIRMATION_UNAVAILABLE') OR
             s.sealed_at IS NULL OR c.id IS NULL OR c.delivery_status NOT IN ('IN_TRANSIT','DELIVERY_PENDING') OR
             EXISTS (SELECT 1 FROM container_receipt_evidence e WHERE e.container_id=c.id) OR
             NOT EXISTS (SELECT 1 FROM handoffs p WHERE p.shipment_id=s.id AND p.kind='PICKUP'
               AND p.container_id IS NULL AND p.status='CONFIRMED') THEN
            RAISE EXCEPTION 'Exceptional receipt requires an eligible container and confirmed whole-Shipment Pickup';
          END IF;
          SELECT count(DISTINCT a.user_id), coalesce(bool_or(a.role_at_time='LAB_MANAGER'),false)
            INTO witness_count, has_manager
          FROM custody_attestations a
          JOIN memberships m ON m.user_id=a.user_id
          JOIN users u ON u.id=a.user_id
          JOIN organizations o ON o.id=m.organization_id
          JOIN regions r ON r.id=o.region_id
          WHERE a.incident_id=NEW.id AND a.physically_present IS TRUE
            AND a.organization_id=s.destination_organization_id AND a.container_code=c.code
            AND m.organization_id=s.destination_organization_id AND m.role=a.role_at_time
            AND m.role IN ('LAB_OPERATOR','LAB_MANAGER') AND m.is_active
            AND u.is_active AND o.is_active AND r.is_active;
          IF witness_count < 2 OR NOT has_manager OR
             EXISTS (SELECT 1 FROM custody_attestations a WHERE a.incident_id=NEW.id AND a.user_id=NEW.resolved_by) THEN
            RAISE EXCEPTION 'Exceptional receipt requires two current Lab witnesses, a Lab Manager, and a separate resolver';
          END IF;
        ELSIF NEW.resolution='NO_PHYSICAL_TRANSFER' THEN
          IF NEW.phase <> 'PICKUP' OR s.status NOT IN ('READY_FOR_PICKUP','PICKUP_PENDING') OR
             EXISTS (SELECT 1 FROM handoffs p WHERE p.shipment_id=s.id AND p.kind='PICKUP'
               AND p.container_id IS NULL AND p.status='CONFIRMED') OR NOT EXISTS (
            SELECT 1 FROM custody_attestations a
            JOIN memberships m ON m.user_id=a.user_id
            JOIN users u ON u.id=a.user_id
            JOIN organizations o ON o.id=m.organization_id
            JOIN regions r ON r.id=o.region_id
            WHERE a.incident_id=NEW.id AND a.organization_id=s.sender_organization_id
              AND a.role_at_time='SENDER_OPERATOR' AND a.physical_release_occurred IS FALSE
              AND a.containers_at_origin IS TRUE AND m.organization_id=s.sender_organization_id
              AND m.role='SENDER_OPERATOR' AND m.is_active AND u.is_active AND o.is_active AND r.is_active
          ) THEN
            RAISE EXCEPTION 'NO_PHYSICAL_TRANSFER requires current origin Sender evidence';
          END IF;
        ELSIF NEW.resolution='CONTAINER_ISSUE_REFERRED' THEN
          SELECT * INTO c FROM transport_containers WHERE id=NEW.container_id AND shipment_id=NEW.shipment_id;
          IF NEW.phase <> 'DELIVERY' OR c.id IS NULL OR c.delivery_status NOT IN ('IN_TRANSIT','DELIVERY_PENDING') OR
             NOT EXISTS (
               SELECT 1 FROM custody_attestations a
               JOIN memberships m ON m.user_id=a.user_id
               JOIN users u ON u.id=a.user_id
               JOIN organizations o ON o.id=m.organization_id
               JOIN regions r ON r.id=o.region_id
               WHERE a.incident_id=NEW.id AND a.role_at_time='LAB_MANAGER'
                 AND a.organization_id=s.destination_organization_id AND a.container_code=c.code
                 AND a.physically_present IS TRUE AND m.organization_id=s.destination_organization_id
                 AND m.role='LAB_MANAGER' AND m.is_active AND u.is_active AND o.is_active AND r.is_active
             ) THEN
            RAISE EXCEPTION 'Container issue referral requires a current destination Lab Manager attestation';
          END IF;
        END IF;
      END IF;
      RETURN NEW;
    END; $$ LANGUAGE plpgsql
    """)
    op.execute("""
    CREATE TRIGGER trg_guard_custody_incident BEFORE INSERT OR UPDATE OR DELETE ON custody_incidents
    FOR EACH ROW EXECUTE FUNCTION guard_custody_incident()
    """)
    op.execute("""
    CREATE FUNCTION guard_custody_attestation() RETURNS trigger AS $$
    DECLARE ci custody_incidents%ROWTYPE; s shipments%ROWTYPE; c transport_containers%ROWTYPE;
    BEGIN
      IF TG_OP <> 'INSERT' THEN RAISE EXCEPTION 'Custody attestations are append-only'; END IF;
      SELECT * INTO ci FROM custody_incidents WHERE id=NEW.incident_id;
      SELECT * INTO s FROM shipments WHERE id=ci.shipment_id;
      IF ci.id IS NULL OR ci.status NOT IN ('OPEN','PENDING_EVIDENCE','UNDER_REVIEW') OR length(trim(NEW.statement))=0 THEN
        RAISE EXCEPTION 'Attestation requires an active incident and nonblank statement';
      END IF;
      IF ci.phase='PICKUP' THEN
        IF NEW.role_at_time <> 'SENDER_OPERATOR' OR NEW.container_code IS NOT NULL OR NEW.physically_present IS NOT NULL OR
          NEW.physical_release_occurred IS NULL OR NEW.containers_at_origin IS NULL OR
          NOT EXISTS (SELECT 1 FROM memberships m JOIN users u ON u.id=m.user_id JOIN organizations o ON o.id=m.organization_id
            JOIN regions r ON r.id=o.region_id WHERE m.user_id=NEW.user_id AND m.organization_id=s.sender_organization_id
            AND NEW.organization_id=s.sender_organization_id AND m.role='SENDER_OPERATOR' AND m.is_active AND u.is_active AND o.is_active AND r.is_active) THEN
          RAISE EXCEPTION 'Pickup attestation requires current origin Sender authority and fields';
        END IF;
      ELSE
        SELECT * INTO c FROM transport_containers WHERE id=ci.container_id AND shipment_id=s.id;
        IF NEW.role_at_time NOT IN ('LAB_OPERATOR','LAB_MANAGER') OR NEW.physically_present IS NOT TRUE OR
          NEW.container_code IS DISTINCT FROM c.code OR NEW.physical_release_occurred IS NOT NULL OR NEW.containers_at_origin IS NOT NULL OR
          NOT EXISTS (SELECT 1 FROM memberships m JOIN users u ON u.id=m.user_id JOIN organizations o ON o.id=m.organization_id
            JOIN regions r ON r.id=o.region_id WHERE m.user_id=NEW.user_id AND m.organization_id=s.destination_organization_id
            AND NEW.organization_id=s.destination_organization_id AND m.role=NEW.role_at_time AND m.is_active AND u.is_active AND o.is_active AND r.is_active) THEN
          RAISE EXCEPTION 'Delivery attestation requires current destination Lab authority and matching container';
        END IF;
      END IF;
      RETURN NEW;
    END; $$ LANGUAGE plpgsql
    """)
    op.execute("""
    CREATE TRIGGER trg_guard_custody_attestation BEFORE INSERT OR UPDATE OR DELETE ON custody_attestations
    FOR EACH ROW EXECUTE FUNCTION guard_custody_attestation()
    """)
    op.execute("""
    CREATE FUNCTION guard_receipt_evidence() RETURNS trigger AS $$
    DECLARE c transport_containers%ROWTYPE; h handoffs%ROWTYPE; ci custody_incidents%ROWTYPE;
      s shipments%ROWTYPE; witness_count integer; has_manager boolean;
    BEGIN
      IF TG_OP <> 'INSERT' THEN RAISE EXCEPTION 'Receipt evidence is immutable'; END IF;
      SELECT * INTO c FROM transport_containers WHERE id=NEW.container_id;
      IF NEW.method='NORMAL' THEN
        SELECT * INTO h FROM handoffs WHERE id=NEW.confirmed_delivery_handoff_id;
        IF h.id IS NULL OR h.kind <> 'DELIVERY' OR h.container_id <> c.id OR h.status <> 'CONFIRMED' OR NEW.verified_by <> h.confirmed_by THEN
          RAISE EXCEPTION 'Normal receipt evidence requires the confirmed Delivery handoff';
        END IF;
      ELSE
        SELECT * INTO ci FROM custody_incidents WHERE id=NEW.resolved_incident_id;
        SELECT * INTO s FROM shipments WHERE id=ci.shipment_id;
        SELECT count(DISTINCT a.user_id), coalesce(bool_or(a.role_at_time='LAB_MANAGER'),false)
          INTO witness_count, has_manager
        FROM custody_attestations a
        JOIN memberships m ON m.user_id=a.user_id
        JOIN users u ON u.id=a.user_id
        JOIN organizations o ON o.id=m.organization_id
        JOIN regions r ON r.id=o.region_id
        WHERE a.incident_id=ci.id AND a.physically_present IS TRUE
          AND a.organization_id=s.destination_organization_id AND a.container_code=c.code
          AND m.organization_id=s.destination_organization_id AND m.role=a.role_at_time
          AND m.role IN ('LAB_OPERATOR','LAB_MANAGER') AND m.is_active
          AND u.is_active AND o.is_active AND r.is_active;
        IF ci.id IS NULL OR ci.status <> 'RESOLVED' OR ci.resolution <> 'EXCEPTIONAL_RECEIPT_VERIFIED' OR
          ci.phase <> 'DELIVERY' OR ci.kind NOT IN
            ('COURIER_UNAVAILABLE_AFTER_PICKUP','COURIER_DELIVERY_CONFIRMATION_UNAVAILABLE') OR ci.container_id <> c.id OR
          s.sealed_at IS NULL OR c.delivery_status NOT IN ('IN_TRANSIT','DELIVERY_PENDING') OR
          NEW.verified_by <> ci.resolved_by OR
          NOT EXISTS (SELECT 1 FROM handoffs p WHERE p.shipment_id=ci.shipment_id AND p.kind='PICKUP'
            AND p.container_id IS NULL AND p.status='CONFIRMED') OR
          witness_count < 2 OR NOT has_manager OR
          EXISTS (SELECT 1 FROM custody_attestations a WHERE a.incident_id=ci.id AND a.user_id=NEW.verified_by) THEN
          RAISE EXCEPTION 'Exceptional receipt evidence requires confirmed Pickup, two current Lab witnesses, a Lab Manager, and separate regional resolver';
        END IF;
      END IF;
      RETURN NEW;
    END; $$ LANGUAGE plpgsql
    """)
    op.execute("""
    CREATE TRIGGER trg_guard_receipt_evidence BEFORE INSERT OR UPDATE OR DELETE ON container_receipt_evidence
    FOR EACH ROW EXECUTE FUNCTION guard_receipt_evidence()
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

    app_role = op.get_bind().dialect.identifier_preparer.quote(get_settings().app_postgres_user)
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE container_issues, "
        "container_issue_resolution_proposals, custody_incidents, custody_attestations, "
        f"container_receipt_evidence TO {app_role}"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_guard_receipt_evidence ON container_receipt_evidence")
    op.execute("DROP FUNCTION IF EXISTS guard_receipt_evidence()")
    op.execute("DROP TRIGGER IF EXISTS trg_guard_custody_attestation ON custody_attestations")
    op.execute("DROP FUNCTION IF EXISTS guard_custody_attestation()")
    op.execute("DROP TRIGGER IF EXISTS trg_guard_custody_incident ON custody_incidents")
    op.execute("DROP FUNCTION IF EXISTS guard_custody_incident()")
    op.drop_table("container_receipt_evidence")
    op.drop_table("custody_attestations")
    op.drop_index("ix_custody_incidents_shipment_opened", table_name="custody_incidents")
    op.drop_table("custody_incidents")
    op.execute("""
    CREATE OR REPLACE FUNCTION validate_delivery_state_consistency() RETURNS trigger AS $$
    DECLARE shipment_key uuid; shipment_state text; pending_pickup integer; confirmed_pickup integer;
      container_count integer; prepared_count integer; transit_count integer; delivery_pending_count integer;
      received_count integer; blocked_count integer; undelivered_count integer; open_sessions integer;
    BEGIN
      IF TG_TABLE_NAME='shipments' THEN shipment_key := COALESCE(NEW.id, OLD.id);
      ELSE shipment_key := COALESCE(NEW.shipment_id, OLD.shipment_id); END IF;
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
      IF shipment_state='PICKUP_PENDING' AND pending_pickup <> 1 THEN RAISE EXCEPTION 'Pending Pickup must match PICKUP_PENDING shipment state';
      ELSIF shipment_state IN ('DRAFT','READY_FOR_PICKUP','CANCELLED') AND (pending_pickup > 0 OR confirmed_pickup > 0) THEN RAISE EXCEPTION 'Active Pickup handoff must match shipment state';
      ELSIF shipment_state IN ('IN_TRANSIT','DELIVERY_PENDING','DELIVERY_BLOCKED','PARTIALLY_DELIVERED','RECEIVING','COMPLETED','COMPLETED_WITH_ISSUES','CLOSED_UNDELIVERED') AND confirmed_pickup <> 1 THEN RAISE EXCEPTION 'Delivery state requires one confirmed whole-Shipment Pickup';
      END IF;
      IF shipment_state IN ('DRAFT','READY_FOR_PICKUP','PICKUP_PENDING','CANCELLED') THEN
        IF EXISTS (SELECT 1 FROM transport_containers WHERE shipment_id=shipment_key AND delivery_status <> 'PREPARED') OR EXISTS (SELECT 1 FROM handoffs WHERE shipment_id=shipment_key AND kind='DELIVERY') THEN RAISE EXCEPTION 'Pre-Pickup shipment cannot contain Delivery custody'; END IF;
      ELSE
        IF container_count=0 OR prepared_count>0 THEN RAISE EXCEPTION 'Picked-up Shipment requires only initialized containers'; END IF;
        IF EXISTS (SELECT 1 FROM transport_containers c WHERE c.shipment_id=shipment_key AND
          ((c.delivery_status='IN_TRANSIT' AND EXISTS (SELECT 1 FROM handoffs h WHERE h.container_id=c.id AND h.kind='DELIVERY' AND h.status IN ('PENDING','CONFIRMED'))) OR
           (c.delivery_status='DELIVERY_PENDING' AND 1 <> (SELECT count(*) FROM handoffs h WHERE h.container_id=c.id AND h.kind='DELIVERY' AND h.status='PENDING')) OR
           (c.delivery_status='RECEIVED' AND 1 <> (SELECT count(*) FROM handoffs h WHERE h.container_id=c.id AND h.kind='DELIVERY' AND h.status='CONFIRMED' AND h.confirmed_by=c.received_by)) OR
           (c.delivery_status IN ('BLOCKED','UNDELIVERED_CLOSED') AND EXISTS (SELECT 1 FROM handoffs h WHERE h.container_id=c.id AND h.kind='DELIVERY' AND h.status IN ('PENDING','CONFIRMED'))))) THEN RAISE EXCEPTION 'Container state must match its Delivery handoff'; END IF;
        IF received_count=0 AND open_sessions <> 0 OR received_count>0 AND open_sessions <> 1 THEN RAISE EXCEPTION 'An OPEN receiving session must match received containers'; END IF;
        IF shipment_state='IN_TRANSIT' AND (transit_count <> container_count OR delivery_pending_count>0 OR blocked_count>0 OR received_count>0) THEN RAISE EXCEPTION 'IN_TRANSIT shipment must have only transit containers';
        ELSIF shipment_state='DELIVERY_PENDING' AND (received_count>0 OR blocked_count>0 OR delivery_pending_count=0) THEN RAISE EXCEPTION 'DELIVERY_PENDING shipment must have a pending Delivery and no receipt or block';
        ELSIF shipment_state='DELIVERY_BLOCKED' AND (received_count>0 OR blocked_count=0) THEN RAISE EXCEPTION 'DELIVERY_BLOCKED shipment must have blocked containers and no receipt';
        ELSIF shipment_state='PARTIALLY_DELIVERED' AND (received_count=0 OR transit_count+delivery_pending_count+blocked_count+undelivered_count=0) THEN RAISE EXCEPTION 'PARTIALLY_DELIVERED shipment must have received and outstanding containers';
        ELSIF shipment_state='RECEIVING' AND (received_count=0 OR transit_count+delivery_pending_count+blocked_count>0) THEN RAISE EXCEPTION 'RECEIVING shipment must have receipts and no unresolved containers';
        ELSIF shipment_state='CLOSED_UNDELIVERED' AND undelivered_count <> container_count THEN RAISE EXCEPTION 'CLOSED_UNDELIVERED requires every container dispositioned undelivered';
        END IF;
      END IF;
      RETURN NULL;
    END; $$ LANGUAGE plpgsql
    """)
    op.execute("DROP TRIGGER IF EXISTS trg_validate_issue_container_state ON transport_containers")
    op.execute("DROP TRIGGER IF EXISTS trg_validate_issue_container ON container_issues")
    op.execute("DROP FUNCTION IF EXISTS validate_issue_container_consistency()")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_guard_issue_proposal ON container_issue_resolution_proposals"
    )
    op.execute("DROP TRIGGER IF EXISTS trg_guard_container_issue ON container_issues")
    op.execute("DROP FUNCTION IF EXISTS guard_issue_resolution_proposal()")
    op.execute("DROP FUNCTION IF EXISTS guard_container_issue()")
    op.drop_index("uq_issue_pending_proposal", table_name="container_issue_resolution_proposals")
    op.drop_index(
        "ix_issue_proposals_issue_created", table_name="container_issue_resolution_proposals"
    )
    op.drop_table("container_issue_resolution_proposals")
    op.drop_index("uq_container_issue_open", table_name="container_issues")
    op.drop_index("ix_container_issues_shipment_created", table_name="container_issues")
    op.drop_table("container_issues")
