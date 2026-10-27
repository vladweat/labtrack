"""Tube result entry and scoped receiving history endpoints."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import API_ERROR_RESPONSES
from app.db.session import get_db_session
from app.modules.idempotency.service import execute_idempotently
from app.modules.identity.access import Principal, get_current_principal
from app.modules.identity.models import Membership, Organization, Region, User
from app.modules.organizations.models import IssueReason
from app.modules.shipments.models import (
    ContainerItem,
    ContainerReceiptEvidence,
    Shipment,
    TransportContainer,
)
from app.modules.shipments.models import (
    ReceiptRevision as ReceiptRevisionModel,
)
from app.modules.shipments.models import (
    ReceivingSession as ReceivingSessionModel,
)
from app.modules.shipments.schemas import (
    ContainerItemWriteResponse,
    ReceiptResultRequest,
)
from app.modules.shipments.schemas import (
    ReceivingHistory as ReceivingHistorySchema,
)

router = APIRouter(prefix="/shipments", tags=["Receiving"])
ERRORS = {status: API_ERROR_RESPONSES[status] for status in (401, 403, 404, 409, 422, 503)}


async def active_destination_lab_operator(
    session: AsyncSession, shipment: Shipment, user_id: UUID
) -> bool:
    return bool(
        await session.scalar(
            select(Membership.id)
            .join(User)
            .join(Organization, Organization.id == Membership.organization_id)
            .join(Region, Region.id == Organization.region_id)
            .where(
                Membership.user_id == user_id,
                Membership.organization_id == shipment.destination_organization_id,
                Membership.role == "LAB_OPERATOR",
                Membership.is_active.is_(True),
                User.is_active.is_(True),
                Organization.is_active.is_(True),
                Region.is_active.is_(True),
            )
        )
    )


def item_response(item: ContainerItem) -> dict:
    return {
        "id": str(item.id),
        "container_id": str(item.container_id),
        "sample_id": str(item.sample_id),
        "receipt_result": item.receipt_result,
        "receipt_recorded_at": item.receipt_recorded_at.isoformat()
        if item.receipt_recorded_at
        else None,
        "receipt_reason_code": item.receipt_reason_code,
        "receipt_comment": item.receipt_comment,
    }


async def visible_history_shipment(
    session: AsyncSession, principal: Principal, shipment_id: UUID
) -> Shipment:
    shipment = await session.get(Shipment, shipment_id)
    if shipment is None or not (
        principal.has_role("SENDER_OPERATOR", organization_id=shipment.sender_organization_id)
        or principal.has_role("LAB_OPERATOR", organization_id=shipment.destination_organization_id)
        or principal.has_role("LAB_MANAGER", organization_id=shipment.destination_organization_id)
    ):
        raise HTTPException(status_code=404, detail="Resource not found")
    return shipment


@router.patch(
    "/{shipment_id}/receiving/items/{item_id}",
    response_model=ContainerItemWriteResponse,
    responses=ERRORS,
    operation_id="recordReceipt",
)
async def record_receipt(
    shipment_id: UUID,
    item_id: UUID,
    payload: ReceiptResultRequest,
    response: Response,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=128)],
) -> dict:
    visible_shipment = await session.get(Shipment, shipment_id)
    if visible_shipment is None or not principal.has_role(
        "LAB_OPERATOR", organization_id=visible_shipment.destination_organization_id
    ):
        raise HTTPException(status_code=404, detail="Resource not found")
    if not await active_destination_lab_operator(session, visible_shipment, principal.user_id):
        raise HTTPException(status_code=404, detail="Resource not found")

    async def action() -> tuple[int, dict]:
        shipment = await session.scalar(
            select(Shipment).where(Shipment.id == shipment_id).with_for_update()
        )
        if shipment is None or not principal.has_role(
            "LAB_OPERATOR", organization_id=shipment.destination_organization_id
        ):
            raise HTTPException(status_code=404, detail="Resource not found")
        if not await active_destination_lab_operator(session, shipment, principal.user_id):
            raise HTTPException(status_code=404, detail="Resource not found")

        container = await session.scalar(
            select(TransportContainer)
            .join(ContainerItem, ContainerItem.container_id == TransportContainer.id)
            .where(
                TransportContainer.shipment_id == shipment.id,
                ContainerItem.id == item_id,
            )
            .with_for_update(of=TransportContainer)
        )
        if container is None:
            raise HTTPException(status_code=404, detail="Resource not found")

        item = await session.scalar(
            select(ContainerItem)
            .where(ContainerItem.id == item_id, ContainerItem.container_id == container.id)
            .with_for_update()
        )
        if item is None:
            raise HTTPException(status_code=404, detail="Resource not found")

        receiving_session = await session.scalar(
            select(ReceivingSessionModel)
            .where(
                ReceivingSessionModel.shipment_id == shipment.id,
                ReceivingSessionModel.status == "OPEN",
            )
            .with_for_update()
        )
        if receiving_session is None:
            raise HTTPException(status_code=409, detail="Receiving session is not open")

        evidence = await session.scalar(
            select(ContainerReceiptEvidence).where(
                ContainerReceiptEvidence.container_id == container.id
            )
        )
        evidence_method = (
            container.receipt_evidence.get("method") if container.receipt_evidence else None
        )
        if (
            container.delivery_status != "RECEIVED"
            or evidence is None
            or evidence.method not in {"NORMAL", "EXCEPTIONAL"}
            or evidence_method != evidence.method
        ):
            raise HTTPException(
                status_code=409,
                detail="Tube results require a received container with verified receipt evidence",
            )

        reason_code = None
        comment = None
        request_data = payload.root
        if request_data.result == "ACCEPTED":
            reason_code = request_data.reason_code
            comment = request_data.comment
        else:
            reason = await session.scalar(
                select(IssueReason).where(
                    IssueReason.code == request_data.reason_code,
                    IssueReason.scope == "SAMPLE",
                    IssueReason.kind == request_data.result,
                    IssueReason.is_active.is_(True),
                )
            )
            if reason is None:
                raise HTTPException(status_code=409, detail="Reason code does not match the result")
            reason_code = reason.code
            comment = request_data.comment

        previous_result = item.receipt_result
        previous_reason_code = item.receipt_reason_code
        previous_comment = item.receipt_comment
        item.receipt_result = request_data.result
        item.receipt_recorded_at = func.clock_timestamp()
        item.receipt_reason_code = reason_code
        item.receipt_comment = comment
        await session.flush()
        await session.refresh(item, attribute_names=["receipt_recorded_at"])

        session.add(
            ReceiptRevisionModel(
                receiving_session_id=receiving_session.id,
                container_item_id=item.id,
                old_result=previous_result,
                new_result=item.receipt_result,
                old_reason_code=previous_reason_code,
                new_reason_code=item.receipt_reason_code,
                old_comment=previous_comment,
                new_comment=item.receipt_comment,
                changed_by=principal.user_id,
            )
        )
        await session.flush()
        return 200, item_response(item)

    operation = f"shipments.{shipment_id}.receiving.items.{item_id}.record"
    status_code, result = await execute_idempotently(
        session,
        actor_id=principal.user_id,
        operation=operation,
        key=idempotency_key,
        request_data=payload.root.model_dump(mode="json"),
        action=action,
        audit_details={
            "shipment_id": str(shipment_id),
            "item_id": str(item_id),
            "result": payload.root.result,
        },
    )
    response.status_code = status_code
    return result


@router.get(
    "/{shipment_id}/receiving/history",
    response_model=ReceivingHistorySchema,
    responses=ERRORS,
    operation_id="getReceivingHistory",
)
async def get_receiving_history(
    shipment_id: UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ReceivingHistorySchema:
    shipment = await visible_history_shipment(session, principal, shipment_id)
    sessions = list(
        await session.scalars(
            select(ReceivingSessionModel)
            .where(ReceivingSessionModel.shipment_id == shipment.id)
            .order_by(ReceivingSessionModel.sequence)
        )
    )
    revisions_query = (
        select(ReceiptRevisionModel)
        .join(
            ReceivingSessionModel,
            ReceivingSessionModel.id == ReceiptRevisionModel.receiving_session_id,
        )
        .join(ContainerItem, ContainerItem.id == ReceiptRevisionModel.container_item_id)
        .join(TransportContainer, TransportContainer.id == ContainerItem.container_id)
        .where(
            ReceivingSessionModel.shipment_id == shipment.id,
            TransportContainer.shipment_id == shipment.id,
        )
    )
    total_revisions = await session.scalar(
        select(func.count()).select_from(revisions_query.subquery())
    )
    revisions = list(
        await session.scalars(
            revisions_query.order_by(
                ReceiptRevisionModel.changed_at.desc(), ReceiptRevisionModel.id.desc()
            )
            .limit(limit)
            .offset(offset)
        )
    )
    return ReceivingHistorySchema(
        sessions=sessions,
        revisions=revisions,
        total_revisions=total_revisions or 0,
        limit=limit,
        offset=offset,
    )
