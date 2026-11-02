"""Scoped custody incident review and first-party attestations."""

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
from app.modules.shipments.models import (
    ContainerReceiptEvidence,
    CustodyAttestation,
    CustodyIncident,
    Handoff,
    ReceivingSession,
    Shipment,
    TransportContainer,
)
from app.modules.shipments.router import derive_delivery_shipment_status, receipt_evidence_dict
from app.modules.shipments.schemas import (
    CustodyAttestationRequest,
    CustodyAttestationResponse,
    CustodyDeliveryAttestationRequest,
    CustodyIncidentRequest,
    CustodyIncidentResolutionRequest,
    CustodyIncidentResponse,
    CustodyPickupAttestationRequest,
    ExceptionalReceiptRequest,
    PagedCustodyIncident,
)

router = APIRouter(prefix="/shipments", tags=["custody incidents"])
ERRORS = {status: API_ERROR_RESPONSES[status] for status in (401, 403, 404, 409, 422, 503)}


PICKUP_KINDS = {
    "PICKUP_UNCONFIRMED",
    "COURIER_UNAVAILABLE_AFTER_PICKUP",
    "HANDOFF_STALLED",
    "OFFLINE_CONFLICT",
}
DELIVERY_KINDS = {
    "COURIER_UNAVAILABLE_AFTER_PICKUP",
    "COURIER_DELIVERY_CONFIRMATION_UNAVAILABLE",
    "HANDOFF_STALLED",
    "OFFLINE_CONFLICT",
}


def incident_dict(incident: CustodyIncident) -> dict:
    return {
        "id": str(incident.id),
        "shipment_id": str(incident.shipment_id),
        "container_id": str(incident.container_id) if incident.container_id else None,
        "handoff_id": str(incident.handoff_id) if incident.handoff_id else None,
        "phase": incident.phase,
        "kind": incident.kind,
        "status": incident.status,
        "reason": incident.reason,
        "opened_by": str(incident.opened_by),
        "opened_at": incident.opened_at.isoformat(),
        "resolved_by": str(incident.resolved_by) if incident.resolved_by else None,
        "resolved_at": incident.resolved_at.isoformat() if incident.resolved_at else None,
        "resolution": incident.resolution,
    }


def attestation_dict(attestation: CustodyAttestation) -> dict:
    return {
        "id": str(attestation.id),
        "incident_id": str(attestation.incident_id),
        "user_id": str(attestation.user_id),
        "organization_id": str(attestation.organization_id),
        "role_at_time": attestation.role_at_time,
        "container_code": attestation.container_code,
        "statement": attestation.statement,
        "recorded_at": attestation.recorded_at.isoformat(),
        "physically_present": attestation.physically_present,
        "physical_release_occurred": attestation.physical_release_occurred,
        "containers_at_origin": attestation.containers_at_origin,
    }


async def lock_incident_shipment(
    session: AsyncSession, principal: Principal, shipment_id: UUID
) -> Shipment:
    shipment = await session.scalar(
        select(Shipment).where(Shipment.id == shipment_id).with_for_update()
    )
    if shipment is None or not principal.has_role(
        "REGIONAL_OPERATIONS", region_id=shipment.region_id
    ):
        raise HTTPException(404, "Resource not found")
    return shipment


async def visible_incident_shipment(
    session: AsyncSession, principal: Principal, shipment_id: UUID
) -> Shipment:
    shipment = await session.get(Shipment, shipment_id)
    if shipment is None or not (
        principal.has_role("REGIONAL_OPERATIONS", region_id=shipment.region_id)
        or principal.has_role("SENDER_OPERATOR", organization_id=shipment.sender_organization_id)
        or principal.has_role("LAB_OPERATOR", organization_id=shipment.destination_organization_id)
        or principal.has_role("LAB_MANAGER", organization_id=shipment.destination_organization_id)
        or (
            shipment.courier_user_id == principal.user_id
            and principal.has_role("COURIER", region_id=shipment.region_id)
        )
    ):
        raise HTTPException(404, "Resource not found")
    return shipment


