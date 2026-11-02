"""Bind HTTP idempotency records to their route resource scope."""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0011_http_request_scope"
down_revision: str | Sequence[str] | None = "0010_notifications"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "idempotency_records",
        sa.Column("request_context", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.execute(
        """
        WITH matched_audit AS (
            SELECT
                records.id,
                records.operation,
                audit.details,
                audit.resource_id,
                count(audit.id) OVER (PARTITION BY records.id) AS audit_count
            FROM idempotency_records AS records
            LEFT JOIN audit_events AS audit
                ON audit.actor_user_id = records.actor_user_id
                AND audit.operation = records.operation
                AND audit.request_key = records.key
                AND audit.request_hash = records.request_hash
            WHERE records.operation <> 'courier_offline_command'
        ),
        recovered_scope AS (
            SELECT
                id,
                CASE
                    WHEN audit_count <> 1 THEN NULL
                    WHEN operation = 'notifications.read' THEN
                        CASE
                            WHEN resource_id IS NULL THEN NULL
                            ELSE jsonb_build_object('notification_id', resource_id)
                        END
                    WHEN details IS NULL OR jsonb_typeof(details) <> 'object' THEN '{}'::jsonb
                    ELSE COALESCE(
                        (
                            SELECT jsonb_object_agg(entry.key, entry.value)
                            FROM jsonb_each(details) AS entry
                            WHERE entry.key = ANY(
                                ARRAY[
                                    'shipment_id', 'container_id', 'item_id', 'incident_id',
                                    'issue_id', 'proposal_id', 'sample_id', 'notification_id'
                                ]
                            )
                            AND NOT (
                                operation = 'shipment.container_issue.report'
                                AND entry.key = 'container_id'
                            )
                        ),
                        '{}'::jsonb
                    )
                END AS request_context
            FROM matched_audit
        )
        UPDATE idempotency_records AS records
        SET request_context = recovered_scope.request_context
        FROM recovered_scope
        WHERE records.id = recovered_scope.id
        """
    )


def downgrade() -> None:
    op.drop_column("idempotency_records", "request_context")
