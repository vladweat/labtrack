"""Container discrepancy reports and scoped issue listing."""

from typing import Annotated
from uuid import UUID, uuid4

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
    ContainerIssue,
    Handoff,
    Shipment,
    TransportContainer,
)
from app.modules.shipments.models import (
    ContainerIssueResolutionProposal as IssueResolutionProposal,
)
from app.modules.shipments.router import derive_delivery_shipment_status, shipment_dict
from app.modules.shipments.schemas import (
    ContainerIssueResolutionProposal,
    CreateContainerIssueRequest,
    CreateContainerIssueResponse,
    CreateContainerResolutionProposalRequest,
    DecideContainerResolutionProposalRequest,
    DecideContainerResolutionProposalResponse,
    PagedContainerIssue,
    PagedContainerIssueResolutionProposal,
    WithdrawResolutionProposalRequest,
)

router = APIRouter(prefix="/shipments", tags=["container issues"])
ERRORS = {status: API_ERROR_RESPONSES[status] for status in (401, 403, 404, 409, 422, 503)}


def issue_dict(issue: ContainerIssue) -> dict:
    return {
        "id": str(issue.id),
        "shipment_id": str(issue.shipment_id),
        "container_id": str(issue.container_id),
        "kind": issue.kind,
        "reason_code": issue.reason_code,
        "comment": issue.comment,
        "status": issue.status,
        "reported_by": str(issue.reported_by),
        "reported_at": issue.reported_at.isoformat(),
        "resolved_by": str(issue.resolved_by) if issue.resolved_by else None,
        "resolved_at": issue.resolved_at.isoformat() if issue.resolved_at else None,
        "resolution": issue.resolution,
    }


def proposal_dict(proposal: IssueResolutionProposal) -> dict:
    return {
        "id": str(proposal.id),
        "issue_id": str(proposal.issue_id),
        "action": proposal.action,
        "reason": proposal.reason,
        "status": proposal.status,
        "proposed_by": str(proposal.proposed_by),
        "proposed_at": proposal.proposed_at.isoformat(),
        "decided_by": str(proposal.decided_by) if proposal.decided_by else None,
        "decided_at": proposal.decided_at.isoformat() if proposal.decided_at else None,
        "decision_reason": proposal.decision_reason,
        "withdrawn_by": str(proposal.withdrawn_by) if proposal.withdrawn_by else None,
        "withdrawn_at": proposal.withdrawn_at.isoformat() if proposal.withdrawn_at else None,
        "withdrawal_reason": proposal.withdrawal_reason,
    }


def container_dict(container: TransportContainer) -> dict:
    return {
        "id": str(container.id),
        "code": container.code,
        "shipment_id": str(container.shipment_id),
        "created_at": container.created_at.isoformat(),
        "delivery_status": container.delivery_status,
        "received_at": container.received_at.isoformat() if container.received_at else None,
        "received_by": str(container.received_by) if container.received_by else None,
        "state_version": container.state_version,
        "receipt_evidence": container.receipt_evidence,
    }


async def lock_issue_shipment(
    session: AsyncSession, principal: Principal, shipment_id: UUID
) -> Shipment:
    shipment = await session.scalar(
        select(Shipment).where(Shipment.id == shipment_id).with_for_update()
    )
    if shipment is None or not (
        principal.has_role("SENDER_OPERATOR", organization_id=shipment.sender_organization_id)
        or principal.has_role("LAB_OPERATOR", organization_id=shipment.destination_organization_id)
        or principal.has_role("LAB_MANAGER", organization_id=shipment.destination_organization_id)
        or (
            shipment.courier_user_id == principal.user_id
            and principal.has_role("COURIER", region_id=shipment.region_id)
        )
    ):
        raise HTTPException(404, "Resource not found")
    return shipment