async def linked_handoff(
    session: AsyncSession, shipment: Shipment, payload: CustodyIncidentRequest
) -> Handoff | None:
    query = select(Handoff).where(Handoff.shipment_id == shipment.id)
    if payload.phase == "PICKUP":
        query = query.where(Handoff.kind == "PICKUP")
    else:
        query = query.where(
            Handoff.kind == "DELIVERY", Handoff.container_id == payload.container_id
        )
    query = query.where(Handoff.status.in_(("PENDING", "CONFIRMED"))).order_by(
        Handoff.attempt_no.desc()
    )
    return await session.scalar(query.limit(1))


async def current_lab_manager_attestation(
    session: AsyncSession, shipment: Shipment, incident: CustodyIncident, container_code: str
) -> UUID | None:
    return await session.scalar(
        select(CustodyAttestation.id)
        .join(Membership, Membership.user_id == CustodyAttestation.user_id)
        .join(User, User.id == Membership.user_id)
        .join(Organization, Organization.id == Membership.organization_id)
        .join(Region, Region.id == Organization.region_id)
        .where(
            CustodyAttestation.incident_id == incident.id,
            CustodyAttestation.organization_id == shipment.destination_organization_id,
            CustodyAttestation.role_at_time == "LAB_MANAGER",
            CustodyAttestation.container_code == container_code,
            CustodyAttestation.physically_present.is_(True),
            Membership.organization_id == shipment.destination_organization_id,
            Membership.role == "LAB_MANAGER",
            Membership.is_active.is_(True),
            User.is_active.is_(True),
            Organization.is_active.is_(True),
            Region.is_active.is_(True),
        )
    )


