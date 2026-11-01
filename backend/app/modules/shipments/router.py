"""Draft shipment and sealed-manifest endpoints."""

from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.errors import API_ERROR_RESPONSES
from app.db.session import get_db_session
from app.modules.idempotency.service import execute_idempotently
from app.modules.identity.access import Principal, get_current_principal
from app.modules.identity.models import Membership, Organization, Region, User
from app.modules.samples.models import Sample
from app.modules.shipments.commands import (
    active_assigned_courier,
    confirm_pickup_command,
    derive_delivery_shipment_status,
    handoff_dict,
    initiate_container_delivery_command,
    lock_delivery_scope,
    lock_pickup_shipment,
)
from app.modules.shipments.models import (
    ContainerItem,
    ContainerReceiptEvidence,
    Handoff,
    ReceivingSession,
    Shipment,
    TransportContainer,
)
from app.modules.shipments.schemas import (
    AddSampleRequest,
    AssignCourierRequest,
    ContainerCodeRequest,
    ContainerCodesRequest,
    ContainerItemWriteResponse,
    CreateShipmentRequest,
    DeliveryHandoffHistory,
    HandoffActionResult,
    ManifestResponse,
    PagedShipmentResponse,
    ReasonRequest,
    ShipmentResponse,
    TransportContainerResponse,
    UpdateDraftShipmentRequest,
)

router = APIRouter(prefix="/shipments", tags=["shipments"])
DELIVERY_API_ERROR_RESPONSES = {
    status: API_ERROR_RESPONSES[status] for status in (401, 403, 404, 409, 422, 503)
}


def shipment_dict(row: Shipment) -> dict:
    return {
        "id": str(row.id),
        "code": row.code,
        "region_id": str(row.region_id),
        "sender_org_id": str(row.sender_organization_id),
        "destination_org_id": str(row.destination_organization_id),
        "courier_user_id": str(row.courier_user_id) if row.courier_user_id else None,
        "status": row.status,
        "version": row.version,
        "manifest_revision": row.manifest_revision,
        "expected_delivery_at": row.expected_delivery_at.isoformat()
        if row.expected_delivery_at
        else None,
        "sealed_at": row.sealed_at.isoformat() if row.sealed_at else None,
        "created_at": row.created_at.isoformat(),
        "updated_at": (row.__dict__.get("updated_at") or row.created_at).isoformat(),
        "completed_at": None,
        "closed_at": row.closed_at.isoformat() if row.closed_at else None,
        "closed_by": str(row.closed_by) if row.closed_by else None,
        "closure_reason": row.closure_reason,
    }


async def visible(session: AsyncSession, principal: Principal, shipment_id: UUID) -> Shipment:
    row = await session.get(Shipment, shipment_id)
    if row is None or not (
        principal.has_role("SENDER_OPERATOR", organization_id=row.sender_organization_id)
        or principal.has_role("LAB_MANAGER", organization_id=row.destination_organization_id)
        or principal.has_role("LAB_OPERATOR", organization_id=row.destination_organization_id)
        or (
            row.courier_user_id == principal.user_id
            and principal.has_role("COURIER", region_id=row.region_id)
        )
    ):
        raise HTTPException(404, "Resource not found")
    return row


async def lock_draft(session: AsyncSession, principal: Principal, shipment_id: UUID) -> Shipment:
    row = await session.scalar(select(Shipment).where(Shipment.id == shipment_id).with_for_update())
    if row is None or not principal.has_role(
        "SENDER_OPERATOR", organization_id=row.sender_organization_id
    ):
        raise HTTPException(404, "Resource not found")
    if row.status != "DRAFT":
        raise HTTPException(409, "Shipment is no longer a draft")
    return row


async def manifest_dict(session: AsyncSession, row: Shipment) -> dict:
    containers = await session.scalars(
        select(TransportContainer)
        .where(TransportContainer.shipment_id == row.id)
        .order_by(TransportContainer.created_at)
    )
    values = []
    for container in containers:
        items = await session.execute(
            select(ContainerItem, Sample)
            .join(Sample)
            .where(ContainerItem.container_id == container.id)
        )
        values.append(
            {
                "id": str(container.id),
                "code": container.code,
                "delivery_status": container.delivery_status,
                "items": [
                    {
                        "id": str(item.id),
                        "sample_id": str(sample.id),
                        "sample_code": sample.code,
                        "material_type": sample.material_type,
                        "receipt_result": item.receipt_result,
                        "receipt_reason_code": item.receipt_reason_code,
                        "receipt_comment": item.receipt_comment,
                    }
                    for item, sample in items
                ],
                "receipt_evidence_method": (
                    container.receipt_evidence.get("method") if container.receipt_evidence else None
                ),
            }
        )
    return {
        "shipment_id": str(row.id),
        "status": row.status,
        "sealed_at": row.sealed_at.isoformat() if row.sealed_at else None,
        "manifest_revision": row.manifest_revision,
        "containers": values,
    }


