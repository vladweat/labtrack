"""PostgreSQL-backed acceptance checks for Stage H operations and analytics."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import null, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import Settings, get_settings
from app.db.session import get_session_factory
from app.main import app
from app.modules.analytics.service import shipment_summary
from app.modules.identity.access import MembershipGrant, Principal, get_current_principal
from app.modules.identity.models import Organization, Region, User
from app.modules.operations.service import list_stalled_cases
from app.modules.organizations.models import IssueReason
from app.modules.samples.models import Sample
from app.modules.shipments.models import (
    ContainerIssue,
    ContainerIssueResolutionProposal,
    ContainerItem,
    Handoff,
    ReceivingSession,
    Shipment,
    TransportContainer,
)


@pytest_asyncio.fixture
async def owner_sessions() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Use the disposable database owner only for synthetic fixture setup."""

    engine = create_async_engine(get_settings().migration_database_url)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


def _principal(user_id: UUID, *grants: MembershipGrant) -> Principal:
    return Principal(user_id, "stageh@example.test", "Stage H", tuple(grants))


async def _identity_rows(session: AsyncSession, suffix: str) -> dict[str, object]:
    pilot = await session.scalar(select(Region).where(Region.code == "PILOT"))
    central_lab = await session.scalar(
        select(Organization).where(Organization.code == "CENTRAL_LAB")
    )
    assert pilot is not None and central_lab is not None

    region_b = Region(code=f"SH-{suffix[:10]}", name="Stage H second region")
    org_a = Organization(
        region_id=pilot.id,
        code=f"SHA-{suffix[:10]}",
        name="Stage H sender A",
        organization_type="HEALTHCARE_FACILITY",
    )
    org_b = Organization(
        region=region_b,
        code=f"SHB-{suffix[:10]}",
        name="Stage H sender B",
        organization_type="HEALTHCARE_FACILITY",
    )
    destination_b = Organization(
        region=region_b,
        code=f"SHD-{suffix[:10]}",
        name="Stage H destination B",
        organization_type="LABORATORY",
    )
    actors = {
        name: User(
            email=f"{name}-{suffix}@example.test",
            display_name=f"Stage H {name}",
            password_hash="unused-stage-h-fixture-hash",
        )
        for name in ("sender_a", "sender_b", "courier_a", "courier_b", "worker")
    }
    session.add_all([region_b, org_a, org_b, destination_b, *actors.values()])
    await session.flush()
    return {
        "region_a": pilot.id,
        "region_b": region_b.id,
        "org_a": org_a.id,
        "org_b": org_b.id,
        "destination_a": central_lab.id,
        "destination_b": destination_b.id,
        **{name: actor.id for name, actor in actors.items()},
    }


async def _shipment(
    session: AsyncSession,
    *,
    identity: dict[str, object],
    code: str,
    region: str,
    sender: str,
    destination: str,
    actor: str,
    status: str,
    expected_delivery_at: datetime | None,
    created_at: datetime | None = None,
    cancelled: bool = False,
) -> Shipment:
    shipment = Shipment(
        code=f"SH-{uuid4().hex[:36]}",
        region_id=identity[region],
        sender_organization_id=identity[sender],
        destination_organization_id=identity[destination],
        courier_user_id=identity[actor] if actor.startswith("courier_") else None,
        status=status,
        expected_delivery_at=expected_delivery_at,
        created_by_user_id=identity[actor],
        created_at=created_at,
        closed_at=datetime.now(UTC) if cancelled else None,
        closed_by=identity[actor] if cancelled else None,
        closure_reason="Stage H fixture cancellation" if cancelled else None,
    )
    session.add(shipment)
    await session.flush()
    return shipment


async def _container(
    session: AsyncSession,
    shipment: Shipment,
    *,
    status: str = "PREPARED",
    received_at: datetime | None = None,
    received_by: UUID | None = None,
) -> TransportContainer:
    container = TransportContainer(
        shipment_id=shipment.id,
        code=f"SHC-{uuid4().hex[:18]}",
        delivery_status=status,
        received_at=received_at,
        received_by=received_by,
        receipt_evidence={"method": "NORMAL"} if received_at else null(),
    )
    session.add(container)
    await session.flush()
    return container


