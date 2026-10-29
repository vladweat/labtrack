"""Validate Sample receipt reasons and require completion before session reopen."""

from collections.abc import Sequence

from alembic import op

revision: str = "0008_stage_f_integrity"
down_revision: str | Sequence[str] | None = "0007_stage_f_lifecycle"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
    CREATE FUNCTION guard_container_item_receipt_reason() RETURNS trigger AS $$
    BEGIN
      IF TG_OP='UPDATE' AND
         NEW.receipt_result IS NOT DISTINCT FROM OLD.receipt_result AND
         NEW.receipt_recorded_at IS NOT DISTINCT FROM OLD.receipt_recorded_at AND
         NEW.receipt_reason_code IS NOT DISTINCT FROM OLD.receipt_reason_code AND
         NEW.receipt_comment IS NOT DISTINCT FROM OLD.receipt_comment THEN
        RETURN NEW;
      END IF;
      IF NEW.receipt_result IS NULL OR NEW.receipt_result='ACCEPTED' THEN
        IF NEW.receipt_reason_code IS NOT NULL OR NEW.receipt_comment IS NOT NULL THEN
          RAISE EXCEPTION 'Receipt result reason and comment require a MISSING or REJECTED result';
        END IF;
      ELSIF NEW.receipt_result NOT IN ('MISSING','REJECTED') OR NOT EXISTS (
        SELECT 1 FROM issue_reason_codes r WHERE r.code=NEW.receipt_reason_code
          AND r.scope='SAMPLE' AND r.kind=NEW.receipt_result AND r.is_active
      ) THEN
        RAISE EXCEPTION
          'Receipt result reason must be an active SAMPLE code matching the result kind';
      END IF;
      RETURN NEW;
    END; $$ LANGUAGE plpgsql
    """)
    # PostgreSQL orders same-event triggers by name: validate the nullable result
    # fields before the existing receipt eligibility and manifest guards.
    op.execute("""
    CREATE TRIGGER trg_guard_container_item_receipt_reason BEFORE INSERT OR UPDATE
    ON container_items FOR EACH ROW EXECUTE FUNCTION guard_container_item_receipt_reason()
    """)

    op.execute("""
    CREATE FUNCTION guard_receiving_reopen() RETURNS trigger AS $$
    DECLARE shipment_state text;
    BEGIN
      SELECT status INTO shipment_state FROM shipments
        WHERE id=NEW.shipment_id FOR UPDATE;
      IF NEW.sequence>1 AND
         COALESCE(shipment_state,'') NOT IN ('COMPLETED','COMPLETED_WITH_ISSUES') THEN
        RAISE EXCEPTION 'Reopened receiving session requires a completed Shipment';
      END IF;
      RETURN NEW;
    END; $$ LANGUAGE plpgsql
    """)
    # Run before guard_receiving_session so all session inserts first lock the
    # Shipment, then validate sequence, receipt provenance, manager and reason.
    op.execute("""
    CREATE TRIGGER trg_guard_receiving_reopen BEFORE INSERT ON receiving_sessions
    FOR EACH ROW EXECUTE FUNCTION guard_receiving_reopen()
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_guard_receiving_reopen ON receiving_sessions")
    op.execute("DROP FUNCTION IF EXISTS guard_receiving_reopen()")
    op.execute("DROP TRIGGER IF EXISTS trg_guard_container_item_receipt_reason ON container_items")
    op.execute("DROP FUNCTION IF EXISTS guard_container_item_receipt_reason()")
