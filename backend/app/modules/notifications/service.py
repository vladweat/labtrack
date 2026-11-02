"""Recipient-owned Inbox queries and idempotent notification persistence."""

from collections.abc import Iterable
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.notifications.models import NotificationRecord
from app.modules.notifications.schemas import (
    Notification,
    NotificationPayload,
    NotificationUnreadCount,
    PagedNotification,
)
from app.modules.operations.service import StalledNotificationCandidate


async def list_notifications(
    session: AsyncSession,
    *,
    recipient_user_id: UUID,
    unread_only: bool,
    limit: int,
    offset: int,
) -> PagedNotification:
    """List only the authenticated recipient's rows in stable newest-first order."""

    conditions = [NotificationRecord.recipient_user_id == recipient_user_id]
    if unread_only:
        conditions.append(NotificationRecord.read_at.is_(None))
    total = await session.scalar(
        select(func.count()).select_from(NotificationRecord).where(*conditions)
    )
    rows = await session.scalars(
        select(NotificationRecord)
        .where(*conditions)
        .order_by(NotificationRecord.created_at.desc(), NotificationRecord.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return PagedNotification(
        items=[Notification.model_validate(row) for row in rows],
        total=total or 0,
        limit=limit,
        offset=offset,
    )


async def unread_notification_count(
    session: AsyncSession, *, recipient_user_id: UUID
) -> NotificationUnreadCount:
    """Count unread rows belonging to the authenticated recipient."""

    count = await session.scalar(
        select(func.count())
        .select_from(NotificationRecord)
        .where(
            NotificationRecord.recipient_user_id == recipient_user_id,
            NotificationRecord.read_at.is_(None),
        )
    )
    return NotificationUnreadCount(count=count or 0)


async def mark_notification_read(
    session: AsyncSession, *, recipient_user_id: UUID, notification_id: UUID
) -> NotificationRecord:
    """Set read_at once and mask notifications owned by another recipient."""

    await session.execute(
        update(NotificationRecord)
        .where(
            NotificationRecord.id == notification_id,
            NotificationRecord.recipient_user_id == recipient_user_id,
            NotificationRecord.read_at.is_(None),
        )
        .values(read_at=func.now())
    )
    notification = await session.scalar(
        select(NotificationRecord).where(
            NotificationRecord.id == notification_id,
            NotificationRecord.recipient_user_id == recipient_user_id,
        )
    )
    if notification is None:
        raise HTTPException(status_code=404, detail="Resource not found")
    return notification


async def persist_stalled_notifications(
    session: AsyncSession, candidates: Iterable[StalledNotificationCandidate]
) -> int:
    """Insert safe recipient rows, relying on the unique key for retry safety."""

    values = []
    for candidate in candidates:
        payload = NotificationPayload.model_validate(candidate.payload).model_dump(
            mode="json", exclude_unset=True
        )
        for recipient_user_id in candidate.recipient_user_ids:
            values.append(
                {
                    "id": uuid4(),
                    "event_id": candidate.event_id,
                    "type": candidate.case_type,
                    "payload": payload,
                    "recipient_user_id": recipient_user_id,
                }
            )
    if not values:
        return 0

    result = await session.execute(
        insert(NotificationRecord)
        .values(values)
        .on_conflict_do_nothing(
            index_elements=[
                NotificationRecord.recipient_user_id,
                NotificationRecord.event_id,
                NotificationRecord.type,
            ]
        )
    )
    return result.rowcount or 0