async def _handoff(
    session: AsyncSession,
    shipment: Shipment,
    *,
    actor_id: UUID,
    kind: str,
    initiated_at: datetime,
    container_id: UUID | None = None,
    status: str = "PENDING",
    attempt_no: int = 1,
    confirmed_at: datetime | None = None,
    withdrawn_at: datetime | None = None,
) -> Handoff:
    handoff = Handoff(
        shipment_id=shipment.id,
        kind=kind,
        container_id=container_id,
        attempt_no=attempt_no,
        status=status,
        initiated_by=actor_id,
        initiated_at=initiated_at,
        confirmed_by=actor_id if confirmed_at else None,
        confirmed_at=confirmed_at,
        withdrawn_by=actor_id if withdrawn_at else None,
        withdrawn_at=withdrawn_at,
        withdrawal_reason="Stage H retry fixture" if withdrawn_at else None,
    )
    session.add(handoff)
    return handoff


@pytest.mark.asyncio
async def test_stalled_operations_thresholds_scope_order_and_paging(
    owner_sessions: async_sessionmaker[AsyncSession],
) -> None:
    suffix = uuid4().hex[:12]
    now = datetime.now(UTC)
    thresholds = Settings(
        _env_file=None,
        stalled_pickup_seconds=600,
        stalled_delivery_seconds=500,
        stalled_proposal_seconds=900,
    )

    async with owner_sessions() as owner, owner.begin():
        await owner.execute(text("SET LOCAL session_replication_role = 'replica'"))
        identity = await _identity_rows(owner, suffix)

        pickup_a = await _shipment(
            owner,
            identity=identity,
            code="pickup-a",
            region="region_a",
            sender="org_a",
            destination="destination_a",
            actor="courier_a",
            status="PICKUP_PENDING",
            expected_delivery_at=now + timedelta(days=2),
        )
        await _handoff(
            owner,
            pickup_a,
            actor_id=identity["sender_a"],
            kind="PICKUP",
            initiated_at=now - timedelta(seconds=1200),
        )

        pickup_recent = await _shipment(
            owner,
            identity=identity,
            code="pickup-recent",
            region="region_a",
            sender="org_a",
            destination="destination_a",
            actor="courier_a",
            status="PICKUP_PENDING",
            expected_delivery_at=now + timedelta(days=2),
        )
        await _handoff(
            owner,
            pickup_recent,
            actor_id=identity["sender_a"],
            kind="PICKUP",
            initiated_at=now - timedelta(seconds=300),
        )

        delivery = await _shipment(
            owner,
            identity=identity,
            code="delivery-a",
            region="region_a",
            sender="org_a",
            destination="destination_a",
            actor="courier_b",
            status="DELIVERY_PENDING",
            expected_delivery_at=now + timedelta(days=2),
        )
        delivery_container = await _container(owner, delivery, status="DELIVERY_PENDING")
        await _handoff(
            owner,
            delivery,
            actor_id=identity["courier_b"],
            kind="DELIVERY",
            container_id=delivery_container.id,
            initiated_at=now - timedelta(seconds=800),
        )

        proposal_shipment = await _shipment(
            owner,
            identity=identity,
            code="proposal-a",
            region="region_a",
            sender="org_a",
            destination="destination_a",
            actor="courier_b",
            status="DELIVERY_BLOCKED",
            expected_delivery_at=now + timedelta(days=2),
        )
        proposal_container = await _container(owner, proposal_shipment, status="BLOCKED")
        container_reason = await owner.scalar(
            select(IssueReason.code).where(
                IssueReason.scope == "CONTAINER", IssueReason.kind == "MISSING"
            )
        )
        assert container_reason is not None
        issue = ContainerIssue(
            shipment_id=proposal_shipment.id,
            container_id=proposal_container.id,
            kind="MISSING",
            reason_code=container_reason,
            status="OPEN",
            reported_by=identity["worker"],
            reported_at=now - timedelta(hours=1),
        )
        owner.add(issue)
        await owner.flush()
        owner.add(
            ContainerIssueResolutionProposal(
                issue_id=issue.id,
                action="RETRY_DELIVERY",
                reason="Stage H proposal fixture",
                status="PENDING",
                proposed_by=identity["sender_a"],
                proposed_at=now - timedelta(seconds=1800),
            )
        )

        overdue = await _shipment(
            owner,
            identity=identity,
            code="overdue-a",
            region="region_a",
            sender="org_a",
            destination="destination_a",
            actor="courier_a",
            status="DRAFT",
            expected_delivery_at=now - timedelta(days=10),
        )

        region_b_pickup = await _shipment(
            owner,
            identity=identity,
            code="pickup-b",
            region="region_b",
            sender="org_b",
            destination="destination_b",
            actor="courier_b",
            status="PICKUP_PENDING",
            expected_delivery_at=now + timedelta(days=2),
        )
        await _handoff(
            owner,
            region_b_pickup,
            actor_id=identity["sender_b"],
            kind="PICKUP",
            initiated_at=now - timedelta(seconds=1200),
        )

    regional_ops = _principal(
        identity["worker"],
        MembershipGrant("REGIONAL_OPERATIONS", None, identity["region_a"]),
        MembershipGrant("REGIONAL_OPERATIONS", None, identity["region_b"]),
    )
    sender = _principal(
        identity["sender_a"],
        MembershipGrant("SENDER_OPERATOR", identity["org_a"], identity["region_a"]),
    )
    courier = _principal(
        identity["courier_a"],
        MembershipGrant("COURIER", None, identity["region_a"]),
    )
    lab_operator = _principal(
        identity["worker"],
        MembershipGrant("LAB_OPERATOR", identity["destination_a"], identity["region_a"]),
    )
    lab_manager = _principal(
        identity["worker"],
        MembershipGrant("LAB_MANAGER", identity["destination_a"], identity["region_a"]),
    )

    async with get_session_factory()() as session:
        first_page = await list_stalled_cases(
            session,
            regional_ops,
            thresholds,
            region_id=identity["region_a"],
            older_than_seconds=None,
            limit=2,
            offset=0,
        )
        second_page = await list_stalled_cases(
            session,
            regional_ops,
            thresholds,
            region_id=identity["region_a"],
            older_than_seconds=None,
            limit=2,
            offset=2,
        )
        old_cases = await list_stalled_cases(
            session,
            regional_ops,
            thresholds,
            region_id=identity["region_a"],
            older_than_seconds=1000,
            limit=20,
            offset=0,
        )
        region_b_cases = await list_stalled_cases(
            session,
            regional_ops,
            thresholds,
            region_id=identity["region_b"],
            older_than_seconds=None,
            limit=20,
            offset=0,
        )
        sender_cases = await list_stalled_cases(
            session,
            sender,
            thresholds,
            region_id=None,
            older_than_seconds=None,
            limit=20,
            offset=0,
        )
        courier_cases = await list_stalled_cases(
            session,
            courier,
            thresholds,
            region_id=None,
            older_than_seconds=None,
            limit=20,
            offset=0,
        )
        lab_operator_cases = await list_stalled_cases(
            session,
            lab_operator,
            thresholds,
            region_id=None,
            older_than_seconds=None,
            limit=20,
            offset=0,
        )
        lab_manager_cases = await list_stalled_cases(
            session,
            lab_manager,
            thresholds,
            region_id=None,
            older_than_seconds=None,
            limit=20,
            offset=0,
        )
        with pytest.raises(HTTPException) as outside_region:
            await list_stalled_cases(
                session,
                _principal(
                    identity["worker"],
                    MembershipGrant("REGIONAL_OPERATIONS", None, identity["region_a"]),
                ),
                thresholds,
                region_id=identity["region_b"],
                older_than_seconds=None,
                limit=20,
                offset=0,
            )

    assert first_page.total == 4
    assert [item.shipment_id for item in first_page.items] == [overdue.id, proposal_shipment.id]
    assert [item.shipment_id for item in second_page.items] == [pickup_a.id, delivery.id]
    assert [item.case_type for item in second_page.items] == ["PICKUP_PENDING", "DELIVERY_PENDING"]
    assert old_cases.total == 3
    assert {item.shipment_id for item in old_cases.items} == {
        overdue.id,
        proposal_shipment.id,
        pickup_a.id,
    }
    assert region_b_cases.total == 1
    assert region_b_cases.items[0].shipment_id == region_b_pickup.id
    assert {item.shipment_id for item in sender_cases.items} == {pickup_a.id, overdue.id}
    assert {item.shipment_id for item in courier_cases.items} == {pickup_a.id, overdue.id}
    assert {item.shipment_id for item in lab_operator_cases.items} == {delivery.id, overdue.id}
    assert {item.shipment_id for item in lab_manager_cases.items} == {
        delivery.id,
        proposal_shipment.id,
        overdue.id,
    }
    assert outside_region.value.status_code == 404
    overdue_case = next(item for item in first_page.items if item.shipment_id == overdue.id)
    assert overdue_case.case_type == "SHIPMENT_OVERDUE"
    assert overdue_case.age_seconds >= 10 * 24 * 60 * 60

    app.dependency_overrides[get_current_principal] = lambda: regional_ops
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://stageh-test"
        ) as client:
            dashboard = await client.get(
                "/api/v1/operations/stalled-shipments",
                params={"region_id": str(identity["region_a"]), "limit": 100},
            )
    finally:
        app.dependency_overrides.pop(get_current_principal, None)
    assert dashboard.status_code == 200
    assert any(
        row["shipment_id"] == str(overdue.id) and row["case_type"] == "SHIPMENT_OVERDUE"
        for row in dashboard.json()["items"]
    )