async def mutate(
    session, principal, operation, key, payload, action, response, *, audit_details=None
):
    code, body = await execute_idempotently(
        session,
        actor_id=principal.user_id,
        operation=operation,
        key=key,
        request_data=payload,
        action=action,
        audit_details=audit_details,
    )
    response.status_code = code
    return body


def receipt_evidence_dict(row: ContainerReceiptEvidence) -> dict:
    return {
        "id": str(row.id),
        "container_id": str(row.container_id),
        "method": row.method,
        "handoff_id": str(row.confirmed_delivery_handoff_id)
        if row.confirmed_delivery_handoff_id
        else None,
        "incident_id": str(row.resolved_incident_id) if row.resolved_incident_id else None,
        "verified_at": row.verified_at.isoformat(),
        "verified_by": str(row.verified_by),
    }


async def pickup_container_codes(session: AsyncSession, shipment_id: UUID) -> list[str]:
    return list(
        await session.scalars(
            select(TransportContainer.code)
            .where(TransportContainer.shipment_id == shipment_id)
            .order_by(TransportContainer.id)
        )
    )


async def active_destination_lab_member(
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
                Membership.role.in_(("LAB_OPERATOR", "LAB_MANAGER")),
                Membership.is_active.is_(True),
                User.is_active.is_(True),
                Organization.is_active.is_(True),
                Region.is_active.is_(True),
            )
        )
    )