@router.post(
    "/{shipment_id}/custody-incidents",
    status_code=201,
    response_model=CustodyIncidentResponse,
    responses=ERRORS,
    operation_id="createCustodyIncident",
)
async def create_custody_incident(
    shipment_id: UUID,
    payload: CustodyIncidentRequest,
    response: Response,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> dict:
    async def action() -> tuple[int, dict]:
        shipment = await lock_incident_shipment(session, principal, shipment_id)
        if not payload.reason.strip():
            raise HTTPException(422, "Incident reason must not be blank")
        if payload.phase == "PICKUP":
            if payload.container_id is not None or payload.kind not in PICKUP_KINDS:
                raise HTTPException(409, "Pickup incident phase, kind, and container do not match")
            if shipment.sealed_at is None or shipment.status not in (
                "PICKUP_PENDING",
                "READY_FOR_PICKUP",
                "IN_TRANSIT",
            ):
                raise HTTPException(409, "Shipment has no eligible Pickup review state")
        else:
            if payload.container_id is None or payload.kind not in DELIVERY_KINDS:
                raise HTTPException(
                    409, "Delivery incident phase, kind, and container do not match"
                )
            if shipment.sealed_at is None or shipment.status not in (
                "IN_TRANSIT",
                "DELIVERY_PENDING",
                "DELIVERY_BLOCKED",
                "PARTIALLY_DELIVERED",
            ):
                raise HTTPException(409, "Shipment has no eligible Delivery review state")
            pickup = await session.scalar(
                select(Handoff.id).where(
                    Handoff.shipment_id == shipment.id,
                    Handoff.kind == "PICKUP",
                    Handoff.container_id.is_(None),
                    Handoff.status == "CONFIRMED",
                )
            )
            container = await session.scalar(
                select(TransportContainer)
                .where(
                    TransportContainer.id == payload.container_id,
                    TransportContainer.shipment_id == shipment.id,
                )
                .with_for_update()
            )
            if (
                pickup is None
                or container is None
                or container.delivery_status
                not in (
                    "IN_TRANSIT",
                    "DELIVERY_PENDING",
                )
            ):
                raise HTTPException(
                    409, "Confirmed Pickup and eligible target container are required"
                )
        handoff = await linked_handoff(session, shipment, payload)
        incident = CustodyIncident(
            shipment_id=shipment.id,
            container_id=payload.container_id,
            handoff_id=handoff.id if handoff else None,
            phase=payload.phase,
            kind=payload.kind,
            reason=payload.reason,
            opened_by=principal.user_id,
        )
        session.add(incident)
        await session.flush()
        await session.refresh(incident)
        return 201, incident_dict(incident)

    status_code, result = await execute_idempotently(
        session,
        actor_id=principal.user_id,
        operation="shipment.custody_incident.create",
        key=idempotency_key,
        request_context={"shipment_id": str(shipment_id)},
        request_data=payload.model_dump(mode="json"),
        action=action,
        audit_details={"shipment_id": str(shipment_id), "phase": payload.phase},
    )
    response.status_code = status_code
    return result


@router.get(
    "/{shipment_id}/custody-incidents",
    response_model=PagedCustodyIncident,
    responses=ERRORS,
    operation_id="listCustodyIncidents",
)
async def list_custody_incidents(
    shipment_id: UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict:
    shipment = await visible_incident_shipment(session, principal, shipment_id)
    total = await session.scalar(
        select(func.count())
        .select_from(CustodyIncident)
        .where(CustodyIncident.shipment_id == shipment.id)
    )
    incidents = await session.scalars(
        select(CustodyIncident)
        .where(CustodyIncident.shipment_id == shipment.id)
        .order_by(CustodyIncident.opened_at.desc(), CustodyIncident.id)
        .limit(limit)
        .offset(offset)
    )
    return {
        "items": [incident_dict(incident) for incident in incidents],
        "total": total or 0,
        "limit": limit,
        "offset": offset,
    }


@router.post(
    "/{shipment_id}/custody-incidents/{incident_id}/attest",
    status_code=201,
    response_model=CustodyAttestationResponse,
    responses=ERRORS,
    operation_id="attestCustodyIncident",
)
async def attest_custody_incident(
    shipment_id: UUID,
    incident_id: UUID,
    payload: CustodyAttestationRequest,
    response: Response,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> dict:
    async def action() -> tuple[int, dict]:
        shipment = await session.scalar(
            select(Shipment).where(Shipment.id == shipment_id).with_for_update()
        )
        incident = await session.scalar(
            select(CustodyIncident)
            .where(
                CustodyIncident.id == incident_id,
                CustodyIncident.shipment_id == shipment_id,
            )
            .with_for_update()
        )
        if shipment is None or incident is None:
            raise HTTPException(404, "Resource not found")
        if incident.status not in ("OPEN", "PENDING_EVIDENCE", "UNDER_REVIEW"):
            raise HTTPException(409, "Incident is no longer accepting attestations")
        if incident.phase == "PICKUP":
            if not isinstance(payload, CustodyPickupAttestationRequest) or not principal.has_role(
                "SENDER_OPERATOR", organization_id=shipment.sender_organization_id
            ):
                raise HTTPException(404, "Resource not found")
            role = "SENDER_OPERATOR"
            organization_id = shipment.sender_organization_id
            container_code = None
            physically_present = None
            physical_release_occurred = payload.physical_release_occurred
            containers_at_origin = payload.containers_at_origin
        else:
            if incident.container_id is None or not isinstance(
                payload, CustodyDeliveryAttestationRequest
            ):
                raise HTTPException(409, "Delivery incident requires a Delivery attestation")
            role = next(
                (
                    role
                    for role in ("LAB_MANAGER", "LAB_OPERATOR")
                    if principal.has_role(
                        role, organization_id=shipment.destination_organization_id
                    )
                ),
                None,
            )
            if role is None:
                raise HTTPException(404, "Resource not found")
            container = await session.scalar(
                select(TransportContainer)
                .where(
                    TransportContainer.id == incident.container_id,
                    TransportContainer.shipment_id == shipment.id,
                )
                .with_for_update()
            )
            if container is None or payload.container_code != container.code:
                raise HTTPException(409, "Attestation container code does not match the incident")
            organization_id = shipment.destination_organization_id
            container_code = container.code
            physically_present = True
            physical_release_occurred = None
            containers_at_origin = None
        if not payload.statement.strip():
            raise HTTPException(422, "Attestation statement must not be blank")
        record = CustodyAttestation(
            incident_id=incident.id,
            user_id=principal.user_id,
            organization_id=organization_id,
            role_at_time=role,
            container_code=container_code,
            statement=payload.statement,
            physically_present=physically_present,
            physical_release_occurred=physical_release_occurred,
            containers_at_origin=containers_at_origin,
        )
        session.add(record)
        await session.flush()
        await session.refresh(record)
        count = await session.scalar(
            select(func.count())
            .select_from(CustodyAttestation)
            .where(CustodyAttestation.incident_id == incident.id)
        )
        incident.status = "UNDER_REVIEW" if (count or 0) > 1 else "PENDING_EVIDENCE"
        await session.flush()
        return 201, attestation_dict(record)

    status_code, result = await execute_idempotently(
        session,
        actor_id=principal.user_id,
        operation="shipment.custody_incident.attest",
        key=idempotency_key,
        request_context={
            "shipment_id": str(shipment_id),
            "incident_id": str(incident_id),
        },
        request_data=payload.model_dump(mode="json"),
        action=action,
        audit_details={"shipment_id": str(shipment_id), "incident_id": str(incident_id)},
    )
    response.status_code = status_code
    return result


@router.post(
    "/{shipment_id}/containers/{container_id}/exceptional-receipts",
    status_code=201,
    response_model=CustodyIncidentResponse,
    responses=ERRORS,
    operation_id="startExceptionalContainerReceipt",
)
async def start_exceptional_container_receipt(
    shipment_id: UUID,
    container_id: UUID,
    payload: ExceptionalReceiptRequest,
    response: Response,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> dict:
    async def action() -> tuple[int, dict]:
        shipment = await lock_incident_shipment(session, principal, shipment_id)
        if not payload.reason.strip():
            raise HTTPException(422, "Incident reason must not be blank")
        if shipment.sealed_at is None or shipment.status not in (
            "IN_TRANSIT",
            "DELIVERY_PENDING",
            "DELIVERY_BLOCKED",
            "PARTIALLY_DELIVERED",
        ):
            raise HTTPException(409, "Exceptional receipt requires a sealed active Shipment")
        pickup = await session.scalar(
            select(Handoff.id).where(
                Handoff.shipment_id == shipment.id,
                Handoff.kind == "PICKUP",
                Handoff.container_id.is_(None),
                Handoff.status == "CONFIRMED",
            )
        )
        container = await session.scalar(
            select(TransportContainer)
            .where(
                TransportContainer.id == container_id,
                TransportContainer.shipment_id == shipment.id,
            )
            .with_for_update()
        )
        if (
            pickup is None
            or container is None
            or container.delivery_status
            not in (
                "IN_TRANSIT",
                "DELIVERY_PENDING",
            )
        ):
            raise HTTPException(409, "Confirmed Pickup and eligible target container are required")
        handoff = await session.scalar(
            select(Handoff)
            .where(
                Handoff.shipment_id == shipment.id,
                Handoff.container_id == container.id,
                Handoff.kind == "DELIVERY",
                Handoff.status == "PENDING",
            )
            .order_by(Handoff.attempt_no.desc())
            .limit(1)
        )
        incident = CustodyIncident(
            shipment_id=shipment.id,
            container_id=container.id,
            handoff_id=handoff.id if handoff else None,
            phase="DELIVERY",
            kind="COURIER_DELIVERY_CONFIRMATION_UNAVAILABLE",
            reason=payload.reason,
            opened_by=principal.user_id,
        )
        session.add(incident)
        await session.flush()
        await session.refresh(incident)
        return 201, incident_dict(incident)

    status_code, result = await execute_idempotently(
        session,
        actor_id=principal.user_id,
        operation="shipment.container.exceptional_receipt.start",
        key=idempotency_key,
        request_context={
            "shipment_id": str(shipment_id),
            "container_id": str(container_id),
        },
        request_data={"container_id": str(container_id), **payload.model_dump(mode="json")},
        action=action,
        audit_details={"shipment_id": str(shipment_id), "container_id": str(container_id)},
    )
    response.status_code = status_code
    return result


@router.post(
    "/{shipment_id}/custody-incidents/{incident_id}/resolve",
    response_model=CustodyIncidentResponse,
    responses=ERRORS,
    operation_id="resolveCustodyIncident",
)
async def resolve_custody_incident(
    shipment_id: UUID,
    incident_id: UUID,
    payload: CustodyIncidentResolutionRequest,
    response: Response,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> dict:
    async def action() -> tuple[int, dict]:
        shipment = await session.scalar(
            select(Shipment).where(Shipment.id == shipment_id).with_for_update()
        )
        if shipment is None or not principal.has_role(
            "REGIONAL_OPERATIONS", region_id=shipment.region_id
        ):
            raise HTTPException(404, "Resource not found")
        incident_scope = await session.scalar(
            select(CustodyIncident).where(
                CustodyIncident.id == incident_id,
                CustodyIncident.shipment_id == shipment.id,
            )
        )
        if incident_scope is None:
            raise HTTPException(404, "Resource not found")
        container = None
        if incident_scope.container_id is not None:
            container = await session.scalar(
                select(TransportContainer)
                .where(
                    TransportContainer.id == incident_scope.container_id,
                    TransportContainer.shipment_id == shipment.id,
                )
                .with_for_update()
            )
            if container is None:
                raise HTTPException(409, "Incident target container no longer exists")
        incident = await session.scalar(
            select(CustodyIncident)
            .where(
                CustodyIncident.id == incident_id,
                CustodyIncident.shipment_id == shipment.id,
            )
            .with_for_update()
        )
        if incident is None:
            raise HTTPException(404, "Resource not found")
        if incident.container_id != incident_scope.container_id:
            raise HTTPException(409, "Incident target changed during resolution")
        if incident.status not in ("OPEN", "PENDING_EVIDENCE", "UNDER_REVIEW"):
            raise HTTPException(409, "Incident has already been resolved")
        if not payload.reason.strip():
            raise HTTPException(422, "Resolution reason must not be blank")

        evidence = None
        delivery_handoff = None
        witness_ids: list[UUID] = []
        if payload.resolution == "EXCEPTIONAL_RECEIPT_VERIFIED":
            if (
                incident.phase != "DELIVERY"
                or incident.kind
                not in (
                    "COURIER_UNAVAILABLE_AFTER_PICKUP",
                    "COURIER_DELIVERY_CONFIRMATION_UNAVAILABLE",
                )
                or incident.container_id is None
            ):
                raise HTTPException(409, "Incident is not eligible for exceptional receipt")
            if shipment.sealed_at is None:
                raise HTTPException(409, "Exceptional receipt requires a sealed Shipment")
            pickup_confirmed = await session.scalar(
                select(Handoff.id).where(
                    Handoff.shipment_id == shipment.id,
                    Handoff.kind == "PICKUP",
                    Handoff.container_id.is_(None),
                    Handoff.status == "CONFIRMED",
                )
            )
            if (
                pickup_confirmed is None
                or container is None
                or container.delivery_status
                not in (
                    "IN_TRANSIT",
                    "DELIVERY_PENDING",
                )
            ):
                raise HTTPException(409, "Confirmed Pickup and eligible container are required")
            if await session.scalar(
                select(ContainerReceiptEvidence.id).where(
                    ContainerReceiptEvidence.container_id == container.id
                )
            ):
                raise HTTPException(409, "Container already has verified receipt evidence")
            witnesses = list(
                await session.execute(
                    select(CustodyAttestation, Membership)
                    .join(Membership, Membership.user_id == CustodyAttestation.user_id)
                    .join(User, User.id == Membership.user_id)
                    .join(Organization, Organization.id == Membership.organization_id)
                    .join(Region, Region.id == Organization.region_id)
                    .where(
                        CustodyAttestation.incident_id == incident.id,
                        CustodyAttestation.organization_id == shipment.destination_organization_id,
                        CustodyAttestation.container_code == container.code,
                        CustodyAttestation.physically_present.is_(True),
                        Membership.organization_id == shipment.destination_organization_id,
                        Membership.role == CustodyAttestation.role_at_time,
                        Membership.role.in_(("LAB_OPERATOR", "LAB_MANAGER")),
                        Membership.is_active.is_(True),
                        User.is_active.is_(True),
                        Organization.is_active.is_(True),
                        Region.is_active.is_(True),
                    )
                )
            )
            for attestation, _membership in witnesses:
                if attestation.user_id not in witness_ids:
                    witness_ids.append(attestation.user_id)
            manager_witness = any(
                attestation.role_at_time == "LAB_MANAGER" for attestation, _membership in witnesses
            )
            if len(witness_ids) < 2 or not manager_witness:
                raise HTTPException(
                    409, "Two current Lab witnesses including one Lab Manager are required"
                )
            if principal.user_id in witness_ids:
                raise HTTPException(409, "Regional resolver cannot be an attesting Lab witness")
            delivery_handoff = await session.scalar(
                select(Handoff)
                .where(
                    Handoff.container_id == container.id,
                    Handoff.shipment_id == shipment.id,
                    Handoff.kind == "DELIVERY",
                    Handoff.status == "PENDING",
                )
                .with_for_update()
            )
            if delivery_handoff is not None and incident.handoff_id != delivery_handoff.id:
                raise HTTPException(
                    409, "Active Delivery attempt changed after the incident opened"
                )
        elif payload.resolution == "NO_PHYSICAL_TRANSFER":
            if incident.phase != "PICKUP" or shipment.status not in (
                "READY_FOR_PICKUP",
                "PICKUP_PENDING",
            ):
                raise HTTPException(
                    409,
                    "NO_PHYSICAL_TRANSFER is only valid before Pickup has been confirmed",
                )
            confirmed_pickup = await session.scalar(
                select(Handoff.id).where(
                    Handoff.shipment_id == shipment.id,
                    Handoff.kind == "PICKUP",
                    Handoff.container_id.is_(None),
                    Handoff.status == "CONFIRMED",
                )
            )
            if confirmed_pickup is not None:
                raise HTTPException(409, "NO_PHYSICAL_TRANSFER cannot reverse a confirmed Pickup")
            sender_attested = await session.scalar(
                select(CustodyAttestation.id)
                .join(Membership, Membership.user_id == CustodyAttestation.user_id)
                .join(User, User.id == CustodyAttestation.user_id)
                .join(Organization, Organization.id == Membership.organization_id)
                .join(Region, Region.id == Organization.region_id)
                .where(
                    CustodyAttestation.incident_id == incident.id,
                    CustodyAttestation.organization_id == shipment.sender_organization_id,
                    CustodyAttestation.role_at_time == "SENDER_OPERATOR",
                    CustodyAttestation.physical_release_occurred.is_(False),
                    CustodyAttestation.containers_at_origin.is_(True),
                    Membership.organization_id == shipment.sender_organization_id,
                    Membership.role == "SENDER_OPERATOR",
                    Membership.is_active.is_(True),
                    User.is_active.is_(True),
                    Organization.is_active.is_(True),
                    Region.is_active.is_(True),
                )
            )
            if sender_attested is None:
                raise HTTPException(
                    409, "Active Sender attestation must confirm no physical transfer"
                )
        elif payload.resolution == "CONTAINER_ISSUE_REFERRED":
            if (
                incident.phase != "DELIVERY"
                or incident.container_id is None
                or container is None
                or container.delivery_status not in ("IN_TRANSIT", "DELIVERY_PENDING")
            ):
                raise HTTPException(409, "Container issue referral requires a Delivery incident")
            if (
                await current_lab_manager_attestation(session, shipment, incident, container.code)
                is None
            ):
                raise HTTPException(
                    409, "An active destination Lab Manager attestation is required for referral"
                )
        elif payload.resolution != "ESCALATED_NO_CUSTODY_CHANGE":
            raise HTTPException(409, "Incident outcome is incompatible with its phase")

        incident.status = "RESOLVED"
        incident.resolved_by = principal.user_id
        incident.resolved_at = func.now()
        incident.resolution = payload.resolution
        await session.flush()
        await session.refresh(incident)

        if payload.resolution == "EXCEPTIONAL_RECEIPT_VERIFIED":
            assert container is not None
            if delivery_handoff is not None:
                delivery_handoff.status = "SUPERSEDED"
                delivery_handoff.superseded_by_incident_id = incident.id
                delivery_handoff.superseded_at = func.now()
                await session.flush()
            evidence = ContainerReceiptEvidence(
                container_id=container.id,
                method="EXCEPTIONAL",
                resolved_incident_id=incident.id,
                verified_by=principal.user_id,
            )
            session.add(evidence)
            await session.flush()
            await session.refresh(evidence)
            now = await session.scalar(select(func.now()))
            container.delivery_status = "RECEIVED"
            container.received_at = now
            container.received_by = None
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

        return 200, incident_dict(incident)

    status_code, result = await execute_idempotently(
        session,
        actor_id=principal.user_id,
        operation="shipment.custody_incident.resolve",
        key=idempotency_key,
        request_context={
            "shipment_id": str(shipment_id),
            "incident_id": str(incident_id),
        },
        request_data=payload.model_dump(mode="json"),
        action=action,
        audit_details={
            "shipment_id": str(shipment_id),
            "incident_id": str(incident_id),
            "resolution": payload.resolution,
            "reason": payload.reason,
        },
    )
    response.status_code = status_code
    return result
