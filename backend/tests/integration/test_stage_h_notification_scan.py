"""PostgreSQL-backed checks for the registered Stage H notification scan task."""

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.core.logging import JsonFormatter
from app.modules.identity.models import Membership, Organization, Region, User
from app.modules.notifications.models import NotificationRecord
from app.modules.shipments.models import Handoff, Shipment, TransportContainer
from app.workers import notifications as notification_worker
from app.workers.notifications import scan_stalled_notifications


@pytest_asyncio.fixture
async def owner_sessions() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(get_settings().migration_database_url)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def _create_stalled_pickup(
    owner_sessions: async_sessionmaker[AsyncSession],
) -> tuple[UUID, UUID, UUID, UUID, UUID, datetime]:
    suffix = uuid4().hex[:12]
    now = datetime.now(UTC)
    async with owner_sessions() as owner, owner.begin():
        await owner.execute(text("SET LOCAL session_replication_role = 'replica'"))
        region = await owner.scalar(select(Region).where(Region.code == "PILOT"))
        destination = await owner.scalar(
            select(Organization).where(Organization.code == "CENTRAL_LAB")
        )
        assert region is not None and destination is not None

        sender_org = Organization(
            region_id=region.id,
            code=f"SCAN-{suffix}",
            name="Synthetic scan sender",
            organization_type="HEALTHCARE_FACILITY",
        )
        outsider_org = Organization(
            region_id=region.id,
            code=f"SCAN-X-{suffix}",
            name="Unrelated synthetic sender",
            organization_type="HEALTHCARE_FACILITY",
        )
        sender = User(
            email=f"scan-sender-{suffix}@example.test",
            display_name="Synthetic scan sender",
            password_hash="unused-stage-h-fixture-hash",
        )
        outsider = User(
            email=f"scan-outsider-{suffix}@example.test",
            display_name="Unrelated synthetic sender",
            password_hash="unused-stage-h-fixture-hash",
        )
        owner.add_all([sender_org, outsider_org, sender, outsider])
        await owner.flush()
        owner.add_all(
            [
                Membership(
                    user_id=sender.id,
                    organization_id=sender_org.id,
                    role="SENDER_OPERATOR",
                ),
                Membership(
                    user_id=outsider.id,
                    organization_id=outsider_org.id,
                    role="SENDER_OPERATOR",
                ),
            ]
        )

        shipment = Shipment(
            code=f"SCAN-S-{suffix}",
            region_id=region.id,
            sender_organization_id=sender_org.id,
            destination_organization_id=destination.id,
            status="PICKUP_PENDING",
            expected_delivery_at=now + timedelta(days=7),
            created_by_user_id=sender.id,
        )
        owner.add(shipment)
        await owner.flush()
        initiated_at = now - timedelta(days=1)
        handoff = Handoff(
            shipment_id=shipment.id,
            kind="PICKUP",
            attempt_no=1,
            status="PENDING",
            initiated_by=sender.id,
            initiated_at=initiated_at,
        )
        owner.add(handoff)
        await owner.flush()

        return shipment.id, handoff.id, sender.id, outsider.id, region.id, initiated_at


async def _custody_snapshot(
    owner_sessions: async_sessionmaker[AsyncSession], shipment_id: UUID, event_id: UUID
) -> tuple[tuple[object, ...], tuple[object, ...]]:
    async with owner_sessions() as owner:
        shipment = await owner.get(Shipment, shipment_id)
        handoff = await owner.get(Handoff, event_id)
        assert shipment is not None and handoff is not None
        return (
            (
                shipment.status,
                shipment.version,
                shipment.manifest_revision,
                shipment.pickup_state_version,
                shipment.courier_user_id,
                shipment.sealed_at,
                shipment.closed_at,
                shipment.closed_by,
                shipment.closure_reason,
                shipment.updated_at,
            ),
            (
                handoff.kind,
                handoff.container_id,
                handoff.attempt_no,
                handoff.status,
                handoff.initiated_by,
                handoff.initiated_at,
                handoff.confirmed_by,
                handoff.confirmed_at,
                handoff.withdrawn_by,
                handoff.withdrawn_at,
                handoff.withdrawal_reason,
                handoff.superseded_by_incident_id,
                handoff.superseded_at,
            ),
        )