@router.post("", status_code=201, response_model=ShipmentResponse)
async def create_shipment(
    request: CreateShipmentRequest,
    response: Response,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    if not principal.has_role("SENDER_OPERATOR", organization_id=request.sender_org_id):
        raise HTTPException(404, "Resource not found")

    async def action():
        sender = await session.scalar(
            select(Organization).where(
                Organization.id == request.sender_org_id, Organization.is_active.is_(True)
            )
        )
        destination = await session.scalar(
            select(Organization).where(
                Organization.id == request.destination_org_id,
                Organization.code == settings.destination_organization_code,
                Organization.organization_type == "LABORATORY",
                Organization.is_active.is_(True),
            )
        )
        if sender is None or destination is None or sender.region_id != destination.region_id:
            raise HTTPException(
                422, "Sender and configured destination laboratory must share a region"
            )
        row = Shipment(
            code=f"SHP-{uuid4().hex[:12].upper()}",
            region_id=sender.region_id,
            sender_organization_id=sender.id,
            destination_organization_id=destination.id,
            expected_delivery_at=request.expected_delivery_at,
            created_by_user_id=principal.user_id,
        )
        session.add(row)
        await session.flush()
        return 201, shipment_dict(row)

    body = await mutate(
        session,
        principal,
        "shipments.create",
        idempotency_key,
        request.model_dump(mode="json"),
        action,
        response,
    )
    return ShipmentResponse.model_validate(body)


@router.get("", response_model=PagedShipmentResponse)
async def list_shipments(
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    org_ids = {
        grant.organization_id
        for grant in principal.memberships
        if grant.organization_id is not None and grant.role in {"SENDER_OPERATOR", "LAB_MANAGER"}
    }
    query = select(Shipment).where(
        Shipment.sender_organization_id.in_(org_ids)
        | Shipment.destination_organization_id.in_(org_ids)
        | (
            (Shipment.courier_user_id == principal.user_id)
            & (
                Shipment.region_id.in_(
                    {g.region_id for g in principal.memberships if g.role == "COURIER"}
                )
            )
        )
    )
    total = await session.scalar(select(func.count()).select_from(query.subquery()))
    rows = await session.scalars(
        query.order_by(Shipment.created_at.desc()).limit(min(limit, 100)).offset(offset)
    )
    return PagedShipmentResponse(
        items=[ShipmentResponse.model_validate(shipment_dict(row)) for row in rows],
        total=total or 0,
        limit=limit,
        offset=offset,
    )


@router.get("/{shipment_id}", response_model=ShipmentResponse)
async def get_shipment(
    shipment_id: UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
):
    return ShipmentResponse.model_validate(
        shipment_dict(await visible(session, principal, shipment_id))
    )


@router.get("/{shipment_id}/manifest", response_model=ManifestResponse)
async def get_manifest(
    shipment_id: UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
):
    return ManifestResponse.model_validate(
        await manifest_dict(session, await visible(session, principal, shipment_id))
    )


@router.patch("/{shipment_id}", response_model=ShipmentResponse)
async def update_draft(
    shipment_id: UUID,
    request: UpdateDraftShipmentRequest,
    response: Response,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    async def action():
        row = await lock_draft(session, principal, shipment_id)
        row.expected_delivery_at = request.expected_delivery_at
        row.version += 1
        await session.flush()
        await session.refresh(row, attribute_names=["updated_at"])
        return 200, shipment_dict(row)

    return ShipmentResponse.model_validate(
        await mutate(
            session,
            principal,
            f"shipments.{shipment_id}.update",
            idempotency_key,
            request.model_dump(mode="json"),
            action,
            response,
        )
    )


@router.patch("/{shipment_id}/courier", response_model=ShipmentResponse)
async def assign_courier(
    shipment_id: UUID,
    request: AssignCourierRequest,
    response: Response,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    async def action():
        row = await lock_draft(session, principal, shipment_id)
        valid = await session.scalar(
            select(Membership.id)
            .join(User)
            .where(
                Membership.user_id == request.courier_user_id,
                Membership.region_id == row.region_id,
                Membership.organization_id.is_(None),
                Membership.role == "COURIER",
                Membership.is_active.is_(True),
                User.is_active.is_(True),
            )
        )
        if valid is None:
            raise HTTPException(422, "Courier must have an active membership in shipment region")
        row.courier_user_id = request.courier_user_id
        row.version += 1
        await session.flush()
        await session.refresh(row, attribute_names=["updated_at"])
        return 200, shipment_dict(row)

    return ShipmentResponse.model_validate(
        await mutate(
            session,
            principal,
            f"shipments.{shipment_id}.courier",
            idempotency_key,
            request.model_dump(mode="json"),
            action,
            response,
        )
    )


@router.post(
    "/{shipment_id}/containers", status_code=201, response_model=TransportContainerResponse
)
async def add_container(
    shipment_id: UUID,
    response: Response,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    async def action():
        row = await lock_draft(session, principal, shipment_id)
        container = TransportContainer(shipment_id=row.id, code=f"CNT-{uuid4().hex[:12].upper()}")
        session.add(container)
        row.version += 1
        row.manifest_revision += 1
        await session.flush()
        return 201, {
            "id": str(container.id),
            "code": container.code,
            "shipment_id": str(container.shipment_id),
            "created_at": container.created_at.isoformat(),
            "delivery_status": container.delivery_status,
            "received_at": None,
            "received_by": None,
            "state_version": container.state_version,
            "receipt_evidence": None,
        }

    return TransportContainerResponse.model_validate(
        await mutate(
            session,
            principal,
            f"shipments.{shipment_id}.containers.create",
            idempotency_key,
            {},
            action,
            response,
        )
    )


@router.delete("/{shipment_id}/containers/{container_id}", status_code=204)
async def delete_container(
    shipment_id: UUID,
    container_id: UUID,
    response: Response,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    async def action():
        row = await lock_draft(session, principal, shipment_id)
        container = await session.scalar(
            select(TransportContainer)
            .where(TransportContainer.id == container_id, TransportContainer.shipment_id == row.id)
            .with_for_update()
        )
        if container is None:
            raise HTTPException(404, "Resource not found")
        if await session.scalar(
            select(ContainerItem.id).where(ContainerItem.container_id == container.id)
        ):
            raise HTTPException(409, "Only empty containers can be deleted")
        await session.delete(container)
        row.version += 1
        row.manifest_revision += 1
        return 204, {}

    await mutate(
        session,
        principal,
        f"shipments.{shipment_id}.containers.{container_id}.delete",
        idempotency_key,
        {},
        action,
        response,
    )
    return Response(status_code=204)


@router.post(
    "/{shipment_id}/containers/{container_id}/items",
    status_code=201,
    response_model=ContainerItemWriteResponse,
)
async def add_sample(
    shipment_id: UUID,
    container_id: UUID,
    request: AddSampleRequest,
    response: Response,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    async def action():
        row = await lock_draft(session, principal, shipment_id)
        container = await session.scalar(
            select(TransportContainer)
            .where(TransportContainer.id == container_id, TransportContainer.shipment_id == row.id)
            .with_for_update()
        )
        sample = await session.scalar(
            select(Sample).where(
                Sample.id == request.sample_id,
                Sample.origin_organization_id == row.sender_organization_id,
            )
        )
        if container is None or sample is None:
            raise HTTPException(404, "Resource not found")
        if await session.scalar(
            select(ContainerItem.id).where(ContainerItem.sample_id == sample.id)
        ):
            raise HTTPException(409, "Sample is already assigned")
        session.add(ContainerItem(container_id=container.id, sample_id=sample.id))
        row.version += 1
        row.manifest_revision += 1
        await session.flush()
        item = await session.scalar(
            select(ContainerItem).where(
                ContainerItem.container_id == container.id,
                ContainerItem.sample_id == sample.id,
            )
        )
        return 201, {
            "id": str(item.id),
            "container_id": str(item.container_id),
            "sample_id": str(item.sample_id),
            "receipt_result": None,
            "receipt_recorded_at": None,
            "receipt_reason_code": None,
            "receipt_comment": None,
        }

    return ContainerItemWriteResponse.model_validate(
        await mutate(
            session,
            principal,
            f"shipments.{shipment_id}.containers.{container_id}.items.create",
            idempotency_key,
            request.model_dump(mode="json"),
            action,
            response,
        )
    )


@router.delete("/{shipment_id}/containers/{container_id}/items/{item_id}", status_code=204)
async def remove_sample(
    shipment_id: UUID,
    container_id: UUID,
    item_id: UUID,
    response: Response,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    async def action():
        row = await lock_draft(session, principal, shipment_id)
        item = await session.scalar(
            select(ContainerItem)
            .join(TransportContainer)
            .where(
                ContainerItem.id == item_id,
                ContainerItem.container_id == container_id,
                TransportContainer.shipment_id == row.id,
            )
            .with_for_update()
        )
        if item is None:
            raise HTTPException(404, "Resource not found")
        await session.delete(item)
        row.version += 1
        row.manifest_revision += 1
        await session.flush()
        return 204, {}

    await mutate(
        session,
        principal,
        f"shipments.{shipment_id}.items.{item_id}.delete",
        idempotency_key,
        {},
        action,
        response,
    )
    return Response(status_code=204)


@router.post("/{shipment_id}/seal", response_model=ShipmentResponse)
async def seal(
    shipment_id: UUID,
    response: Response,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    async def action():
        row = await lock_draft(session, principal, shipment_id)
        now = await session.scalar(select(func.now()))
        configured = await session.scalar(
            select(Organization.id).where(
                Organization.id == row.destination_organization_id,
                Organization.code == settings.destination_organization_code,
                Organization.organization_type == "LABORATORY",
                Organization.is_active.is_(True),
            )
        )
        if (
            configured is None
            or row.expected_delivery_at is None
            or row.expected_delivery_at <= now
        ):
            raise HTTPException(422, "Configured destination and future delivery time are required")
        containers = list(
            await session.scalars(
                select(TransportContainer)
                .where(TransportContainer.shipment_id == row.id)
                .with_for_update()
            )
        )
        if not containers:
            raise HTTPException(422, "Shipment must contain at least one container")
        for container in containers:
            if (
                await session.scalar(
                    select(ContainerItem.id).where(ContainerItem.container_id == container.id)
                )
                is None
            ):
                raise HTTPException(422, "Every container must contain a sample")
        row.status = "READY_FOR_PICKUP"
        row.sealed_at = now
        row.version += 1
        await session.flush()
        await session.refresh(row, attribute_names=["updated_at"])
        return 200, shipment_dict(row)

    return ShipmentResponse.model_validate(
        await mutate(
            session,
            principal,
            f"shipments.{shipment_id}.seal",
            idempotency_key,
            {},
            action,
            response,
        )
    )


@router.post(
    "/{shipment_id}/handoffs/pickup/confirm-release",
    response_model=HandoffActionResult,
    responses=API_ERROR_RESPONSES,
)
async def confirm_pickup_release(
    shipment_id: UUID,
    request: ContainerCodesRequest,
    response: Response,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=128)],
):
    # Authorization is evaluated before idempotency replay and rechecked under the aggregate lock.
    preview = await visible(session, principal, shipment_id)
    if not principal.has_role("SENDER_OPERATOR", organization_id=preview.sender_organization_id):
        raise HTTPException(404, "Resource not found")
    if not await active_assigned_courier(session, preview):
        raise HTTPException(409, "Shipment must have an active assigned Courier")

    async def action():
        row = await lock_pickup_shipment(session, principal, shipment_id, role="SENDER_OPERATOR")
        if not await active_assigned_courier(session, row):
            raise HTTPException(409, "Shipment must have an active assigned Courier")
        if row.status != "READY_FOR_PICKUP":
            raise HTTPException(409, "Shipment is not ready for Pickup release")
        expected_codes = await pickup_container_codes(session, row.id)
        if sorted(request.container_codes) != sorted(expected_codes):
            raise HTTPException(409, "Container codes must match the complete sealed manifest")
        previous_attempt = await session.scalar(
            select(func.max(Handoff.attempt_no)).where(
                Handoff.shipment_id == row.id, Handoff.kind == "PICKUP"
            )
        )
        handoff = Handoff(
            shipment_id=row.id,
            kind="PICKUP",
            attempt_no=(previous_attempt or 0) + 1,
            status="PENDING",
            initiated_by=principal.user_id,
        )
        session.add(handoff)
        row.status = "PICKUP_PENDING"
        row.version += 1
        await session.flush()
        await session.refresh(row, attribute_names=["updated_at"])
        return 200, {
            "shipment_id": str(row.id),
            "shipment_status": row.status,
            "version": row.version,
            "handoff": handoff_dict(handoff),
            "container_id": None,
            "container_status": None,
        }

    return HandoffActionResult.model_validate(
        await mutate(
            session,
            principal,
            f"shipments.{shipment_id}.handoffs.pickup.confirm-release",
            idempotency_key,
            request.model_dump(mode="json"),
            action,
            response,
        )
    )


@router.post(
    "/{shipment_id}/handoffs/pickup/confirm-pickup",
    response_model=HandoffActionResult,
    responses=API_ERROR_RESPONSES,
)
async def confirm_pickup(
    shipment_id: UUID,
    request: ContainerCodesRequest,
    response: Response,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=128)],
):
    preview = await visible(session, principal, shipment_id)
    if preview.courier_user_id != principal.user_id or not principal.has_role(
        "COURIER", region_id=preview.region_id
    ):
        raise HTTPException(404, "Resource not found")
    if not await active_assigned_courier(session, preview):
        raise HTTPException(404, "Resource not found")

    async def action():
        return await confirm_pickup_command(
            session, principal, shipment_id, request.container_codes
        )

    return HandoffActionResult.model_validate(
        await mutate(
            session,
            principal,
            f"shipments.{shipment_id}.handoffs.pickup.confirm-pickup",
            idempotency_key,
            request.model_dump(mode="json"),
            action,
            response,
        )
    )


@router.post(
    "/{shipment_id}/handoffs/pickup/withdraw",
    response_model=HandoffActionResult,
    responses=API_ERROR_RESPONSES,
    description=(
        "The initiating Sender asserts that no physical transfer occurred and all containers "
        "remain at origin. The reason and assertion are stored in the audit event. If physical "
        "custody is disputed, use the incident review workflow instead."
    ),
)
async def withdraw_pickup(
    shipment_id: UUID,
    request: ReasonRequest,
    response: Response,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=128)],
):
    preview = await visible(session, principal, shipment_id)
    if not principal.has_role("SENDER_OPERATOR", organization_id=preview.sender_organization_id):
        raise HTTPException(404, "Resource not found")

    async def action():
        row = await lock_pickup_shipment(session, principal, shipment_id, role="SENDER_OPERATOR")
        if row.status != "PICKUP_PENDING":
            raise HTTPException(409, "Shipment has no pending Pickup to withdraw")
        handoff = await session.scalar(
            select(Handoff)
            .where(
                Handoff.shipment_id == row.id,
                Handoff.kind == "PICKUP",
                Handoff.status == "PENDING",
            )
            .with_for_update()
        )
        if handoff is None or handoff.initiated_by != principal.user_id:
            raise HTTPException(409, "Only the initiating Sender can withdraw pending Pickup")
        handoff.status = "WITHDRAWN"
        handoff.withdrawn_by = principal.user_id
        handoff.withdrawn_at = await session.scalar(select(func.now()))
        handoff.withdrawal_reason = request.reason
        row.status = "READY_FOR_PICKUP"
        row.version += 1
        await session.flush()
        await session.refresh(row, attribute_names=["updated_at"])
        return 200, {
            "shipment_id": str(row.id),
            "shipment_status": row.status,
            "version": row.version,
            "handoff": handoff_dict(handoff),
            "container_id": None,
            "container_status": None,
        }

    return HandoffActionResult.model_validate(
        await mutate(
            session,
            principal,
            f"shipments.{shipment_id}.handoffs.pickup.withdraw",
            idempotency_key,
            request.model_dump(mode="json"),
            action,
            response,
            audit_details={
                "reason": request.reason,
                "physical_release_occurred": False,
                "containers_remain_at_origin": True,
            },
        )
    )


@router.post(
    "/{shipment_id}/containers/{container_id}/handoffs/delivery/confirm-delivery",
    response_model=HandoffActionResult,
    responses=DELIVERY_API_ERROR_RESPONSES,
    tags=["Handoffs"],
    operation_id="courierInitiateContainerDelivery",
    summary="Courier initiates one container delivery handoff",
)
async def courier_initiate_container_delivery(
    shipment_id: UUID,
    container_id: UUID,
    request: ContainerCodeRequest,
    response: Response,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=128)],
):
    preview = await session.get(Shipment, shipment_id)
    if (
        preview is None
        or preview.courier_user_id != principal.user_id
        or not principal.has_role("COURIER", region_id=preview.region_id)
    ):
        raise HTTPException(404, "Resource not found")
    if not await active_assigned_courier(session, preview):
        raise HTTPException(404, "Resource not found")

    async def action():
        return await initiate_container_delivery_command(
            session, principal, shipment_id, container_id, request.container_code
        )

    return HandoffActionResult.model_validate(
        await mutate(
            session,
            principal,
            f"shipments.{shipment_id}.containers.{container_id}.handoffs.delivery.confirm-delivery",
            idempotency_key,
            request.model_dump(mode="json"),
            action,
            response,
        )
    )


@router.post(
    "/{shipment_id}/containers/{container_id}/handoffs/delivery/confirm-receipt",
    response_model=HandoffActionResult,
    responses=DELIVERY_API_ERROR_RESPONSES,
    tags=["Handoffs"],
    operation_id="labConfirmContainerReceipt",
    summary="Destination lab confirms one container custody",
)
async def lab_confirm_container_receipt(
    shipment_id: UUID,
    container_id: UUID,
    request: ContainerCodeRequest,
    response: Response,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=128)],
):
    preview = await session.get(Shipment, shipment_id)
    if preview is None or not (
        principal.has_role("LAB_OPERATOR", organization_id=preview.destination_organization_id)
        or principal.has_role("LAB_MANAGER", organization_id=preview.destination_organization_id)
    ):
        raise HTTPException(404, "Resource not found")
    if not await active_destination_lab_member(session, preview, principal.user_id):
        raise HTTPException(404, "Resource not found")

    async def action():
        shipment, container = await lock_delivery_scope(
            session, principal, shipment_id, container_id, actor="LAB"
        )
        if not await active_destination_lab_member(session, shipment, principal.user_id):
            raise HTTPException(404, "Resource not found")
        if shipment.sealed_at is None or shipment.status not in {
            "DELIVERY_PENDING",
            "PARTIALLY_DELIVERED",
        }:
            raise HTTPException(409, "Shipment is not awaiting Delivery receipt")
        if container.delivery_status != "DELIVERY_PENDING":
            raise HTTPException(409, "Container has no pending Delivery")
        if request.container_code != container.code:
            raise HTTPException(409, "Container code does not match the sealed manifest")
        handoff = await session.scalar(
            select(Handoff)
            .where(
                Handoff.shipment_id == shipment.id,
                Handoff.container_id == container.id,
                Handoff.kind == "DELIVERY",
                Handoff.status == "PENDING",
            )
            .order_by(Handoff.attempt_no.desc())
            .with_for_update()
        )
        if handoff is None:
            raise HTTPException(409, "Container has no pending Delivery handoff")
        if handoff.initiated_by == principal.user_id:
            raise HTTPException(409, "Delivery receipt requires a different actor")
        now = await session.scalar(select(func.now()))
        handoff.status = "CONFIRMED"
        handoff.confirmed_by = principal.user_id
        handoff.confirmed_at = now
        await session.flush()
        evidence = ContainerReceiptEvidence(
            container_id=container.id,
            method="NORMAL",
            confirmed_delivery_handoff_id=handoff.id,
            verified_by=principal.user_id,
        )
        session.add(evidence)
        await session.flush()
        await session.refresh(evidence)
        container.delivery_status = "RECEIVED"
        container.received_at = now
        container.received_by = principal.user_id
        container.receipt_evidence = receipt_evidence_dict(evidence)
        container.state_version += 1
        await session.flush()
        open_session = await session.scalar(
            select(ReceivingSession).where(
                ReceivingSession.shipment_id == shipment.id,
                ReceivingSession.status == "OPEN",
            )
        )
        if open_session is None:
            prior_session = await session.scalar(
                select(ReceivingSession.id).where(ReceivingSession.shipment_id == shipment.id)
            )
            if prior_session is not None:
                raise HTTPException(409, "Receiving session must be reopened by a Lab Manager")
            session.add(
                ReceivingSession(
                    shipment_id=shipment.id,
                    sequence=1,
                    status="OPEN",
                    opened_by=principal.user_id,
                )
            )
        shipment.status = await derive_delivery_shipment_status(session, shipment.id)
        shipment.version += 1
        await session.flush()
        await session.refresh(shipment, attribute_names=["updated_at"])
        return 200, {
            "shipment_id": str(shipment.id),
            "shipment_status": shipment.status,
            "version": shipment.version,
            "handoff": handoff_dict(handoff),
            "container_id": str(container.id),
            "container_status": container.delivery_status,
        }

    return HandoffActionResult.model_validate(
        await mutate(
            session,
            principal,
            f"shipments.{shipment_id}.containers.{container_id}.handoffs.delivery.confirm-receipt",
            idempotency_key,
            request.model_dump(mode="json"),
            action,
            response,
        )
    )