@pytest.mark.asyncio
async def test_analytics_utc_cohort_current_counts_reopen_means_and_breakdowns(
    owner_sessions: async_sessionmaker[AsyncSession],
) -> None:
    suffix = uuid4().hex[:12]
    from_at = datetime(2026, 7, 1, 23, 30, tzinfo=UTC)
    to_at = datetime(2026, 7, 3, 0, 0, tzinfo=UTC)
    session_now = datetime.now(UTC)

    async with owner_sessions() as owner, owner.begin():
        await owner.execute(text("SET LOCAL session_replication_role = 'replica'"))
        pilot = await owner.scalar(select(Region).where(Region.code == "PILOT"))
        assert pilot is not None
        org_a = Organization(
            region_id=pilot.id,
            code=f"SHA-{suffix}",
            name="Analytics sender A",
            organization_type="HEALTHCARE_FACILITY",
        )
        org_b = Organization(
            region_id=pilot.id,
            code=f"SHB-{suffix}",
            name="Analytics destination B",
            organization_type="LABORATORY",
        )
        org_c = Organization(
            region_id=pilot.id,
            code=f"SHC-{suffix}",
            name="Analytics self facility C",
            organization_type="HEALTHCARE_FACILITY",
        )
        worker = User(
            email=f"analytics-{suffix}@example.test",
            display_name="Analytics worker",
            password_hash="unused-stage-h-fixture-hash",
        )
        owner.add_all([org_a, org_b, org_c, worker])
        await owner.flush()

        a = await _shipment(
            owner,
            identity={
                "region": pilot.id,
                "sender": org_a.id,
                "destination": org_b.id,
                "worker": worker.id,
            },
            code="analytics-a",
            region="region",
            sender="sender",
            destination="destination",
            actor="worker",
            status="COMPLETED",
            expected_delivery_at=None,
            created_at=from_at,
        )
        b = await _shipment(
            owner,
            identity={
                "region": pilot.id,
                "sender": org_a.id,
                "destination": org_b.id,
                "worker": worker.id,
            },
            code="analytics-b",
            region="region",
            sender="sender",
            destination="destination",
            actor="worker",
            status="COMPLETED",
            expected_delivery_at=None,
            created_at=datetime(2026, 7, 2, 0, 0, tzinfo=UTC),
        )
        c = await _shipment(
            owner,
            identity={
                "region": pilot.id,
                "sender": org_a.id,
                "destination": org_b.id,
                "worker": worker.id,
            },
            code="analytics-partial-issues",
            region="region",
            sender="sender",
            destination="destination",
            actor="worker",
            status="COMPLETED_WITH_ISSUES",
            expected_delivery_at=None,
            created_at=datetime(2026, 7, 2, 12, 0, tzinfo=UTC),
        )
        d = await _shipment(
            owner,
            identity={
                "region": pilot.id,
                "sender": org_c.id,
                "destination": org_c.id,
                "worker": worker.id,
            },
            code="analytics-reopened",
            region="region",
            sender="sender",
            destination="destination",
            actor="worker",
            status="PARTIALLY_DELIVERED",
            expected_delivery_at=None,
            created_at=datetime(2026, 7, 2, 23, 0, tzinfo=UTC),
        )
        await _shipment(
            owner,
            identity={
                "region": pilot.id,
                "sender": org_a.id,
                "destination": org_b.id,
                "worker": worker.id,
            },
            code="analytics-overdue-active",
            region="region",
            sender="sender",
            destination="destination",
            actor="worker",
            status="IN_TRANSIT",
            expected_delivery_at=session_now - timedelta(days=1),
            created_at=datetime(2026, 7, 2, 18, 0, tzinfo=UTC),
        )
        await _shipment(
            owner,
            identity={
                "region": pilot.id,
                "sender": org_a.id,
                "destination": org_b.id,
                "worker": worker.id,
            },
            code="analytics-closed-undelivered",
            region="region",
            sender="sender",
            destination="destination",
            actor="worker",
            status="CLOSED_UNDELIVERED",
            expected_delivery_at=session_now - timedelta(days=2),
            created_at=datetime(2026, 7, 2, 19, 0, tzinfo=UTC),
        )
        outside = await _shipment(
            owner,
            identity={
                "region": pilot.id,
                "sender": org_a.id,
                "destination": org_b.id,
                "worker": worker.id,
            },
            code="analytics-upper-bound",
            region="region",
            sender="sender",
            destination="destination",
            actor="worker",
            status="DRAFT",
            expected_delivery_at=session_now - timedelta(days=3),
            created_at=to_at,
        )

        pickup_a = from_at + timedelta(seconds=100)
        await _handoff(
            owner,
            a,
            actor_id=worker.id,
            kind="PICKUP",
            initiated_at=pickup_a - timedelta(seconds=20),
            status="WITHDRAWN",
            attempt_no=1,
            withdrawn_at=pickup_a - timedelta(seconds=10),
        )
        await _handoff(
            owner,
            a,
            actor_id=worker.id,
            kind="PICKUP",
            initiated_at=pickup_a,
            status="CONFIRMED",
            attempt_no=2,
            confirmed_at=pickup_a,
        )
        for shipment, picked_up in (
            (b, b.created_at + timedelta(seconds=100)),
            (c, c.created_at + timedelta(seconds=100)),
            (d, d.created_at + timedelta(seconds=100)),
        ):
            await _handoff(
                owner,
                shipment,
                actor_id=worker.id,
                kind="PICKUP",
                initiated_at=picked_up,
                status="CONFIRMED",
                confirmed_at=picked_up,
            )

        a1 = await _container(
            owner,
            a,
            status="RECEIVED",
            received_at=pickup_a + timedelta(seconds=10),
            received_by=worker.id,
        )
        a2 = await _container(
            owner,
            a,
            status="RECEIVED",
            received_at=pickup_a + timedelta(seconds=30),
            received_by=worker.id,
        )
        await _container(
            owner,
            b,
            status="RECEIVED",
            received_at=b.created_at + timedelta(seconds=140),
            received_by=worker.id,
        )
        c1 = await _container(
            owner,
            c,
            status="RECEIVED",
            received_at=c.created_at + timedelta(seconds=120),
            received_by=worker.id,
        )
        await _container(owner, c)
        await _container(
            owner,
            d,
            status="RECEIVED",
            received_at=d.created_at + timedelta(seconds=130),
            received_by=worker.id,
        )
        await _container(owner, d)

        sample_missing = Sample(
            code=f"SHS-{uuid4().hex[:18]}",
            origin_organization_id=org_a.id,
            material_type="BLOOD",
            created_by_user_id=worker.id,
        )
        sample_rejected = Sample(
            code=f"SHS-{uuid4().hex[:18]}",
            origin_organization_id=org_a.id,
            material_type="BLOOD",
            created_by_user_id=worker.id,
        )
        sample_c_missing = Sample(
            code=f"SHS-{uuid4().hex[:18]}",
            origin_organization_id=org_a.id,
            material_type="BLOOD",
            created_by_user_id=worker.id,
        )
        owner.add_all([sample_missing, sample_rejected, sample_c_missing])
        await owner.flush()
        owner.add_all(
            [
                ContainerItem(
                    container_id=a1.id,
                    sample_id=sample_missing.id,
                    receipt_result="MISSING",
                    receipt_recorded_at=pickup_a + timedelta(seconds=11),
                ),
                ContainerItem(
                    container_id=a2.id,
                    sample_id=sample_rejected.id,
                    receipt_result="REJECTED",
                    receipt_recorded_at=pickup_a + timedelta(seconds=31),
                ),
                ContainerItem(
                    container_id=c1.id,
                    sample_id=sample_c_missing.id,
                    receipt_result="MISSING",
                    receipt_recorded_at=c.created_at + timedelta(seconds=121),
                ),
            ]
        )

        missing_code = await owner.scalar(
            select(IssueReason.code).where(
                IssueReason.scope == "CONTAINER", IssueReason.kind == "MISSING"
            )
        )
        rejected_code = await owner.scalar(
            select(IssueReason.code).where(
                IssueReason.scope == "CONTAINER", IssueReason.kind == "REJECTED"
            )
        )
        assert missing_code is not None and rejected_code is not None
        owner.add_all(
            [
                ContainerIssue(
                    shipment_id=a.id,
                    container_id=a1.id,
                    kind="MISSING",
                    reason_code=missing_code,
                    status="RESOLVED",
                    reported_by=worker.id,
                    reported_at=pickup_a - timedelta(seconds=50),
                    resolved_by=worker.id,
                    resolved_at=pickup_a - timedelta(seconds=40),
                    resolution="RETRY_DELIVERY",
                ),
                ContainerIssue(
                    shipment_id=a.id,
                    container_id=a1.id,
                    kind="REJECTED",
                    reason_code=rejected_code,
                    status="OPEN",
                    reported_by=worker.id,
                    reported_at=pickup_a - timedelta(seconds=30),
                ),
                ContainerIssue(
                    shipment_id=a.id,
                    container_id=a2.id,
                    kind="MISSING",
                    reason_code=missing_code,
                    status="OPEN",
                    reported_by=worker.id,
                    reported_at=pickup_a - timedelta(seconds=20),
                ),
                ContainerIssue(
                    shipment_id=c.id,
                    container_id=c1.id,
                    kind="REJECTED",
                    reason_code=rejected_code,
                    status="RESOLVED",
                    reported_by=worker.id,
                    reported_at=c.created_at + timedelta(seconds=30),
                    resolved_by=worker.id,
                    resolved_at=c.created_at + timedelta(seconds=40),
                    resolution="RETRY_DELIVERY",
                ),
                ContainerIssue(
                    shipment_id=c.id,
                    container_id=c1.id,
                    kind="MISSING",
                    reason_code=missing_code,
                    status="OPEN",
                    reported_by=worker.id,
                    reported_at=c.created_at + timedelta(seconds=50),
                ),
            ]
        )
        owner.add_all(
            [
                ReceivingSession(
                    shipment_id=d.id,
                    sequence=1,
                    status="CLOSED",
                    opened_by=worker.id,
                    opened_at=d.created_at + timedelta(seconds=10),
                    closed_by=worker.id,
                    closed_at=d.created_at + timedelta(seconds=20),
                ),
                ReceivingSession(
                    shipment_id=d.id,
                    sequence=2,
                    status="OPEN",
                    opened_by=worker.id,
                    opened_at=d.created_at + timedelta(seconds=30),
                    reopen_reason="Stage H reopened fixture",
                ),
            ]
        )
        await owner.flush()

    assert outside.id
    analytics_principal = _principal(
        worker.id,
        MembershipGrant("REGIONAL_ANALYST", None, pilot.id),
    )
    async with get_session_factory()() as session:
        await session.execute(text("SET LOCAL TIME ZONE 'Pacific/Auckland'"))
        summary = await shipment_summary(
            session,
            analytics_principal,
            region_id=pilot.id,
            organization_id=None,
            from_at=from_at,
            to_at=to_at,
            granularity="day",
        )
        weekly = await shipment_summary(
            session,
            analytics_principal,
            region_id=pilot.id,
            organization_id=None,
            from_at=from_at,
            to_at=to_at,
            granularity="week",
        )
        sender_slice = await shipment_summary(
            session,
            analytics_principal,
            region_id=pilot.id,
            organization_id=org_c.id,
            from_at=from_at,
            to_at=to_at,
            granularity="day",
        )

    assert summary.total == 6
    assert summary.by_status == {
        "COMPLETED": 2,
        "COMPLETED_WITH_ISSUES": 1,
        "PARTIALLY_DELIVERED": 1,
        "IN_TRANSIT": 1,
        "CLOSED_UNDELIVERED": 1,
    }
    assert summary.completed_with_issues == 1
    assert summary.overdue_active == 1
    assert summary.closed_undelivered == 1
    assert summary.partially_delivered == 2
    assert summary.partially_completed_with_issues == 1
    assert summary.average_full_shipment_delivery_seconds == pytest.approx(35.0)
    assert summary.average_received_container_delivery_seconds == pytest.approx(25.0)
    assert summary.missing_samples == 2
    assert summary.rejected_samples == 1
    assert summary.container_missing == 2
    assert summary.container_rejected == 1
    assert len(summary.period_buckets) == 2
    assert summary.period_buckets[0].period_start == datetime(2026, 7, 1, tzinfo=UTC)
    assert summary.period_buckets[0].total_shipments == 1
    assert summary.period_buckets[0].completed_shipments == 1
    assert summary.period_buckets[0].sample_issues == 2
    assert summary.period_buckets[0].container_issues == 2
    assert summary.period_buckets[1].period_start == datetime(2026, 7, 2, tzinfo=UTC)
    assert summary.period_buckets[1].total_shipments == 5
    assert summary.period_buckets[1].completed_shipments == 2
    assert summary.period_buckets[1].partially_delivered_shipments == 2
    assert summary.period_buckets[1].sample_issues == 1
    assert summary.period_buckets[1].container_issues == 1

    facilities = {row.organization_id: row for row in summary.by_facility}
    assert facilities[org_a.id].shipment_count == 5
    assert facilities[org_a.id].completed_count == 3
    assert facilities[org_a.id].with_issues_count == 1
    assert facilities[org_b.id].shipment_count == 5
    assert facilities[org_b.id].completed_count == 3
    assert facilities[org_c.id].shipment_count == 1
    assert len(weekly.period_buckets) == 1
    assert weekly.period_buckets[0].period_start == datetime(2026, 6, 29, tzinfo=UTC)
    assert weekly.period_buckets[0].period_end == datetime(2026, 7, 6, tzinfo=UTC)
    assert weekly.period_buckets[0].total_shipments == 6
    assert sender_slice.total == 1
    assert sender_slice.period_buckets[0].total_shipments == 1
    assert len(sender_slice.by_facility) == 1
    assert sender_slice.by_facility[0].organization_id == org_c.id
