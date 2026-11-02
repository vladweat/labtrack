"""PostgreSQL aggregates over one current-state row per Shipment.

The cohort and its UTC calendar bucket are based on Shipment.created_at. Receipt
revisions and receiving sessions are deliberately not joined: ContainerItem is
the transactional latest-result projection. Container discrepancy counts use the
latest recorded issue per container, including a resolved incident, rather than
counting repeated reports or proposal decisions as new containers.
"""

from datetime import datetime, timedelta
from typing import Literal
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import DateTime, and_, case, func, literal_column, or_, select, union_all
from sqlalchemy.dialects.postgresql import JSONB, aggregate_order_by
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select

from app.modules.analytics.schemas import ShipmentSummary
from app.modules.identity.access import Principal
from app.modules.identity.models import Organization
from app.modules.shipments.models import (
    ContainerIssue,
    ContainerItem,
    Handoff,
    Shipment,
    TransportContainer,
)

TERMINAL_STATUSES = ("COMPLETED", "COMPLETED_WITH_ISSUES", "CANCELLED", "CLOSED_UNDELIVERED")
COMPLETED_STATUSES = ("COMPLETED", "COMPLETED_WITH_ISSUES")
ANALYTICS_ROLES = {"REGIONAL_ANALYST", "REGIONAL_OPERATIONS"}


def analytics_region(principal: Principal, region_id: UUID | None) -> UUID:
    """An omitted filter is unambiguous only for a single authorized region."""

    regions = {grant.region_id for grant in principal.memberships if grant.role in ANALYTICS_ROLES}
    if not regions:
        raise HTTPException(403, "Insufficient role")
    if region_id is not None:
        if region_id not in regions:
            raise HTTPException(404, "Resource not found")
        return region_id
    if len(regions) != 1:
        raise HTTPException(422, "region_id is required for multiple authorized regions")
    return next(iter(regions))