@router.post(
    "/{shipment_id}/containers/{container_id}/handoffs/delivery/withdraw",
    response_model=HandoffActionResult,
    responses=DELIVERY_API_ERROR_RESPONSES,
    tags=["Handoffs"],
    operation_id="courierWithdrawContainerDelivery",
    summary="Courier withdraws a pending container delivery",
)
async def courier_withdraw_container_delivery(
    shipment_id: UUID,
    container_id: UUID,
    request: ReasonRequest,
    response: Response,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=128)],
):
    preview = await session.get(Shipment, shipment_id)
    if (
        preview is None
        or preview.courier_user_id != principal.user_id
        or not principal.has_role("COURIER", region_id=preview.region_id)
    ):
        raise HTTPException(404, "Resource not found")
    if not await active_assigned_courier(session, preview):
        raise HTTPException(404, "Resource not found")

    async def action():
        shipment, container = await lock_delivery_scope(
            session, principal, shipment_id, container_id, actor="COURIER"
        )
        if not await active_assigned_courier(session, shipment):
            raise HTTPException(404, "Resource not found")
        if shipment.status not in {"DELIVERY_PENDING", "PARTIALLY_DELIVERED"} or (
            container.delivery_status != "DELIVERY_PENDING"
        ):
            raise HTTPException(409, "Container has no pending Delivery to withdraw")
        handoff = await session.scalar(
            select(Handoff)
            .where(
                Handoff.shipment_id == shipment.id,
                Handoff.container_id == container.id,
                Handoff.kind == "DELIVERY",
                Handoff.status == "PENDING",
                Handoff.initiated_by == principal.user_id,
            )
            .order_by(Handoff.attempt_no.desc())
            .with_for_update()
        )
        if handoff is None:
            raise HTTPException(409, "Only the initiating Courier can withdraw pending Delivery")
        handoff.status = "WITHDRAWN"
        handoff.withdrawn_by = principal.user_id
        handoff.withdrawn_at = await session.scalar(select(func.now()))
        handoff.withdrawal_reason = request.reason
        await session.flush()
        container.delivery_status = "IN_TRANSIT"
        container.state_version += 1
        await session.flush()
        shipment.status = await derive_delivery_shipment_status(session, shipment.id)
        shipment.version += 1
        await session.flush()
        await session.refresh(shipment, attribute_names=["updated_at"])
        return 200, {
            "shipment_id": str(shipment.id),
            "shipment_status": shipment.status,
            "version": shipment.version,
            "handoff": handoff_dict(handoff),
            "container_id": str(container.id),
            "container_status": container.delivery_status,
        }

    return HandoffActionResult.model_validate(
        await mutate(
            session,
            principal,
            f"shipments.{shipment_id}.containers.{container_id}.handoffs.delivery.withdraw",
            idempotency_key,
            request.model_dump(mode="json"),
            action,
            response,
            audit_details={"reason": request.reason},
        )
    )