@pytest.mark.asyncio
async def test_registered_scan_persists_safe_row_deduplicates_without_custody_changes(
    owner_sessions: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    (
        shipment_id,
        event_id,
        recipient_id,
        outsider_id,
        region_id,
        initiated_at,
    ) = await _create_stalled_pickup(owner_sessions)
    scan_settings = get_settings().model_copy(
        update={"stalled_pickup_seconds": 300, "stalled_delivery_seconds": 300}
    )
    monkeypatch.setattr(notification_worker, "get_settings", lambda: scan_settings)
    custody_before = await _custody_snapshot(owner_sessions, shipment_id, event_id)

    with caplog.at_level("INFO", logger="app.workers.notifications"):
        first_insert_count = await asyncio.to_thread(scan_stalled_notifications.run)
        second_insert_count = await asyncio.to_thread(scan_stalled_notifications.run)
        custody_after_initial_scans = await _custody_snapshot(owner_sessions, shipment_id, event_id)

        async with owner_sessions() as owner, owner.begin():
            await owner.execute(text("SET LOCAL session_replication_role = 'replica'"))
            shipment = await owner.get(Shipment, shipment_id)
            assert shipment is not None
            shipment.status = "DELIVERY_PENDING"
            container = TransportContainer(
                shipment_id=shipment_id,
                code=f"SCAN-DELIVERY-{uuid4().hex[:12]}",
                delivery_status="DELIVERY_PENDING",
            )
            owner.add(container)
            await owner.flush()
            owner.add(
                Handoff(
                    shipment_id=shipment_id,
                    kind="DELIVERY",
                    container_id=container.id,
                    attempt_no=1,
                    status="PENDING",
                    initiated_by=recipient_id,
                    initiated_at=initiated_at,
                )
            )

        custody_before_delivery_scan = await _custody_snapshot(
            owner_sessions, shipment_id, event_id
        )
        third_insert_count = await asyncio.to_thread(scan_stalled_notifications.run)

    async with owner_sessions() as owner:
        records = list(
            (
                await owner.scalars(
                    select(NotificationRecord).where(NotificationRecord.event_id == event_id)
                )
            ).all()
        )
    custody_after = await _custody_snapshot(owner_sessions, shipment_id, event_id)
    completed_events = [
        record for record in caplog.records if record.msg == "stalled_notification_scan_completed"
    ]
    completed_payloads = [json.loads(JsonFormatter().format(record)) for record in completed_events]

    assert first_insert_count == 1
    assert second_insert_count == 0
    assert third_insert_count == 0
    assert len(records) == 1
    record = records[0]
    assert record.recipient_user_id == recipient_id
    assert outsider_id not in {row.recipient_user_id for row in records}
    assert record.type == "PICKUP_PENDING"
    assert record.payload["case_type"] == "PICKUP_PENDING"
    assert record.payload["shipment_id"] == str(shipment_id)
    assert record.payload["region_id"] == str(region_id)
    assert record.payload["status"] == "PICKUP_PENDING"
    assert (
        datetime.fromisoformat(record.payload["last_event_at"].replace("Z", "+00:00"))
        == initiated_at
    )
    assert set(record.payload) == {
        "case_type",
        "shipment_id",
        "region_id",
        "status",
        "last_event_at",
    }
    assert custody_after_initial_scans == custody_before
    assert custody_after == custody_before_delivery_scan
    assert [
        (item["stalled_pickup_count"], item["stalled_delivery_count"])
        for item in completed_payloads
    ] == [(1, 0), (1, 0), (0, 1)]


@pytest.mark.asyncio
async def test_stalled_work_count_failure_does_not_block_notification_persistence(
    owner_sessions: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _, event_id, _, _, _, _ = await _create_stalled_pickup(owner_sessions)
    scan_settings = get_settings().model_copy(update={"stalled_pickup_seconds": 300})
    monkeypatch.setattr(notification_worker, "get_settings", lambda: scan_settings)

    async def fail_work_count(*_args: object, **_kwargs: object) -> tuple[int, int]:
        raise RuntimeError("synthetic aggregate query failure")

    monkeypatch.setattr(
        notification_worker, "count_stalled_pickup_delivery", fail_work_count, raising=False
    )
    with caplog.at_level("INFO", logger="app.workers.notifications"):
        inserted = await asyncio.to_thread(scan_stalled_notifications.run)

    async with owner_sessions() as owner:
        persisted_count = await owner.scalar(
            select(func.count())
            .select_from(NotificationRecord)
            .where(NotificationRecord.event_id == event_id)
        )
    assert inserted == 1
    assert persisted_count == 1
    failures = [record for record in caplog.records if record.msg == "stalled_work_count_failed"]
    assert len(failures) == 1
    payload = json.loads(JsonFormatter().format(failures[0]))
    assert payload["error_type"] == "RuntimeError"
    assert "synthetic aggregate query failure" not in json.dumps(payload)


@pytest.mark.asyncio
async def test_registered_scan_logs_and_reraises_a_persistence_failure(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    class SyntheticScanFailure(RuntimeError):
        pass

    async def fail_persistence(session: AsyncSession, candidates: list[object]) -> int:
        raise SyntheticScanFailure("synthetic notification persistence failure")

    monkeypatch.setattr(notification_worker, "persist_stalled_notifications", fail_persistence)
    with caplog.at_level("ERROR", logger="app.workers.notifications"):
        with pytest.raises(
            SyntheticScanFailure, match="synthetic notification persistence failure"
        ):
            await asyncio.to_thread(scan_stalled_notifications.run)

    failures = [
        record
        for record in caplog.records
        if record.name == "app.workers.notifications"
        and record.msg == "stalled_notification_scan_failed"
    ]
    assert len(failures) == 1
    assert failures[0].levelname == "ERROR"
    assert failures[0].outcome == "failure"
    assert failures[0].error_type == "SyntheticScanFailure"
