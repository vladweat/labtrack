"""Authenticated personal Inbox routes."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import API_ERROR_RESPONSES
from app.db.session import get_db_session
from app.modules.idempotency.service import execute_idempotently
from app.modules.identity.access import Principal, get_current_principal
from app.modules.notifications.schemas import (
    Notification,
    NotificationUnreadCount,
    PagedNotification,
)
from app.modules.notifications.service import (
    list_notifications,
    mark_notification_read,
    unread_notification_count,
)

router = APIRouter(prefix="/notifications", tags=["Notifications"])
SUCCESS_RESPONSE = {
    "description": "Success",
    "headers": {"X-Request-ID": {"schema": {"type": "string", "format": "uuid"}}},
}


@router.get(
    "",
    response_model=PagedNotification,
    operation_id="listInboxNotifications",
    summary="Private inbox; read/unread entries only for recipient",
    responses={
        **{status: API_ERROR_RESPONSES[status] for status in (401, 403, 404, 409, 422, 503)},
        200: SUCCESS_RESPONSE,
    },
)
async def get_notifications(
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    unread_only: bool = False,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> PagedNotification:
    return await list_notifications(
        session,
        recipient_user_id=principal.user_id,
        unread_only=unread_only,
        limit=limit,
        offset=offset,
    )


@router.post(
    "/{notification_id}/read",
    response_model=Notification,
    operation_id="markInboxNotificationRead",
    summary="Idempotently mark own notification read",
    responses={
        **{status: API_ERROR_RESPONSES[status] for status in (401, 403, 404, 409, 422, 503)},
        200: SUCCESS_RESPONSE,
    },
)
async def mark_notification_read_route(
    notification_id: UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    idempotency_key: Annotated[
        str,
        Header(
            alias="Idempotency-Key",
            min_length=8,
            max_length=128,
            description=(
                "Stable idempotency key for this HTTP command; ordinary route retention "
                "independent of 90-day offline replay"
            ),
        ),
    ],
) -> Notification:
    async def action() -> tuple[int, dict]:
        row = await mark_notification_read(
            session,
            recipient_user_id=principal.user_id,
            notification_id=notification_id,
        )
        return 200, Notification.model_validate(row).model_dump(mode="json")

    _, response_body = await execute_idempotently(
        session,
        actor_id=principal.user_id,
        operation="notifications.read",
        key=idempotency_key,
        request_data={"notification_id": str(notification_id)},
        action=action,
    )
    return Notification.model_validate(response_body)


@router.get(
    "/unread-count",
    response_model=NotificationUnreadCount,
    operation_id="getInboxUnreadCount",
    summary="getInboxUnreadCount",
    responses={
        **{status: API_ERROR_RESPONSES[status] for status in (401, 403, 404, 409, 422, 503)},
        200: SUCCESS_RESPONSE,
    },
)
async def get_unread_count(
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> NotificationUnreadCount:
    return await unread_notification_count(session, recipient_user_id=principal.user_id)
