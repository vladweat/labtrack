"""Draft shipment and sealed-manifest endpoints."""

from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.db.session import get_db_session
from app.modules.idempotency.service import execute_idempotently
from app.modules.identity.access import Principal, get_current_principal
from app.modules.identity.models import Membership, Organization, User
from app.modules.samples.models import Sample
from app.modules.shipments.models import ContainerItem, Shipment, TransportContainer
from app.modules.shipments.schemas import (
    AddSampleRequest,
    AssignCourierRequest,
    ContainerItemWriteResponse,
    CreateShipmentRequest,
    ManifestResponse,
    PagedShipmentResponse,
    ShipmentResponse,
    TransportContainerResponse,
    UpdateDraftShipmentRequest,
)

router = APIRouter(prefix="/shipments", tags=["shipments"])


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
        "closed_at": None,
        "closed_by": None,
        "closure_reason": None,
    }


async def visible(session: AsyncSession, principal: Principal, shipment_id: UUID) -> Shipment:
    row = await session.get(Shipment, shipment_id)
    if row is None or not (
        principal.has_role("SENDER_OPERATOR", organization_id=row.sender_organization_id)
        or principal.has_role("LAB_MANAGER", organization_id=row.destination_organization_id)
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
                "receipt_evidence_method": None,
            }
        )
    return {
        "shipment_id": str(row.id),
        "status": row.status,
        "sealed_at": row.sealed_at.isoformat() if row.sealed_at else None,
        "manifest_revision": row.manifest_revision,
        "containers": values,
    }


async def mutate(session, principal, operation, key, payload, action, response):
    code, body = await execute_idempotently(
        session,
        actor_id=principal.user_id,
        operation=operation,
        key=key,
        request_data=payload,
        action=action,
    )
    response.status_code = code
    return body


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
