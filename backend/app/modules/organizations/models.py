"""Fixed issue-reason catalog."""

from uuid import UUID, uuid4

from sqlalchemy import Boolean, CheckConstraint, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class IssueReason(Base):
    __tablename__ = "issue_reason_codes"
    __table_args__ = (
        CheckConstraint("scope IN ('SAMPLE', 'CONTAINER')", name="subject_type"),
        CheckConstraint("kind IN ('MISSING', 'REJECTED')", name="outcome"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    code: Mapped[str] = mapped_column(String(48), unique=True, nullable=False)
    scope: Mapped[str] = mapped_column(String(16), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    label: Mapped[str] = mapped_column(String(160), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