async def visible_issue_shipment(
    session: AsyncSession, principal: Principal, shipment_id: UUID
) -> Shipment:
    shipment = await session.get(Shipment, shipment_id)
    if shipment is None or not (
        principal.has_role("SENDER_OPERATOR", organization_id=shipment.sender_organization_id)
        or principal.has_role("LAB_OPERATOR", organization_id=shipment.destination_organization_id)
        or principal.has_role("LAB_MANAGER", organization_id=shipment.destination_organization_id)
        or principal.has_role("REGIONAL_OPERATIONS", region_id=shipment.region_id)
        or (
            shipment.courier_user_id == principal.user_id
            and principal.has_role("COURIER", region_id=shipment.region_id)
        )
    ):
        raise HTTPException(404, "Resource not found")
    return shipment


async def active_assigned_courier(session: AsyncSession, shipment: Shipment) -> bool:
    return bool(
        await session.scalar(
            select(Membership.id)
            .join(User)
            .join(Region, Region.id == Membership.region_id)
            .where(
                Membership.user_id == shipment.courier_user_id,
                Membership.organization_id.is_(None),
                Membership.region_id == shipment.region_id,
                Membership.role == "COURIER",
                Membership.is_active.is_(True),
                User.is_active.is_(True),
                Region.is_active.is_(True),
            )
        )
    )


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


