"""PostgreSQL-backed acceptance checks for Stage H Inbox and migration behavior."""

import asyncio
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import Settings, get_settings
from app.db.session import get_session_factory
from app.main import app
from app.modules.identity.access import Principal, get_current_principal
from app.modules.identity.models import Membership, Organization, Region, User
from app.modules.notifications.models import NotificationRecord
from app.modules.notifications.service import persist_stalled_notifications
from app.modules.operations.service import list_notification_candidates
from app.modules.shipments.models import Handoff, Shipment


@pytest_asyncio.fixture
async def owner_sessions() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(get_settings().migration_database_url)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_notification_scan_is_recipient_scoped_deduped_and_phi_free(
    owner_sessions: async_sessionmaker[AsyncSession],
) -> None:
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
            code=f"SHN-{suffix}",
            name="Notification test sender",
            organization_type="HEALTHCARE_FACILITY",
        )
        sender = User(
            email=f"notify-sender-{suffix}@example.test",
            display_name="Notification sender",
            password_hash="unused-stage-h-fixture-hash",
        )
        courier = User(
            email=f"notify-courier-{suffix}@example.test",
            display_name="Notification courier",
            password_hash="unused-stage-h-fixture-hash",
        )
        operations = User(
            email=f"notify-ops-{suffix}@example.test",
            display_name="Notification operations",
            password_hash="unused-stage-h-fixture-hash",
        )
        outsider = User(
            email=f"notify-outsider-{suffix}@example.test",
            display_name="Notification outsider",
            password_hash="unused-stage-h-fixture-hash",
        )
        owner.add_all([sender_org, sender, courier, operations, outsider])
        await owner.flush()
        owner.add_all(
            [
                Membership(
                    user_id=sender.id,
                    organization_id=sender_org.id,
                    role="SENDER_OPERATOR",
                ),
                Membership(user_id=courier.id, region_id=region.id, role="COURIER"),
                Membership(
                    user_id=operations.id,
                    region_id=region.id,
                    role="REGIONAL_OPERATIONS",
                ),
            ]
        )
        shipment = Shipment(
            code=f"SHN-S-{suffix}",
            region_id=region.id,
            sender_organization_id=sender_org.id,
            destination_organization_id=destination.id,
            courier_user_id=courier.id,
            status="PICKUP_PENDING",
            expected_delivery_at=now + timedelta(days=2),
            created_by_user_id=sender.id,
        )
        owner.add(shipment)
        await owner.flush()
        event = Handoff(
            shipment_id=shipment.id,
            kind="PICKUP",
            attempt_no=1,
            status="PENDING",
            initiated_by=sender.id,
            initiated_at=now - timedelta(hours=2),
        )
        owner.add(event)

    async with get_session_factory()() as session:
        candidates = await list_notification_candidates(
            session,
            Settings(_env_file=None, stalled_pickup_seconds=300),
        )
        assert len(candidates) == 1
        candidate = candidates[0]
        assert candidate.event_id == event.id
        assert set(candidate.recipient_user_ids) == {sender.id, courier.id, operations.id}
        assert outsider.id not in candidate.recipient_user_ids
        assert set(candidate.payload) == {
            "case_type",
            "shipment_id",
            "region_id",
            "status",
            "last_event_at",
        }
        first_inserted = await persist_stalled_notifications(session, candidates)
        duplicate_inserted = await persist_stalled_notifications(session, candidates)
        await session.commit()

        records = list(
            (
                await session.scalars(
                    select(NotificationRecord).where(NotificationRecord.event_id == event.id)
                )
            ).all()
        )

    assert first_inserted == 3
    assert duplicate_inserted == 0
    assert {record.recipient_user_id for record in records} == {
        sender.id,
        courier.id,
        operations.id,
    }
    assert all(
        set(record.payload)
        <= {
            "case_type",
            "shipment_id",
            "region_id",
            "status",
            "last_event_at",
            "container_id",
            "proposal_id",
        }
        for record in records
    )
    assert all("sample" not in str(record.payload).casefold() for record in records)