def summary_statement(
    *,
    region_id: UUID,
    organization_id: UUID | None,
    from_at: datetime | None,
    to_at: datetime | None,
    granularity: Literal["day", "week"],
) -> Select:
    """Build one snapshot-consistent statement, keeping every denominator explicit."""

    cohort_query = select(
        Shipment.id,
        Shipment.status,
        Shipment.sender_organization_id,
        Shipment.destination_organization_id,
        Shipment.expected_delivery_at,
        Shipment.created_at,
    ).where(Shipment.region_id == region_id)
    if organization_id is not None:
        cohort_query = cohort_query.where(
            or_(
                Shipment.sender_organization_id == organization_id,
                Shipment.destination_organization_id == organization_id,
            )
        )
    if from_at is not None:
        cohort_query = cohort_query.where(Shipment.created_at >= from_at)
    if to_at is not None:
        cohort_query = cohort_query.where(Shipment.created_at < to_at)
    cohort = cohort_query.cte("analytics_cohort")

    pickup = (
        select(Handoff.shipment_id, func.min(Handoff.confirmed_at).label("picked_up_at"))
        .join(cohort, cohort.c.id == Handoff.shipment_id)
        .where(Handoff.kind == "PICKUP", Handoff.status == "CONFIRMED")
        .group_by(Handoff.shipment_id)
        .cte("confirmed_pickups")
    )
    ranked_issues = (
        select(
            ContainerIssue.container_id,
            ContainerIssue.kind,
            func.row_number()
            .over(
                partition_by=ContainerIssue.container_id,
                order_by=(ContainerIssue.reported_at.desc(), ContainerIssue.id.desc()),
            )
            .label("rank"),
        )
        .join(cohort, cohort.c.id == ContainerIssue.shipment_id)
        .cte("ranked_container_issues")
    )
    latest_issues = select(ranked_issues).where(ranked_issues.c.rank == 1).cte("latest_issues")
    received = TransportContainer.delivery_status == "RECEIVED"
    timed_receipt = and_(
        received,
        pickup.c.picked_up_at.is_not(None),
        TransportContainer.received_at >= pickup.c.picked_up_at,
    )
    container_metrics = (
        select(
            TransportContainer.shipment_id,
            func.count().label("container_count"),
            func.count().filter(received).label("received_count"),
            func.count().filter(timed_receipt).label("timed_received_count"),
            func.max(TransportContainer.received_at).filter(received).label("last_receipt_at"),
            func.sum(
                case(
                    (
                        timed_receipt,
                        func.extract(
                            "epoch", TransportContainer.received_at - pickup.c.picked_up_at
                        ),
                    ),
                    else_=0,
                )
            ).label("received_duration_sum"),
            func.count().filter(latest_issues.c.kind == "MISSING").label("container_missing"),
            func.count().filter(latest_issues.c.kind == "REJECTED").label("container_rejected"),
        )
        .join(cohort, cohort.c.id == TransportContainer.shipment_id)
        .outerjoin(pickup, pickup.c.shipment_id == TransportContainer.shipment_id)
        .outerjoin(latest_issues, latest_issues.c.container_id == TransportContainer.id)
        .group_by(TransportContainer.shipment_id)
        .cte("container_metrics")
    )
    sample_metrics = (
        select(
            TransportContainer.shipment_id,
            func.count().filter(ContainerItem.receipt_result == "MISSING").label("missing_samples"),
            func.count()
            .filter(ContainerItem.receipt_result == "REJECTED")
            .label("rejected_samples"),
        )
        .join(ContainerItem, ContainerItem.container_id == TransportContainer.id)
        .join(cohort, cohort.c.id == TransportContainer.shipment_id)
        .group_by(TransportContainer.shipment_id)
        .cte("sample_metrics")
    )
    partial = and_(
        container_metrics.c.received_count > 0,
        container_metrics.c.received_count < container_metrics.c.container_count,
    )
    full_timed = and_(
        container_metrics.c.container_count > 0,
        container_metrics.c.timed_received_count == container_metrics.c.container_count,
    )
    # Truncate a timestamp WITHOUT time zone in UTC, then restore UTC explicitly.
    # This is independent of the database connection's session TimeZone.
    period_start = func.timezone(
        "UTC",
        func.date_trunc(
            granularity,
            func.timezone("UTC", cohort.c.created_at, type_=DateTime(timezone=False)),
            type_=DateTime(timezone=False),
        ),
        type_=DateTime(timezone=True),
    )
    current = (
        select(
            cohort,
            period_start.label("period_start"),
            func.coalesce(partial, False).label("is_partial"),
            case(
                (
                    full_timed,
                    func.extract(
                        "epoch", container_metrics.c.last_receipt_at - pickup.c.picked_up_at
                    ),
                ),
                else_=None,
            ).label("full_delivery_seconds"),
            case((partial, container_metrics.c.received_duration_sum), else_=0).label(
                "partial_duration_sum"
            ),
            case((partial, container_metrics.c.timed_received_count), else_=0).label(
                "partial_duration_count"
            ),
            func.coalesce(sample_metrics.c.missing_samples, 0).label("missing_samples"),
            func.coalesce(sample_metrics.c.rejected_samples, 0).label("rejected_samples"),
            func.coalesce(container_metrics.c.container_missing, 0).label("container_missing"),
            func.coalesce(container_metrics.c.container_rejected, 0).label("container_rejected"),
        )
        .outerjoin(container_metrics, container_metrics.c.shipment_id == cohort.c.id)
        .outerjoin(sample_metrics, sample_metrics.c.shipment_id == cohort.c.id)
        .outerjoin(pickup, pickup.c.shipment_id == cohort.c.id)
        .cte("current_shipments")
    )
    complete = current.c.status.in_(COMPLETED_STATUSES)
    with_issues = current.c.status == "COMPLETED_WITH_ISSUES"
    completed_partial = and_(with_issues, current.c.is_partial)
    overdue = and_(
        current.c.status.not_in(TERMINAL_STATUSES),
        current.c.expected_delivery_at < func.now(),
    )
    # Ratios operate on explicit sums/counts; a zero denominator yields NULL.
    partial_average = func.sum(current.c.partial_duration_sum) / func.nullif(
        func.sum(current.c.partial_duration_count), 0
    )
    totals = (
        select(
            func.count().label("total"),
            func.count().filter(with_issues).label("completed_with_issues"),
            func.coalesce(func.sum(current.c.missing_samples), 0).label("missing_samples"),
            func.coalesce(func.sum(current.c.rejected_samples), 0).label("rejected_samples"),
            func.count().filter(overdue).label("overdue_active"),
            func.count()
            .filter(current.c.status == "CLOSED_UNDELIVERED")
            .label("closed_undelivered"),
            func.coalesce(func.sum(current.c.container_missing), 0).label("container_missing"),
            func.coalesce(func.sum(current.c.container_rejected), 0).label("container_rejected"),
            func.count().filter(current.c.is_partial).label("partially_delivered"),
            func.count().filter(completed_partial).label("partially_completed_with_issues"),
            func.avg(current.c.full_delivery_seconds).label(
                "average_full_shipment_delivery_seconds"
            ),
            partial_average.label("average_received_container_delivery_seconds"),
        )
        .select_from(current)
        .cte("analytics_totals")
    )
    statuses = (
        select(current.c.status, func.count().label("count"))
        .group_by(current.c.status)
        .cte("status_totals")
    )
    by_status = select(
        func.coalesce(
            func.jsonb_object_agg(statuses.c.status, statuses.c.count),
            literal_column("'{}'::jsonb", JSONB),
        )
    ).scalar_subquery()

    # Add calendar days to a UTC timestamp WITHOUT time zone, then restore UTC;
    # timestamptz + interval '1 day' could instead follow the session timezone.
    bucket_delta = timedelta(days=1 if granularity == "day" else 7)
    buckets = (
        select(
            current.c.period_start,
            func.timezone(
                "UTC",
                func.timezone("UTC", current.c.period_start, type_=DateTime(timezone=False))
                + bucket_delta,
                type_=DateTime(timezone=True),
            ).label("period_end"),
            func.count().label("total_shipments"),
            func.count().filter(complete).label("completed_shipments"),
            func.count().filter(current.c.is_partial).label("partially_delivered_shipments"),
            func.sum(current.c.missing_samples + current.c.rejected_samples).label("sample_issues"),
            func.sum(current.c.container_missing + current.c.container_rejected).label(
                "container_issues"
            ),
        )
        .group_by(current.c.period_start)
        .cte("period_totals")
    )
    bucket_values = func.jsonb_build_object(
        "period_start",
        buckets.c.period_start,
        "period_end",
        buckets.c.period_end,
        "total_shipments",
        buckets.c.total_shipments,
        "completed_shipments",
        buckets.c.completed_shipments,
        "partially_delivered_shipments",
        buckets.c.partially_delivered_shipments,
        "sample_issues",
        buckets.c.sample_issues,
        "container_issues",
        buckets.c.container_issues,
    )
    period_buckets = select(
        func.coalesce(
            func.jsonb_agg(aggregate_order_by(bucket_values, buckets.c.period_start)),
            literal_column("'[]'::jsonb", JSONB),
        )
    ).scalar_subquery()

    facility_rows = union_all(
        select(
            current.c.sender_organization_id.label("organization_id"),
            complete.label("is_complete"),
            with_issues.label("with_issues"),
        ),
        select(current.c.destination_organization_id, complete, with_issues).where(
            current.c.destination_organization_id != current.c.sender_organization_id
        ),
    ).cte("facility_shipments")
    facilities = (
        select(
            facility_rows.c.organization_id,
            func.count().label("shipment_count"),
            func.count().filter(facility_rows.c.is_complete).label("completed_count"),
            func.count().filter(facility_rows.c.with_issues).label("with_issues_count"),
        )
        .group_by(facility_rows.c.organization_id)
        .cte("facility_totals")
    )
    facility_values = func.jsonb_build_object(
        "organization_id",
        facilities.c.organization_id,
        "shipment_count",
        facilities.c.shipment_count,
        "completed_count",
        facilities.c.completed_count,
        "with_issues_count",
        facilities.c.with_issues_count,
    )
    by_facility = select(
        func.coalesce(
            func.jsonb_agg(aggregate_order_by(facility_values, facilities.c.organization_id)),
            literal_column("'[]'::jsonb", JSONB),
        )
    ).scalar_subquery()
    return select(
        totals,
        by_status.label("by_status"),
        period_buckets.label("period_buckets"),
        by_facility.label("by_facility"),
    )


async def shipment_summary(
    session: AsyncSession,
    principal: Principal,
    *,
    region_id: UUID | None,
    organization_id: UUID | None,
    from_at: datetime | None,
    to_at: datetime | None,
    granularity: Literal["day", "week"],
) -> ShipmentSummary:
    selected_region = analytics_region(principal, region_id)
    if from_at is not None and to_at is not None and from_at >= to_at:
        raise HTTPException(422, "from must precede to")
    if organization_id is not None:
        organization = await session.scalar(
            select(Organization.id).where(
                Organization.id == organization_id,
                Organization.region_id == selected_region,
            )
        )
        if organization is None:
            raise HTTPException(404, "Resource not found")
    result = await session.execute(
        summary_statement(
            region_id=selected_region,
            organization_id=organization_id,
            from_at=from_at,
            to_at=to_at,
            granularity=granularity,
        )
    )
    return ShipmentSummary.model_validate(
        {
            **result.mappings().one(),
            "region_id": selected_region,
            "from": from_at,
            "to": to_at,
        }
    )