@router.post(
    "/{shipment_id}/container-issues",
    status_code=201,
    response_model=CreateContainerIssueResponse,
    responses=ERRORS,
    operation_id="reportContainerIssue",
)
async def report_container_issue(
    shipment_id: UUID,
    payload: CreateContainerIssueRequest,
    response: Response,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> dict:
    async def action() -> tuple[int, dict]:
        shipment = await lock_issue_shipment(session, principal, shipment_id)
        container = await session.scalar(
            select(TransportContainer)
            .where(
                TransportContainer.id == payload.container_id,
                TransportContainer.shipment_id == shipment.id,
            )
            .with_for_update()
        )
        if container is None:
            raise HTTPException(404, "Resource not found")
        is_courier = (
            shipment.courier_user_id == principal.user_id
            and principal.has_role("COURIER", region_id=shipment.region_id)
            and await active_assigned_courier(session, shipment)
        )
        is_lab_operator = principal.has_role(
            "LAB_OPERATOR", organization_id=shipment.destination_organization_id
        ) and await active_destination_lab_operator(session, shipment, principal.user_id)
        if not (is_courier and payload.kind == "MISSING") and not is_lab_operator:
            raise HTTPException(404, "Resource not found")
        if not shipment.sealed_at or shipment.status not in (
            "IN_TRANSIT",
            "DELIVERY_PENDING",
            "DELIVERY_BLOCKED",
            "PARTIALLY_DELIVERED",
        ):
            raise HTTPException(409, "Container issue requires a picked-up sealed Shipment")
        if container.delivery_status not in ("IN_TRANSIT", "DELIVERY_PENDING"):
            raise HTTPException(409, "Container is not eligible for a new issue")
        pickup_confirmed = await session.scalar(
            select(Handoff.id).where(
                Handoff.shipment_id == shipment.id,
                Handoff.kind == "PICKUP",
                Handoff.status == "CONFIRMED",
            )
        )
        if pickup_confirmed is None:
            raise HTTPException(409, "Confirmed Pickup is required to report a container issue")
        reason = await session.scalar(
            select(IssueReason).where(
                IssueReason.code == payload.reason_code,
                IssueReason.scope == "CONTAINER",
                IssueReason.kind == payload.kind,
                IssueReason.is_active.is_(True),
            )
        )
        if reason is None:
            raise HTTPException(409, "Reason code does not match the container issue")
        if await session.scalar(
            select(ContainerIssue.id).where(
                ContainerIssue.container_id == container.id, ContainerIssue.status == "OPEN"
            )
        ):
            raise HTTPException(409, "Container already has an open issue")
        issue = ContainerIssue(
            id=uuid4(),
            shipment_id=shipment.id,
            container_id=container.id,
            kind=payload.kind,
            reason_code=reason.code,
            comment=payload.comment,
            reported_by=principal.user_id,
        )
        session.add(issue)
        await session.flush()
        pending = await session.scalar(
            select(Handoff)
            .where(
                Handoff.container_id == container.id,
                Handoff.kind == "DELIVERY",
                Handoff.status == "PENDING",
            )
            .with_for_update()
        )
        if pending is not None:
            pending.status = "WITHDRAWN"
            pending.withdrawn_by = principal.user_id
            pending.withdrawn_at = func.now()
            pending.withdrawal_reason = f"Container issue {issue.id}: {payload.kind}"
            await session.flush()
        container.delivery_status = "BLOCKED"
        container.state_version += 1
        await session.flush()
        shipment.status = await derive_delivery_shipment_status(session, shipment.id)
        shipment.version += 1
        await session.flush()
        return 201, {
            "issue": issue_dict(issue),
            "shipment_id": str(shipment.id),
            "shipment_status": shipment.status,
        }

    status_code, result = await execute_idempotently(
        session,
        actor_id=principal.user_id,
        operation="shipment.container_issue.report",
        key=idempotency_key,
        request_context={"shipment_id": str(shipment_id)},
        request_data=payload.model_dump(mode="json"),
        action=action,
        audit_details={"shipment_id": str(shipment_id), "container_id": str(payload.container_id)},
    )
    response.status_code = status_code
    return result


@router.get(
    "/{shipment_id}/container-issues",
    response_model=PagedContainerIssue,
    responses=ERRORS,
    operation_id="listContainerIssues",
)
async def list_container_issues(
    shipment_id: UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict:
    shipment = await visible_issue_shipment(session, principal, shipment_id)
    total = await session.scalar(
        select(func.count())
        .select_from(ContainerIssue)
        .where(ContainerIssue.shipment_id == shipment.id)
    )
    rows = await session.scalars(
        select(ContainerIssue)
        .where(ContainerIssue.shipment_id == shipment.id)
        .order_by(ContainerIssue.reported_at.desc(), ContainerIssue.id)
        .limit(limit)
        .offset(offset)
    )
    return {
        "items": [issue_dict(issue) for issue in rows],
        "total": total or 0,
        "limit": limit,
        "offset": offset,
    }


async def lock_issue_and_container(
    session: AsyncSession,
    principal: Principal,
    shipment_id: UUID,
    issue_id: UUID,
) -> tuple[Shipment, ContainerIssue, TransportContainer]:
    shipment = await session.scalar(
        select(Shipment).where(Shipment.id == shipment_id).with_for_update()
    )
    if shipment is None or not (
        principal.has_role("SENDER_OPERATOR", organization_id=shipment.sender_organization_id)
        or principal.has_role("LAB_OPERATOR", organization_id=shipment.destination_organization_id)
        or principal.has_role("LAB_MANAGER", organization_id=shipment.destination_organization_id)
        or principal.has_role("REGIONAL_OPERATIONS", region_id=shipment.region_id)
        or (
            shipment.courier_user_id == principal.user_id
            and principal.has_role("COURIER", region_id=shipment.region_id)
        )
    ):
        raise HTTPException(404, "Resource not found")
    issue = await session.scalar(
        select(ContainerIssue)
        .where(ContainerIssue.id == issue_id, ContainerIssue.shipment_id == shipment.id)
        .with_for_update()
    )
    if issue is None:
        raise HTTPException(404, "Resource not found")
    container = await session.scalar(
        select(TransportContainer)
        .where(
            TransportContainer.id == issue.container_id,
            TransportContainer.shipment_id == shipment.id,
        )
        .with_for_update()
    )
    if container is None:
        raise HTTPException(404, "Resource not found")
    return shipment, issue, container


def require_open_blocked_issue(issue: ContainerIssue, container: TransportContainer) -> None:
    if issue.status != "OPEN" or container.delivery_status != "BLOCKED":
        raise HTTPException(409, "Container issue is no longer open and blocked")


@router.post(
    "/{shipment_id}/container-issues/{issue_id}/proposals",
    status_code=201,
    response_model=ContainerIssueResolutionProposal,
    responses=ERRORS,
    operation_id="proposeContainerIssueResolution",
)
async def propose_container_issue_resolution(
    shipment_id: UUID,
    issue_id: UUID,
    payload: CreateContainerResolutionProposalRequest,
    response: Response,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> dict:
    async def action() -> tuple[int, dict]:
        shipment, issue, container = await lock_issue_and_container(
            session, principal, shipment_id, issue_id
        )
        if not principal.has_role("REGIONAL_OPERATIONS", region_id=shipment.region_id):
            raise HTTPException(404, "Resource not found")
        require_open_blocked_issue(issue, container)
        if shipment.status not in ("DELIVERY_BLOCKED", "PARTIALLY_DELIVERED"):
            raise HTTPException(409, "Shipment is not in a resolvable delivery state")
        if not payload.reason.strip():
            raise HTTPException(422, "Proposal reason must not be blank")
        proposal = IssueResolutionProposal(
            issue_id=issue.id,
            action=payload.action,
            reason=payload.reason,
            proposed_by=principal.user_id,
        )
        session.add(proposal)
        await session.flush()
        await session.refresh(proposal)
        return 201, proposal_dict(proposal)

    status_code, result = await execute_idempotently(
        session,
        actor_id=principal.user_id,
        operation="shipment.container_issue.proposal.create",
        key=idempotency_key,
        request_context={
            "shipment_id": str(shipment_id),
            "issue_id": str(issue_id),
        },
        request_data=payload.model_dump(mode="json"),
        action=action,
        audit_details={"shipment_id": str(shipment_id), "issue_id": str(issue_id)},
    )
    response.status_code = status_code
    return result


@router.get(
    "/{shipment_id}/container-issues/{issue_id}/proposals",
    response_model=PagedContainerIssueResolutionProposal,
    responses=ERRORS,
    operation_id="listContainerIssueResolutionProposals",
)
async def list_container_issue_proposals(
    shipment_id: UUID,
    issue_id: UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict:
    shipment = await visible_issue_shipment(session, principal, shipment_id)
    issue = await session.scalar(
        select(ContainerIssue).where(
            ContainerIssue.id == issue_id, ContainerIssue.shipment_id == shipment.id
        )
    )
    if issue is None:
        raise HTTPException(404, "Resource not found")
    total = await session.scalar(
        select(func.count())
        .select_from(IssueResolutionProposal)
        .where(IssueResolutionProposal.issue_id == issue.id)
    )
    rows = await session.scalars(
        select(IssueResolutionProposal)
        .where(IssueResolutionProposal.issue_id == issue.id)
        .order_by(IssueResolutionProposal.proposed_at.desc(), IssueResolutionProposal.id)
        .limit(limit)
        .offset(offset)
    )
    return {
        "items": [proposal_dict(proposal) for proposal in rows],
        "total": total or 0,
        "limit": limit,
        "offset": offset,
    }


@router.post(
    "/{shipment_id}/container-issues/{issue_id}/proposals/{proposal_id}/decide",
    response_model=DecideContainerResolutionProposalResponse,
    responses=ERRORS,
    operation_id="decideContainerIssueResolution",
)
async def decide_container_issue_resolution(
    shipment_id: UUID,
    issue_id: UUID,
    proposal_id: UUID,
    payload: DecideContainerResolutionProposalRequest,
    response: Response,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> dict:
    async def action() -> tuple[int, dict]:
        shipment, issue, container = await lock_issue_and_container(
            session, principal, shipment_id, issue_id
        )
        if not principal.has_role(
            "LAB_MANAGER", organization_id=shipment.destination_organization_id
        ):
            raise HTTPException(404, "Resource not found")
        proposal = await session.scalar(
            select(IssueResolutionProposal)
            .where(
                IssueResolutionProposal.id == proposal_id,
                IssueResolutionProposal.issue_id == issue.id,
            )
            .with_for_update()
        )
        if proposal is None:
            raise HTTPException(404, "Resource not found")
        if proposal.proposed_by == principal.user_id:
            raise HTTPException(409, "Proposal author cannot decide the proposal")
        require_open_blocked_issue(issue, container)
        if proposal.status != "PENDING" or shipment.status not in (
            "DELIVERY_BLOCKED",
            "PARTIALLY_DELIVERED",
        ):
            raise HTTPException(409, "Proposal or Shipment state is stale")
        if payload.reason is not None and not payload.reason.strip():
            raise HTTPException(422, "Decision reason must not be blank")
        proposal.status = "APPROVED" if payload.decision == "APPROVE" else "REJECTED"
        proposal.decided_by = principal.user_id
        proposal.decided_at = func.now()
        proposal.decision_reason = payload.reason
        await session.flush()
        await session.refresh(proposal)
        if payload.decision == "APPROVE":
            issue.status = (
                "RESOLVED" if proposal.action == "RETRY_DELIVERY" else "CLOSED_UNDELIVERED"
            )
            issue.resolved_by = principal.user_id
            issue.resolved_at = func.now()
            issue.resolution = proposal.action
            await session.flush()
            await session.refresh(issue)
            container.delivery_status = (
                "IN_TRANSIT" if proposal.action == "RETRY_DELIVERY" else "UNDELIVERED_CLOSED"
            )
            container.state_version += 1
            await session.flush()
            shipment.status = await derive_delivery_shipment_status(session, shipment.id)
            shipment.version += 1
            await session.flush()
        result = {
            "proposal": proposal_dict(proposal),
            "issue": issue_dict(issue),
            "container": container_dict(container),
            "shipment": shipment_dict(shipment),
        }
        return 200, result

    status_code, result = await execute_idempotently(
        session,
        actor_id=principal.user_id,
        operation="shipment.container_issue.proposal.decide",
        key=idempotency_key,
        request_context={
            "shipment_id": str(shipment_id),
            "issue_id": str(issue_id),
            "proposal_id": str(proposal_id),
        },
        request_data=payload.model_dump(mode="json"),
        action=action,
        audit_details={
            "shipment_id": str(shipment_id),
            "issue_id": str(issue_id),
            "proposal_id": str(proposal_id),
        },
    )
    response.status_code = status_code
    return result


@router.post(
    "/{shipment_id}/container-issues/{issue_id}/proposals/{proposal_id}/withdraw",
    response_model=ContainerIssueResolutionProposal,
    responses=ERRORS,
    operation_id="withdrawContainerIssueResolutionProposal",
)
async def withdraw_container_issue_proposal(
    shipment_id: UUID,
    issue_id: UUID,
    proposal_id: UUID,
    payload: WithdrawResolutionProposalRequest,
    response: Response,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> dict:
    async def action() -> tuple[int, dict]:
        shipment, issue, container = await lock_issue_and_container(
            session, principal, shipment_id, issue_id
        )
        if not principal.has_role("REGIONAL_OPERATIONS", region_id=shipment.region_id):
            raise HTTPException(404, "Resource not found")
        require_open_blocked_issue(issue, container)
        proposal = await session.scalar(
            select(IssueResolutionProposal)
            .where(
                IssueResolutionProposal.id == proposal_id,
                IssueResolutionProposal.issue_id == issue.id,
            )
            .with_for_update()
        )
        if proposal is None:
            raise HTTPException(404, "Resource not found")
        if proposal.proposed_by != principal.user_id or proposal.status != "PENDING":
            raise HTTPException(409, "Only the original proposer can withdraw a pending proposal")
        if not payload.reason.strip():
            raise HTTPException(422, "Withdrawal reason must not be blank")
        proposal.status = "WITHDRAWN"
        proposal.withdrawn_by = principal.user_id
        proposal.withdrawn_at = func.now()
        proposal.withdrawal_reason = payload.reason
        await session.flush()
        await session.refresh(proposal)
        return 200, proposal_dict(proposal)

    status_code, result = await execute_idempotently(
        session,
        actor_id=principal.user_id,
        operation="shipment.container_issue.proposal.withdraw",
        key=idempotency_key,
        request_context={
            "shipment_id": str(shipment_id),
            "issue_id": str(issue_id),
            "proposal_id": str(proposal_id),
        },
        request_data=payload.model_dump(mode="json"),
        action=action,
        audit_details={
            "shipment_id": str(shipment_id),
            "issue_id": str(issue_id),
            "proposal_id": str(proposal_id),
        },
    )
    response.status_code = status_code
    return result
