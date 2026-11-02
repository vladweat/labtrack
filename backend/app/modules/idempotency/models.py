"""Idempotency request/replay persistence."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_records"
    __table_args__ = (
        CheckConstraint("length(key) > 0", name="nonempty_key"),
        UniqueConstraint(
            "actor_user_id", "operation", "key", name="uq_idempotency_actor_operation_key"
        ),
        CheckConstraint(
            "(offline_retention_expires_at IS NULL AND offline_causal_metadata IS NULL) "
            "OR operation = 'courier_offline_command'",
            name="offline_metadata_namespace",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    actor_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    operation: Mapped[str] = mapped_column(String(160), nullable=False)
    key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    request_context: Mapped[dict[str, str] | None] = mapped_column(JSONB)
    response_status: Mapped[int | None] = mapped_column(Integer)
    response_body: Mapped[dict | None] = mapped_column(JSONB)
    offline_retention_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    offline_causal_metadata: Mapped[dict | None] = mapped_column(JSONB)


class OfflineCommandTombstone(Base):
    """Immutable replay facts keyed independently of the actor's user row."""

    __tablename__ = "offline_command_tombstones"
    __table_args__ = (
        CheckConstraint("length(client_command_id) > 0", name="nonempty_client_command_id"),
        CheckConstraint("length(request_hash) = 64", name="request_hash_length"),
        CheckConstraint(
            "original_outcome IN ('APPLIED', 'REJECTED', 'REVIEW_REQUIRED')",
            name="persisted_outcome",
        ),
        CheckConstraint("jsonb_typeof(replay_response) = 'object'", name="replay_response_object"),
    )

    # Deliberately no FK: deleting an actor must not release their command IDs.
    actor_user_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    client_command_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    original_outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp(), nullable=False
    )
    audit_event_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("audit_events.id", ondelete="RESTRICT")
    )
    replay_response: Mapped[dict] = mapped_column(JSONB, nullable=False)
