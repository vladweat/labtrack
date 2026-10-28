"""Tube result entry and scoped receiving history endpoints."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query, Response
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
    CompleteReceivingRequest,
    ContainerItemWriteResponse,
    ReceiptResultRequest,
    ReopenReceivingRequest,
    ShipmentResponse,
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


async def active_destination_lab_manager(
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
                Membership.role == "LAB_MANAGER",
                Membership.is_active.is_(True),
                User.is_active.is_(True),
                Organization.is_active.is_(True),
                Region.is_active.is_(True),
            )
        )
    )


def receiving_shipment_response(shipment: Shipment) -> dict:
    return {
        "id": str(shipment.id),
        "code": shipment.code,
        "region_id": str(shipment.region_id),
        "sender_org_id": str(shipment.sender_organization_id),
        "destination_org_id": str(shipment.destination_organization_id),
        "courier_user_id": str(shipment.courier_user_id) if shipment.courier_user_id else None,
        "status": shipment.status,
        "version": shipment.version,
        "manifest_revision": shipment.manifest_revision,
        "expected_delivery_at": shipment.expected_delivery_at.isoformat()
        if shipment.expected_delivery_at
        else None,
        "sealed_at": shipment.sealed_at.isoformat() if shipment.sealed_at else None,
        "created_at": shipment.created_at.isoformat(),
        "updated_at": (shipment.__dict__.get("updated_at") or shipment.created_at).isoformat(),
        "completed_at": None,
        "closed_at": shipment.closed_at.isoformat() if shipment.closed_at else None,
        "closed_by": str(shipment.closed_by) if shipment.closed_by else None,
        "closure_reason": shipment.closure_reason,
    }


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


@router.post(
    "/{shipment_id}/receiving/complete",
    response_model=ShipmentResponse,
    responses=ERRORS,
    operation_id="completeReceiving",
)
async def complete_receiving(
    shipment_id: UUID,
    response: Response,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=128)],
    payload: Annotated[CompleteReceivingRequest | None, Body()] = None,
) -> ShipmentResponse:
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
        if shipment.status != "RECEIVING":
            raise HTTPException(status_code=409, detail="Shipment is not receiving")

        containers = list(
            await session.scalars(
                select(TransportContainer)
                .where(TransportContainer.shipment_id == shipment.id)
                .order_by(TransportContainer.id)
                .with_for_update()
            )
        )
        if not containers:
            raise HTTPException(status_code=409, detail="Shipment has no containers to receive")
        if any(
            container.delivery_status not in {"RECEIVED", "UNDELIVERED_CLOSED"}
            for container in containers
        ):
            raise HTTPException(
                status_code=409,
                detail="Every container must be received or closed as undelivered",
            )
        received_containers = [
            container for container in containers if container.delivery_status == "RECEIVED"
        ]
        if not received_containers:
            raise HTTPException(status_code=409, detail="At least one container must be received")

        items = list(
            await session.scalars(
                select(ContainerItem)
                .where(ContainerItem.container_id.in_([container.id for container in containers]))
                .order_by(ContainerItem.id)
                .with_for_update()
            )
        )
        items_by_container: dict[UUID, list[ContainerItem]] = {
            container.id: [] for container in containers
        }
        for item in items:
            items_by_container[item.container_id].append(item)

        for container in received_containers:
            if any(
                item.receipt_result not in {"ACCEPTED", "MISSING", "REJECTED"}
                or item.receipt_recorded_at is None
                for item in items_by_container[container.id]
            ):
                raise HTTPException(
                    status_code=409,
                    detail="Every item in a received container must have a result",
                )
        for container in containers:
            if container.delivery_status == "UNDELIVERED_CLOSED" and any(
                item.receipt_result is not None
                or item.receipt_recorded_at is not None
                or item.receipt_reason_code is not None
                or item.receipt_comment is not None
                for item in items_by_container[container.id]
            ):
                raise HTTPException(
                    status_code=409,
                    detail="Undelivered container items must not have receipt results",
                )

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

        all_received_accepted = len(received_containers) == len(containers) and all(
            item.receipt_result == "ACCEPTED"
            for container in received_containers
            for item in items_by_container[container.id]
        )
        final_status = "COMPLETED" if all_received_accepted else "COMPLETED_WITH_ISSUES"
        receiving_session.status = "CLOSED"
        receiving_session.closed_by = principal.user_id
        receiving_session.closed_at = await session.scalar(select(func.clock_timestamp()))
        await session.flush()
        shipment.status = final_status
        shipment.version += 1
        await session.flush()
        await session.refresh(shipment, attribute_names=["updated_at"])
        return 200, receiving_shipment_response(shipment)

    operation = f"shipments.{shipment_id}.receiving.complete"
    request_data = payload.model_dump(mode="json") if payload is not None else {}
    status_code, result = await execute_idempotently(
        session,
        actor_id=principal.user_id,
        operation=operation,
        key=idempotency_key,
        request_data=request_data,
        action=action,
        audit_details={"shipment_id": str(shipment_id)},
    )
    response.status_code = status_code
    return ShipmentResponse.model_validate(result)


@router.post(
    "/{shipment_id}/receiving/reopen",
    response_model=ShipmentResponse,
    responses=ERRORS,
    operation_id="reopenReceiving",
)
async def reopen_receiving(
    shipment_id: UUID,
    payload: ReopenReceivingRequest,
    response: Response,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=128)],
) -> ShipmentResponse:
    visible_shipment = await session.get(Shipment, shipment_id)
    if visible_shipment is None or not principal.has_role(
        "LAB_MANAGER", organization_id=visible_shipment.destination_organization_id
    ):
        raise HTTPException(status_code=404, detail="Resource not found")
    if not await active_destination_lab_manager(session, visible_shipment, principal.user_id):
        raise HTTPException(status_code=404, detail="Resource not found")

    async def action() -> tuple[int, dict]:
        shipment = await session.scalar(
            select(Shipment).where(Shipment.id == shipment_id).with_for_update()
        )
        if shipment is None or not principal.has_role(
            "LAB_MANAGER", organization_id=shipment.destination_organization_id
        ):
            raise HTTPException(status_code=404, detail="Resource not found")
        if not await active_destination_lab_manager(session, shipment, principal.user_id):
            raise HTTPException(status_code=404, detail="Resource not found")
        if shipment.status not in {"COMPLETED", "COMPLETED_WITH_ISSUES"}:
            raise HTTPException(status_code=409, detail="Shipment receiving is not complete")

        receiving_sessions = list(
            await session.scalars(
                select(ReceivingSessionModel)
                .where(ReceivingSessionModel.shipment_id == shipment.id)
                .order_by(ReceivingSessionModel.sequence)
                .with_for_update()
            )
        )
        if not receiving_sessions or any(
            receiving_session.status != "CLOSED" for receiving_session in receiving_sessions
        ):
            raise HTTPException(
                status_code=409,
                detail="Completed shipment must have only closed receiving sessions",
            )

        next_sequence = receiving_sessions[-1].sequence + 1
        session.add(
            ReceivingSessionModel(
                shipment_id=shipment.id,
                sequence=next_sequence,
                status="OPEN",
                opened_by=principal.user_id,
                reopen_reason=payload.reason,
            )
        )
        await session.flush()
        shipment.status = "RECEIVING"
        shipment.version += 1
        await session.flush()
        await session.refresh(shipment, attribute_names=["updated_at"])
        return 200, receiving_shipment_response(shipment)

    operation = f"shipments.{shipment_id}.receiving.reopen"
    status_code, result = await execute_idempotently(
        session,
        actor_id=principal.user_id,
        operation=operation,
        key=idempotency_key,
        request_data=payload.model_dump(mode="json"),
        action=action,
        audit_details={"shipment_id": str(shipment_id), "reason": payload.reason},
    )
    response.status_code = status_code
    return ShipmentResponse.model_validate(result)


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