@router.get(
    "/{shipment_id}/containers/{container_id}/handoffs/delivery",
    response_model=DeliveryHandoffHistory,
    responses=DELIVERY_API_ERROR_RESPONSES,
    tags=["Handoffs"],
    operation_id="listContainerDeliveryHandoffs",
    summary="Read individual container handoff attempts",
)
async def list_container_delivery_handoffs(
    shipment_id: UUID,
    container_id: UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
):
    shipment = await session.get(Shipment, shipment_id)
    if shipment is None:
        raise HTTPException(404, "Resource not found")
    allowed = (
        principal.has_role("SENDER_OPERATOR", organization_id=shipment.sender_organization_id)
        or (
            shipment.courier_user_id == principal.user_id
            and principal.has_role("COURIER", region_id=shipment.region_id)
        )
        or principal.has_role("LAB_OPERATOR", organization_id=shipment.destination_organization_id)
        or principal.has_role("LAB_MANAGER", organization_id=shipment.destination_organization_id)
    )
    if not allowed:
        raise HTTPException(404, "Resource not found")
    container = await session.scalar(
        select(TransportContainer).where(
            TransportContainer.id == container_id,
            TransportContainer.shipment_id == shipment.id,
        )
    )
    if container is None:
        raise HTTPException(404, "Resource not found")
    items = list(
        await session.scalars(
            select(Handoff)
            .where(
                Handoff.shipment_id == shipment.id,
                Handoff.container_id == container.id,
                Handoff.kind == "DELIVERY",
            )
            .order_by(Handoff.attempt_no)
        )
    )
    return DeliveryHandoffHistory(
        container_id=container.id,
        items=[handoff_dict(item) for item in items],
    )


