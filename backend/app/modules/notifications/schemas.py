"""Safe response contracts for the personal Inbox."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

StalledCaseType = Literal[
    "PICKUP_PENDING",
    "DELIVERY_PENDING",
    "PROPOSAL_PENDING",
    "SHIPMENT_OVERDUE",
]


class NotificationPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_type: StalledCaseType
    shipment_id: UUID
    region_id: UUID
    status: str
    last_event_at: datetime
    container_id: UUID | None = None
    proposal_id: UUID | None = None


class Notification(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: UUID
    event_id: UUID
    type: str
    payload: NotificationPayload
    recipient_user_id: UUID
    created_at: datetime
    read_at: datetime | None


class PagedNotification(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[Notification]
    total: int = Field(ge=0)
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)


class NotificationUnreadCount(BaseModel):
    model_config = ConfigDict(extra="forbid")

    count: int = Field(ge=0)
