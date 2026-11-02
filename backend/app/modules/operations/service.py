"""Query live PostgreSQL state, independently of notification delivery."""

from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import (
    Integer,
    Uuid,
    and_,
    case,
    cast,
    false,
    func,
    literal,
    or_,
    select,
    true,
    union_all,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.core.config import Settings
from app.modules.identity.access import Principal
from app.modules.identity.models import Membership, Organization, Region, User
from app.modules.operations.schemas import PagedStalledShipment, StalledShipment
from app.modules.shipments.models import (
    ContainerIssue,
    ContainerIssueResolutionProposal,
    Handoff,
    Shipment,
    TransportContainer,
)

TERMINAL_STATUSES = ("COMPLETED", "COMPLETED_WITH_ISSUES", "CANCELLED", "CLOSED_UNDELIVERED")
PARTICIPANT_ROLES = {"SENDER_OPERATOR", "COURIER", "LAB_OPERATOR", "LAB_MANAGER"}
CASE_TYPES = ("PICKUP_PENDING", "DELIVERY_PENDING", "PROPOSAL_PENDING", "SHIPMENT_OVERDUE")
PUBLIC_CASE_COLUMNS = (
    "shipment_id",
    "status",
    "region_id",
    "last_event_at",
    "age_seconds",
    "case_type",
    "container_id",
    "proposal_id",
)


@dataclass(frozen=True)
class StalledNotificationCandidate:
    """One qualified business case and its current, safely scoped recipients."""

    case_type: str
    event_id: UUID
    recipient_user_ids: tuple[UUID, ...]
    payload: dict[str, str]


def case_visibility(principal: Principal, case_type: str) -> ColumnElement[bool]:
    """Authorize each stage separately, so a role never becomes a regional admin."""

    predicates: list[ColumnElement[bool]] = []
    for grant in principal.memberships:
        in_region = Shipment.region_id == grant.region_id
        if grant.role == "REGIONAL_OPERATIONS":
            predicates.append(in_region)
        elif grant.role == "SENDER_OPERATOR" and case_type in {
            "PICKUP_PENDING",
            "SHIPMENT_OVERDUE",
        }:
            if grant.organization_id is not None:
                predicates.append(
                    and_(in_region, Shipment.sender_organization_id == grant.organization_id)
                )
        elif grant.role == "COURIER" and case_type != "PROPOSAL_PENDING":
            predicates.append(and_(in_region, Shipment.courier_user_id == principal.user_id))
        elif grant.role in {"LAB_OPERATOR", "LAB_MANAGER"} and case_type in {
            "DELIVERY_PENDING",
            "SHIPMENT_OVERDUE",
        }:
            if grant.organization_id is not None:
                predicates.append(
                    and_(in_region, Shipment.destination_organization_id == grant.organization_id)
                )
        elif grant.role == "LAB_MANAGER" and case_type == "PROPOSAL_PENDING":
            if grant.organization_id is not None:
                predicates.append(
                    and_(in_region, Shipment.destination_organization_id == grant.organization_id)
                )
    return or_(*predicates) if predicates else false()


def _stalled_case_cte(
    settings: Settings,
    *,
    principal: Principal | None = None,
    region_id: UUID | None = None,
    older_than_seconds: int | None = None,
):
    """Build the shared threshold-qualified business cases for dashboard and worker."""

    now = func.now()
    base_conditions = [Shipment.status.not_in(TERMINAL_STATUSES)]
    if region_id is not None:
        base_conditions.append(Shipment.region_id == region_id)

    def columns(
        case_type,
        event_at,
        event_id,
        *,
        container_id=None,
        proposal_id=None,
        proposed_by=None,
    ):
        return (
            Shipment.id.label("shipment_id"),
            Shipment.status.label("status"),
            Shipment.region_id.label("region_id"),
            event_at.label("last_event_at"),
            cast(func.floor(func.extract("epoch", now - event_at)), Integer).label("age_seconds"),
            literal(case_type).label("case_type"),
            event_id.label("event_id"),
            (container_id if container_id is not None else cast(literal(None), Uuid)).label(
                "container_id"
            ),
            (proposal_id if proposal_id is not None else cast(literal(None), Uuid)).label(
                "proposal_id"
            ),
            Shipment.sender_organization_id.label("sender_organization_id"),
            Shipment.destination_organization_id.label("destination_organization_id"),
            Shipment.courier_user_id.label("courier_user_id"),
            (proposed_by if proposed_by is not None else cast(literal(None), Uuid)).label(
                "proposed_by"
            ),
        )

    def old_enough(event_at, threshold_seconds):
        threshold = max(threshold_seconds, older_than_seconds or 0)
        return event_at <= now - timedelta(seconds=threshold)

    def visible(selectable, case_type):
        if principal is None:
            return selectable
        return selectable.where(case_visibility(principal, case_type))

    pickup = visible(
        select(*columns("PICKUP_PENDING", Handoff.initiated_at, Handoff.id))
        .join(Handoff, Handoff.shipment_id == Shipment.id)
        .where(
            *base_conditions,
            Shipment.status == "PICKUP_PENDING",
            Handoff.kind == "PICKUP",
            Handoff.status == "PENDING",
            old_enough(Handoff.initiated_at, settings.stalled_pickup_seconds),
        ),
        "PICKUP_PENDING",
    )
    delivery = visible(
        select(
            *columns(
                "DELIVERY_PENDING",
                Handoff.initiated_at,
                Handoff.id,
                container_id=Handoff.container_id,
            )
        )
        .join(Handoff, Handoff.shipment_id == Shipment.id)
        .join(TransportContainer, TransportContainer.id == Handoff.container_id)
        .where(
            *base_conditions,
            Handoff.kind == "DELIVERY",
            Handoff.status == "PENDING",
            TransportContainer.delivery_status == "DELIVERY_PENDING",
            old_enough(Handoff.initiated_at, settings.stalled_delivery_seconds),
        ),
        "DELIVERY_PENDING",
    )
    proposal = visible(
        select(
            *columns(
                "PROPOSAL_PENDING",
                ContainerIssueResolutionProposal.proposed_at,
                ContainerIssueResolutionProposal.id,
                container_id=ContainerIssue.container_id,
                proposal_id=ContainerIssueResolutionProposal.id,
                proposed_by=ContainerIssueResolutionProposal.proposed_by,
            )
        )
        .join(ContainerIssue, ContainerIssue.shipment_id == Shipment.id)
        .join(
            ContainerIssueResolutionProposal,
            ContainerIssueResolutionProposal.issue_id == ContainerIssue.id,
        )
        .where(
            *base_conditions,
            ContainerIssue.status == "OPEN",
            ContainerIssueResolutionProposal.status == "PENDING",
            old_enough(
                ContainerIssueResolutionProposal.proposed_at, settings.stalled_proposal_seconds
            ),
        ),
        "PROPOSAL_PENDING",
    )
    overdue = visible(
        select(
            *columns(
                "SHIPMENT_OVERDUE",
                Shipment.expected_delivery_at,
                Shipment.id,
            )
        ).where(
            *base_conditions,
            Shipment.expected_delivery_at < now,
            old_enough(Shipment.expected_delivery_at, 0),
        ),
        "SHIPMENT_OVERDUE",
    )
    return union_all(pickup, delivery, proposal, overdue).cte("stalled_cases")


async def list_stalled_cases(
    session: AsyncSession,
    principal: Principal,
    settings: Settings,
    *,
    region_id: UUID | None,
    older_than_seconds: int | None,
    limit: int,
    offset: int,
) -> PagedStalledShipment:
    """Return current cases from PostgreSQL, independent of notification state."""

    regions = {
        grant.region_id
        for grant in principal.memberships
        if grant.role in PARTICIPANT_ROLES | {"REGIONAL_OPERATIONS"}
    }
    if not regions:
        raise HTTPException(403, "Insufficient role")
    if region_id is not None and region_id not in regions:
        raise HTTPException(404, "Resource not found")

    cases = _stalled_case_cte(
        settings,
        principal=principal,
        region_id=region_id,
        older_than_seconds=older_than_seconds,
    )
    public_columns = [getattr(cases.c, name) for name in PUBLIC_CASE_COLUMNS]
    page = (
        select(*public_columns)
        .order_by(
            cases.c.last_event_at,
            cases.c.case_type,
            cases.c.shipment_id,
            cases.c.container_id,
            cases.c.proposal_id,
        )
        .limit(limit)
        .offset(offset)
        .subquery("case_page")
    )
    totals = select(func.count().label("total")).select_from(cases).subquery("case_total")
    # The outer join returns the count even for an empty/out-of-range page. Both
    # count and page observe the same PostgreSQL statement snapshot.
    result = await session.execute(
        select(totals.c.total, *page.c)
        .select_from(totals.outerjoin(page, true()))
        .order_by(
            page.c.last_event_at,
            page.c.case_type,
            page.c.shipment_id,
            page.c.container_id,
            page.c.proposal_id,
        )
    )
    rows = result.mappings().all()
    return PagedStalledShipment(
        items=[
            StalledShipment.model_validate({key: row[key] for key in PUBLIC_CASE_COLUMNS})
            for row in rows
            if row["shipment_id"] is not None
        ],
        total=rows[0]["total"],
        limit=limit,
        offset=offset,
    )


async def list_notification_candidates(
    session: AsyncSession, settings: Settings
) -> list[StalledNotificationCandidate]:
    """Resolve current active, scoped notification recipients without a Principal."""

    cases = _stalled_case_cte(settings)
    membership_region_id = case(
        (Membership.organization_id.is_not(None), Organization.region_id),
        else_=Membership.region_id,
    )
    sender_operator = and_(
        cases.c.case_type.in_(("PICKUP_PENDING", "SHIPMENT_OVERDUE")),
        Membership.role == "SENDER_OPERATOR",
        Membership.organization_id == cases.c.sender_organization_id,
    )
    assigned_courier = and_(
        cases.c.case_type.in_(("PICKUP_PENDING", "DELIVERY_PENDING", "SHIPMENT_OVERDUE")),
        cases.c.courier_user_id == Membership.user_id,
        Membership.role == "COURIER",
    )
    destination_lab = and_(
        cases.c.case_type.in_(("DELIVERY_PENDING", "SHIPMENT_OVERDUE")),
        Membership.role.in_(("LAB_OPERATOR", "LAB_MANAGER")),
        Membership.organization_id == cases.c.destination_organization_id,
    )
    destination_manager_for_proposal = and_(
        cases.c.case_type == "PROPOSAL_PENDING",
        Membership.role == "LAB_MANAGER",
        Membership.organization_id == cases.c.destination_organization_id,
    )
    regional_operations = and_(
        cases.c.case_type.in_(CASE_TYPES),
        Membership.role == "REGIONAL_OPERATIONS",
    )
    recipient_membership = or_(
        sender_operator,
        assigned_courier,
        destination_lab,
        destination_manager_for_proposal,
        regional_operations,
    )
    rows = await session.execute(
        select(*[cases.c[name] for name in cases.c.keys()], User.id.label("recipient_user_id"))
        .select_from(cases)
        .join(Membership, recipient_membership)
        .join(User, User.id == Membership.user_id)
        .outerjoin(Organization, Organization.id == Membership.organization_id)
        .join(
            Region,
            and_(Region.id == membership_region_id, Region.id == cases.c.region_id),
        )
        .where(
            User.is_active.is_(True),
            Membership.is_active.is_(True),
            Region.is_active.is_(True),
            or_(Membership.organization_id.is_(None), Organization.is_active.is_(True)),
        )
        .distinct()
    )

    grouped: dict[tuple[str, UUID], dict] = {}
    for row in rows.mappings():
        key = (row["case_type"], row["event_id"])
        case_data = grouped.setdefault(
            key,
            {
                "case_type": row["case_type"],
                "event_id": row["event_id"],
                "recipients": set(),
                "payload": {
                    "case_type": row["case_type"],
                    "shipment_id": str(row["shipment_id"]),
                    "region_id": str(row["region_id"]),
                    "status": row["status"],
                    "last_event_at": row["last_event_at"].isoformat(),
                },
            },
        )
        if row["container_id"] is not None:
            case_data["payload"]["container_id"] = str(row["container_id"])
        if row["proposal_id"] is not None:
            case_data["payload"]["proposal_id"] = str(row["proposal_id"])
        case_data["recipients"].add(row["recipient_user_id"])

    return [
        StalledNotificationCandidate(
            case_type=case_data["case_type"],
            event_id=case_data["event_id"],
            recipient_user_ids=tuple(sorted(case_data["recipients"], key=str)),
            payload=case_data["payload"],
        )
        for case_data in grouped.values()
    ]