@router.post(
    "/{shipment_id}/cancel",
    response_model=ShipmentResponse,
    responses=API_ERROR_RESPONSES,
)
async def cancel_shipment(
    shipment_id: UUID,
    request: ReasonRequest,
    response: Response,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=128)],
):
    preview = await visible(session, principal, shipment_id)
    if not principal.has_role("SENDER_OPERATOR", organization_id=preview.sender_organization_id):
        raise HTTPException(404, "Resource not found")

    async def action():
        row = await session.scalar(
            select(Shipment).where(Shipment.id == shipment_id).with_for_update()
        )
        if row is None or not principal.has_role(
            "SENDER_OPERATOR", organization_id=row.sender_organization_id
        ):
            raise HTTPException(404, "Resource not found")
        if row.status not in {"DRAFT", "READY_FOR_PICKUP"}:
            raise HTTPException(409, "Shipment can only be cancelled before Pickup release")
        if await session.scalar(
            select(Handoff.id).where(
                Handoff.shipment_id == row.id,
                Handoff.kind == "PICKUP",
                Handoff.status.in_(["PENDING", "CONFIRMED"]),
            )
        ):
            raise HTTPException(409, "Withdraw pending Pickup before cancelling shipment")
        row.status = "CANCELLED"
        row.closed_at = await session.scalar(select(func.now()))
        row.closed_by = principal.user_id
        row.closure_reason = request.reason
        row.version += 1
        await session.flush()
        await session.refresh(row, attribute_names=["updated_at"])
        return 200, shipment_dict(row)

    return ShipmentResponse.model_validate(
        await mutate(
            session,
            principal,
            f"shipments.{shipment_id}.cancel",
            idempotency_key,
            request.model_dump(mode="json"),
            action,
            response,
            audit_details={"reason": request.reason},
        )
    )