@pytest.mark.asyncio
async def test_inbox_masks_foreign_ids_and_read_is_idempotent(
    owner_sessions: async_sessionmaker[AsyncSession],
) -> None:
    suffix = uuid4().hex[:12]
    now = datetime.now(UTC)
    async with owner_sessions() as owner, owner.begin():
        first = User(
            email=f"inbox-first-{suffix}@example.test",
            display_name="Inbox first",
            password_hash="unused-stage-h-fixture-hash",
        )
        second = User(
            email=f"inbox-second-{suffix}@example.test",
            display_name="Inbox second",
            password_hash="unused-stage-h-fixture-hash",
        )
        owner.add_all([first, second])
        await owner.flush()
        region = await owner.scalar(select(Region).where(Region.code == "PILOT"))
        assert region is not None
        first_record = NotificationRecord(
            event_id=uuid4(),
            type="SHIPMENT_OVERDUE",
            payload={
                "case_type": "SHIPMENT_OVERDUE",
                "shipment_id": str(uuid4()),
                "region_id": str(region.id),
                "status": "DRAFT",
                "last_event_at": now.isoformat(),
            },
            recipient_user_id=first.id,
        )
        second_record = NotificationRecord(
            event_id=uuid4(),
            type="SHIPMENT_OVERDUE",
            payload={
                "case_type": "SHIPMENT_OVERDUE",
                "shipment_id": str(uuid4()),
                "region_id": str(region.id),
                "status": "DRAFT",
                "last_event_at": now.isoformat(),
            },
            recipient_user_id=second.id,
        )
        owner.add_all([first_record, second_record])

    first_principal = Principal(first.id, first.email, first.display_name, ())
    app.dependency_overrides[get_current_principal] = lambda: first_principal
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://stageh-test"
        ) as client:
            listed = await client.get("/api/v1/notifications")
            unread_list = await client.get("/api/v1/notifications", params={"unread_only": "true"})
            unread = await client.get("/api/v1/notifications/unread-count")
            own_first_read = await client.post(
                f"/api/v1/notifications/{first_record.id}/read",
                headers={"Idempotency-Key": f"read-own-{suffix}-one"},
            )
            own_replay = await client.post(
                f"/api/v1/notifications/{first_record.id}/read",
                headers={"Idempotency-Key": f"read-own-{suffix}-one"},
            )
            own_new_key = await client.post(
                f"/api/v1/notifications/{first_record.id}/read",
                headers={"Idempotency-Key": f"read-own-{suffix}-two"},
            )
            foreign_read = await client.post(
                f"/api/v1/notifications/{second_record.id}/read",
                headers={"Idempotency-Key": f"read-foreign-{suffix}"},
            )
            unknown_read = await client.post(
                f"/api/v1/notifications/{uuid4()}/read",
                headers={"Idempotency-Key": f"read-unknown-{suffix}"},
            )
            unread_list_after = await client.get(
                "/api/v1/notifications", params={"unread_only": "true"}
            )
            unread_after = await client.get("/api/v1/notifications/unread-count")
    finally:
        app.dependency_overrides.pop(get_current_principal, None)

    assert listed.status_code == 200
    assert listed.json()["total"] == 1
    assert [item["id"] for item in listed.json()["items"]] == [str(first_record.id)]
    assert unread_list.status_code == 200 and unread_list.json()["total"] == 1
    assert unread.status_code == 200 and unread.json() == {"count": 1}
    assert own_first_read.status_code == own_replay.status_code == own_new_key.status_code == 200
    assert own_first_read.json()["read_at"] is not None
    assert own_replay.json()["read_at"] == own_first_read.json()["read_at"]
    assert own_new_key.json()["read_at"] == own_first_read.json()["read_at"]
    assert foreign_read.status_code == unknown_read.status_code == 404
    assert foreign_read.json()["error"] | {"request_id": None} == unknown_read.json()["error"] | {
        "request_id": None
    }
    assert foreign_read.json()["error"]["request_id"] != unknown_read.json()["error"]["request_id"]
    assert unread_list_after.status_code == 200 and unread_list_after.json()["total"] == 0
    assert unread_after.status_code == 200 and unread_after.json() == {"count": 0}

    async with owner_sessions() as owner:
        foreign_read_at = await owner.scalar(
            select(NotificationRecord.read_at).where(NotificationRecord.id == second_record.id)
        )
        own_read_at = await owner.scalar(
            select(NotificationRecord.read_at).where(NotificationRecord.id == first_record.id)
        )
    assert foreign_read_at is None
    assert own_read_at == datetime.fromisoformat(
        own_first_read.json()["read_at"].replace("Z", "+00:00")
    )


def _run_alembic(
    arguments: list[str], cwd: Path, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "alembic", *arguments],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.asyncio
async def test_notifications_migration_downgrade_upgrade_and_app_role_grants(
    owner_sessions: async_sessionmaker[AsyncSession],
) -> None:
    backend = Path(__file__).resolve().parents[2]
    env = os.environ.copy()

    downgraded = await asyncio.to_thread(
        _run_alembic,
        ["downgrade", "0009_stage_g_offline"],
        backend,
        env,
    )
    assert downgraded.returncode == 0, downgraded.stderr
    async with owner_sessions() as owner:
        assert await owner.scalar(text("SELECT to_regclass('public.notifications')")) is None

    upgraded = await asyncio.to_thread(_run_alembic, ["upgrade", "head"], backend, env)
    assert upgraded.returncode == 0, upgraded.stderr
    async with owner_sessions() as owner:
        assert await owner.scalar(text("SELECT to_regclass('public.notifications')")) == (
            "notifications"
        )
        privileges = await owner.scalar(
            text(
                "SELECT has_table_privilege(:role_name, 'public.notifications', "
                "'SELECT,INSERT,UPDATE,DELETE')"
            ),
            {"role_name": get_settings().app_postgres_user},
        )
        assert privileges is True

    async with get_session_factory()() as app_session:
        assert await app_session.scalar(select(func.count()).select_from(NotificationRecord)) == 0
