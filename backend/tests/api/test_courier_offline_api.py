"""PostgreSQL persistence and guard regressions for courier offline work."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine
from test_pickup_api import create_pickup_fixture

from app.core.config import get_settings
from app.core.security import hash_password
from app.db.session import get_session_factory
from app.main import app
from app.modules.identity.models import Membership, User

PASSWORD = "Courier Offline Test Password 2026!"


async def authorize_unassigned_courier(
    shipment_id: str, client: AsyncClient
) -> tuple[str, dict[str, str]]:
    suffix = uuid4().hex[:10]
    async with get_session_factory()() as session:
        region_id = await session.scalar(
            text("SELECT region_id FROM shipments WHERE id=:shipment_id"),
            {"shipment_id": shipment_id},
        )
        assert region_id is not None
        courier = User(
            email=f"unassigned-courier-{suffix}@example.test",
            display_name="Unassigned Offline Courier",
            password_hash=hash_password(PASSWORD),
        )
        session.add(courier)
        await session.flush()
        session.add(Membership(user_id=courier.id, region_id=region_id, role="COURIER"))
        await session.commit()
        courier_id = str(courier.id)
        email = courier.email

    login = await client.post("/api/v1/auth/token", json={"email": email, "password": PASSWORD})
    assert login.status_code == 200, login.text
    return courier_id, {"Authorization": f"Bearer {login.json()['access_token']}"}


def test_offline_manifest_runtime_openapi_conforms_to_historical_design_excerpt() -> None:
    contract_path = Path(__file__).resolve().parents[3] / "openapi" / "labtrack-v1.json"
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    generated = app.openapi()
    path = "/courier/shipments/{shipment_id}/offline-manifest"
    expected = contract["paths"][path]["get"]
    actual = generated["paths"].get(f"/api/v1{path}", {}).get("get")

    assert actual is not None
    assert actual["operationId"] == "getCourierOfflineManifest"
    assert actual["operationId"] == expected["operationId"]
    assert actual["summary"] == expected["summary"]
    assert actual["tags"] == expected["tags"]
    assert set(actual["responses"]) == set(expected["responses"])
    assert actual["responses"]["200"]["description"] == expected["responses"]["200"]["description"]
    assert actual["responses"]["200"]["headers"] == expected["responses"]["200"]["headers"]
    expected_schema_name = expected["responses"]["200"]["content"]["application/json"]["schema"][
        "$ref"
    ].rsplit("/", 1)[1]
    actual_schema_name = actual["responses"]["200"]["content"]["application/json"]["schema"][
        "$ref"
    ].rsplit("/", 1)[1]
    assert actual_schema_name == expected_schema_name == "CourierOfflineManifest"

    expected_schema = contract["components"]["schemas"][expected_schema_name]
    actual_schema = generated["components"]["schemas"][actual_schema_name]
    assert set(actual_schema["properties"]) == set(expected_schema["properties"])
    assert set(actual_schema["required"]) == set(expected_schema["required"])
    assert actual_schema["additionalProperties"] is False

    expected_container = expected_schema["properties"]["containers"]["items"]
    actual_container = actual_schema["properties"]["containers"]["items"]
    if "$ref" in actual_container:
        name = actual_container["$ref"].rsplit("/", 1)[1]
        actual_container = generated["components"]["schemas"][name]
    assert set(actual_container["properties"]) == set(expected_container["properties"])
    assert set(actual_container["required"]) == set(expected_container["required"])
    assert actual_container["additionalProperties"] is False


@pytest.mark.asyncio
async def test_offline_manifest_issues_persisted_phi_free_server_snapshot() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context = await create_pickup_fixture(client, container_count=2)
        request_started = datetime.now(UTC)
        response = await client.get(
            f"/api/v1/courier/shipments/{context['shipment_id']}/offline-manifest",
            headers=context["courier_auth"],
        )
        request_finished = datetime.now(UTC)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["shipment_id"] == context["shipment_id"]
    assert body["status"] == "READY_FOR_PICKUP"
    assert body["manifest_revision"] >= 1
    assert body["container_codes"] == [item["code"] for item in body["containers"]]
    assert set(body["container_codes"]) == {item["code"] for item in context["containers"]}
    assert {item["id"] for item in body["containers"]} == {
        item["id"] for item in context["containers"]
    }
    assert len(body["containers"]) == len(context["containers"])
    assert UUID(body["snapshot_id"])
    assert body["cached_at"] == body["issued_at"]

    issued_at = datetime.fromisoformat(body["issued_at"].replace("Z", "+00:00"))
    assert (
        request_started - timedelta(seconds=5)
        <= issued_at
        <= request_finished + timedelta(seconds=5)
    )
    assert set(body) == {
        "shipment_id",
        "manifest_revision",
        "status",
        "container_codes",
        "cached_at",
        "containers",
        "snapshot_id",
        "issued_at",
        "pickup_state_version",
    }
    assert body["pickup_state_version"] >= 1
    for container in body["containers"]:
        assert set(container) == {"id", "code", "delivery_status", "state_version"}
        assert container["state_version"] >= 1
        assert "sample" not in json.dumps(container).casefold()
        assert "patient" not in json.dumps(container).casefold()

    async with get_session_factory()() as session:
        row = (
            (
                await session.execute(
                    text(
                        "SELECT actor_user_id, assigned_courier_user_id, shipment_id, region_id, "
                        "manifest_revision, pickup_state_version, issued_at, policy_version, "
                        "container_payload FROM courier_manifest_snapshots WHERE id=:id"
                    ),
                    {"id": body["snapshot_id"]},
                )
            )
            .mappings()
            .one()
        )
        shipment = (
            await session.execute(
                text(
                    "SELECT region_id, manifest_revision, pickup_state_version, status "
                    "FROM shipments WHERE id=:id"
                ),
                {"id": context["shipment_id"]},
            )
        ).one()
        container_rows = (
            (
                await session.execute(
                    text(
                        "SELECT id, code, delivery_status, state_version FROM transport_containers "
                        "WHERE shipment_id=:shipment_id ORDER BY id"
                    ),
                    {"shipment_id": context["shipment_id"]},
                )
            )
            .mappings()
            .all()
        )

        assert str(row["actor_user_id"]) == str(context["courier_id"])
        assert str(row["assigned_courier_user_id"]) == str(context["courier_id"])
        assert str(row["shipment_id"]) == context["shipment_id"]
        assert row["region_id"] == shipment.region_id
        assert row["manifest_revision"] == body["manifest_revision"] == shipment.manifest_revision
        assert (
            row["pickup_state_version"]
            == body["pickup_state_version"]
            == (shipment.pickup_state_version)
        )
        assert row["issued_at"] == issued_at
        assert row["policy_version"] == "courier-offline-v1"
        assert row["container_payload"] == body["containers"]
        assert shipment.status == body["status"]
        assert row["container_payload"] == [
            {
                "id": str(item["id"]),
                "code": item["code"],
                "delivery_status": item["delivery_status"],
                "state_version": item["state_version"],
            }
            for item in container_rows
        ]

        with pytest.raises(DBAPIError, match="immutable"):
            async with session.begin_nested():
                await session.execute(
                    text(
                        "UPDATE courier_manifest_snapshots SET policy_version='changed' "
                        "WHERE id=:id"
                    ),
                    {"id": body["snapshot_id"]},
                )
        with pytest.raises(DBAPIError, match="immutable"):
            async with session.begin_nested():
                await session.execute(
                    text("DELETE FROM courier_manifest_snapshots WHERE id=:id"),
                    {"id": body["snapshot_id"]},
                )
        await session.rollback()


@pytest.mark.asyncio
async def test_offline_manifest_denies_unassigned_and_revoked_couriers() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context = await create_pickup_fixture(client)
        unassigned_id, unassigned_auth = await authorize_unassigned_courier(
            context["shipment_id"], client
        )
        unassigned = await client.get(
            f"/api/v1/courier/shipments/{context['shipment_id']}/offline-manifest",
            headers=unassigned_auth,
        )
        assert unassigned.status_code == 404

        async with get_session_factory()() as session:
            await session.execute(
                text(
                    "UPDATE memberships SET is_active=FALSE WHERE user_id=:user_id "
                    "AND role='COURIER'"
                ),
                {"user_id": context["courier_id"]},
            )
            await session.commit()

        revoked = await client.get(
            f"/api/v1/courier/shipments/{context['shipment_id']}/offline-manifest",
            headers=context["courier_auth"],
        )
        assert revoked.status_code == 404

    async with get_session_factory()() as session:
        snapshot_count = await session.scalar(
            text("SELECT count(*) FROM courier_manifest_snapshots")
        )
        assert snapshot_count == 0
        active_unassigned_membership = await session.scalar(
            text(
                "SELECT count(*) FROM memberships WHERE user_id=:user_id AND role='COURIER' "
                "AND is_active=TRUE"
            ),
            {"user_id": unassigned_id},
        )
        assert active_unassigned_membership == 1


def test_offline_migration_is_additive_after_stage_f_integrity() -> None:
    backend = Path(__file__).resolve().parents[2]
    config = Config(str(backend / "alembic.ini"))
    config.set_main_option("script_location", str(backend / "alembic"))
    config.set_main_option("path_separator", "os")
    script = ScriptDirectory.from_config(config)
    head_revision = script.get_revision(script.get_current_head())
    offline_revision = script.get_revision("0009_stage_g_offline")

    assert head_revision is not None
    assert offline_revision is not None
    assert head_revision.revision == "0011_http_request_scope"
    assert head_revision.down_revision == "0010_notifications"
    assert offline_revision.down_revision == "0008_stage_f_integrity"


@pytest.mark.asyncio
async def test_manifest_snapshot_is_server_issued_and_immutable() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context = await create_pickup_fixture(client)

    snapshot_id = uuid4()
    async with get_session_factory()() as session:
        region_id = await session.scalar(
            text("SELECT region_id FROM shipments WHERE id=:shipment_id"),
            {"shipment_id": context["shipment_id"]},
        )
        payload = [
            {
                "id": context["container_id"],
                "code": context["container_code"],
                "delivery_status": "PREPARED",
                "state_version": 1,
            }
        ]
        await session.execute(
            text(
                "INSERT INTO courier_manifest_snapshots "
                "(id, actor_user_id, assigned_courier_user_id, shipment_id, region_id, "
                "manifest_revision, pickup_state_version, issued_at, policy_version, "
                "container_payload) VALUES "
                "(:id, :actor_id, :courier_id, :shipment_id, :region_id, 1, 1, :issued_at, "
                ":policy_version, CAST(:container_payload AS jsonb))"
            ),
            {
                "id": snapshot_id,
                "actor_id": context["courier_id"],
                "courier_id": context["courier_id"],
                "shipment_id": context["shipment_id"],
                "region_id": region_id,
                "issued_at": datetime(2000, 1, 1, tzinfo=UTC),
                "policy_version": "courier-offline-v1",
                "container_payload": json.dumps(payload),
            },
        )
        snapshot = (
            (
                await session.execute(
                    text(
                        "SELECT actor_user_id, assigned_courier_user_id, shipment_id, region_id, "
                        "manifest_revision, pickup_state_version, issued_at, policy_version, "
                        "container_payload FROM courier_manifest_snapshots WHERE id=:id"
                    ),
                    {"id": snapshot_id},
                )
            )
            .mappings()
            .one()
        )

        assert snapshot["actor_user_id"] == context["courier_id"]
        assert snapshot["assigned_courier_user_id"] == context["courier_id"]
        assert snapshot["shipment_id"] == UUID(context["shipment_id"])
        assert snapshot["region_id"] == region_id
        assert snapshot["manifest_revision"] == 1
        assert snapshot["pickup_state_version"] == 1
        assert snapshot["issued_at"] > datetime(2026, 1, 1, tzinfo=UTC)
        assert snapshot["policy_version"] == "courier-offline-v1"
        assert snapshot["container_payload"] == [
            {
                "id": context["container_id"],
                "code": context["container_code"],
                "delivery_status": "PREPARED",
                "state_version": 1,
            }
        ]

        with pytest.raises(DBAPIError, match="phi_free_container_payload"):
            async with session.begin_nested():
                await session.execute(
                    text(
                        "INSERT INTO courier_manifest_snapshots "
                        "(id, actor_user_id, assigned_courier_user_id, shipment_id, region_id, "
                        "manifest_revision, pickup_state_version, policy_version, "
                        "container_payload) VALUES "
                        "(:id, :actor_id, :courier_id, :shipment_id, :region_id, 1, 1, "
                        ":policy_version, CAST(:container_payload AS jsonb))"
                    ),
                    {
                        "id": uuid4(),
                        "actor_id": context["courier_id"],
                        "courier_id": context["courier_id"],
                        "shipment_id": context["shipment_id"],
                        "region_id": region_id,
                        "policy_version": "courier-offline-v1",
                        "container_payload": json.dumps(
                            [{**payload[0], "patient_name": "must-not-be-stored"}]
                        ),
                    },
                )

        with pytest.raises(DBAPIError, match="immutable"):
            async with session.begin_nested():
                await session.execute(
                    text(
                        "UPDATE courier_manifest_snapshots SET policy_version='changed' "
                        "WHERE id=:id"
                    ),
                    {"id": snapshot_id},
                )

        with pytest.raises(DBAPIError, match="immutable"):
            async with session.begin_nested():
                await session.execute(
                    text("DELETE FROM courier_manifest_snapshots WHERE id=:id"),
                    {"id": snapshot_id},
                )
        await session.rollback()


@pytest.mark.asyncio
async def test_offline_tombstone_identity_is_unique_durable_and_server_recorded() -> None:
    settings = get_settings()
    engine = create_async_engine(settings.migration_database_url)
    actor_id = uuid4()
    second_actor_id = uuid4()
    command_id = f"command-{uuid4()}"
    request_hash = "a" * 64
    recorded_at = datetime(2000, 1, 1, tzinfo=UTC)

    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            await connection.execute(
                text(
                    "INSERT INTO users (id, email, display_name, password_hash) "
                    "VALUES (:id, :email, 'Offline actor', 'not-used')"
                ),
                {"id": actor_id, "email": f"offline-{actor_id}@example.test"},
            )
            await connection.execute(
                text(
                    "INSERT INTO users (id, email, display_name, password_hash) "
                    "VALUES (:id, :email, 'Second offline actor', 'not-used')"
                ),
                {"id": second_actor_id, "email": f"offline-{second_actor_id}@example.test"},
            )
            tombstone = text(
                "INSERT INTO offline_command_tombstones "
                "(actor_user_id, client_command_id, request_hash, original_outcome, "
                "recorded_at, replay_response) VALUES "
                "(:actor_id, :command_id, :request_hash, 'APPLIED', :recorded_at, "
                "CAST(:replay_response AS jsonb))"
            )
            parameters = {
                "actor_id": actor_id,
                "command_id": command_id,
                "request_hash": request_hash,
                "recorded_at": recorded_at,
                "replay_response": '{"outcome":"APPLIED"}',
            }
            await connection.execute(tombstone, parameters)

            with pytest.raises(IntegrityError):
                async with connection.begin_nested():
                    await connection.execute(tombstone, parameters)

            await connection.execute(tombstone, {**parameters, "actor_id": second_actor_id})
            await connection.execute(text("DELETE FROM users WHERE id=:id"), {"id": actor_id})
            row = (
                (
                    await connection.execute(
                        text(
                            "SELECT request_hash, original_outcome, recorded_at, replay_response "
                            "FROM offline_command_tombstones WHERE actor_user_id=:actor_id "
                            "AND client_command_id=:command_id"
                        ),
                        {"actor_id": actor_id, "command_id": command_id},
                    )
                )
                .mappings()
                .one()
            )

            assert row["request_hash"] == request_hash
            assert row["original_outcome"] == "APPLIED"
            assert row["recorded_at"] > datetime(2026, 1, 1, tzinfo=UTC)
            assert row["replay_response"] == {"outcome": "APPLIED"}

            with pytest.raises(DBAPIError, match="immutable"):
                async with connection.begin_nested():
                    await connection.execute(
                        text(
                            "UPDATE offline_command_tombstones SET request_hash=:request_hash "
                            "WHERE actor_user_id=:actor_id AND client_command_id=:command_id"
                        ),
                        {
                            "request_hash": "b" * 64,
                            "actor_id": actor_id,
                            "command_id": command_id,
                        },
                    )
            await transaction.rollback()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_offline_idempotency_metadata_is_limited_to_its_namespace() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context = await create_pickup_fixture(client)

    async with get_session_factory()() as session:
        parameters = {
            "id": uuid4(),
            "actor_id": context["courier_id"],
            "key": f"offline-meta-{context['suffix']}",
            "request_hash": "c" * 64,
            "retention_expires_at": datetime.now(UTC) + timedelta(days=90),
            "causal_metadata": json.dumps({"post_pickup_state_version": 3}),
        }

        with pytest.raises(DBAPIError, match="offline_metadata_namespace"):
            async with session.begin_nested():
                await session.execute(
                    text(
                        "INSERT INTO idempotency_records "
                        "(id, actor_user_id, operation, key, request_hash, "
                        "offline_retention_expires_at, offline_causal_metadata) VALUES "
                        "(:id, :actor_id, 'shipments.online_mutation', :key, :request_hash, "
                        ":retention_expires_at, CAST(:causal_metadata AS jsonb))"
                    ),
                    parameters,
                )

        offline_id = uuid4()
        await session.execute(
            text(
                "INSERT INTO idempotency_records "
                "(id, actor_user_id, operation, key, request_hash, offline_retention_expires_at, "
                "offline_causal_metadata) VALUES "
                "(:id, :actor_id, 'courier_offline_command', :key, :request_hash, "
                ":retention_expires_at, CAST(:causal_metadata AS jsonb))"
            ),
            {**parameters, "id": offline_id},
        )
        record = (
            (
                await session.execute(
                    text(
                        "SELECT operation, offline_retention_expires_at, offline_causal_metadata "
                        "FROM idempotency_records WHERE id=:id"
                    ),
                    {"id": offline_id},
                )
            )
            .mappings()
            .one()
        )
        assert record["operation"] == "courier_offline_command"
        assert record["offline_retention_expires_at"] == parameters["retention_expires_at"]
        assert record["offline_causal_metadata"] == {"post_pickup_state_version": 3}


@pytest.mark.asyncio
async def test_pickup_state_version_is_managed_and_changes_across_withdrawal_retry() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context = await create_pickup_fixture(client)
        shipment_id = context["shipment_id"]
        pickup_url = f"/api/v1/shipments/{shipment_id}/handoffs/pickup"
        codes = {"container_codes": [item["code"] for item in context["containers"]]}

        first_release = await client.post(
            f"{pickup_url}/confirm-release",
            headers={
                **context["sender_auth"],
                "Idempotency-Key": f"offline-pickup-first-release-{context['suffix']}",
            },
            json=codes,
        )
        assert first_release.status_code == 200, first_release.text

        async with get_session_factory()() as session:
            first_pending_version = await session.scalar(
                text("SELECT pickup_state_version FROM shipments WHERE id=:id"),
                {"id": shipment_id},
            )
            assert first_pending_version == 2

            with pytest.raises(DBAPIError, match="database-managed"):
                async with session.begin_nested():
                    await session.execute(
                        text("UPDATE shipments SET pickup_state_version=1 WHERE id=:id"),
                        {"id": shipment_id},
                    )

        withdrawal = await client.post(
            f"{pickup_url}/withdraw",
            headers={
                **context["sender_auth"],
                "Idempotency-Key": f"offline-pickup-withdraw-{context['suffix']}",
            },
            json={"reason": "No physical transfer occurred"},
        )
        assert withdrawal.status_code == 200, withdrawal.text
        assert withdrawal.json()["handoff"]["status"] == "WITHDRAWN"

        async with get_session_factory()() as session:
            withdrawn_version = await session.scalar(
                text("SELECT pickup_state_version FROM shipments WHERE id=:id"),
                {"id": shipment_id},
            )
        assert withdrawn_version == first_pending_version + 1

        retry_release = await client.post(
            f"{pickup_url}/confirm-release",
            headers={
                **context["sender_auth"],
                "Idempotency-Key": f"offline-pickup-retry-release-{context['suffix']}",
            },
            json=codes,
        )
        assert retry_release.status_code == 200, retry_release.text
        assert retry_release.json()["handoff"]["attempt_no"] == 2

        async with get_session_factory()() as session:
            retry_pending_version = await session.scalar(
                text("SELECT pickup_state_version FROM shipments WHERE id=:id"),
                {"id": shipment_id},
            )

        assert retry_pending_version == withdrawn_version + 1
        assert retry_pending_version == first_pending_version + 2


@pytest.mark.asyncio
async def test_pickup_and_container_versions_advance_with_custody_transitions() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context = await create_pickup_fixture(client)
        shipment_id = context["shipment_id"]
        container = context["containers"][0]
        container_url = (
            f"/api/v1/shipments/{shipment_id}/containers/{container['id']}/handoffs/delivery"
        )

        async with get_session_factory()() as session:
            initial = (
                await session.execute(
                    text(
                        "SELECT s.pickup_state_version, c.state_version "
                        "FROM shipments s JOIN transport_containers c ON c.shipment_id=s.id "
                        "WHERE s.id=:shipment_id AND c.id=:container_id"
                    ),
                    {"shipment_id": shipment_id, "container_id": container["id"]},
                )
            ).one()
            container_version_type = await session.scalar(
                text(
                    "SELECT data_type FROM information_schema.columns "
                    "WHERE table_schema='public' AND table_name='transport_containers' "
                    "AND column_name='state_version'"
                )
            )

        assert initial.pickup_state_version == 1
        assert initial.state_version == 1
        assert container_version_type == "bigint"

        release = await client.post(
            f"/api/v1/shipments/{shipment_id}/handoffs/pickup/confirm-release",
            headers={
                **context["sender_auth"],
                "Idempotency-Key": f"offline-release-{context['suffix']}",
            },
            json={"container_codes": [item["code"] for item in context["containers"]]},
        )
        assert release.status_code == 200, release.text
        async with get_session_factory()() as session:
            pickup_pending_version = await session.scalar(
                text("SELECT pickup_state_version FROM shipments WHERE id=:id"),
                {"id": shipment_id},
            )
        assert pickup_pending_version == initial.pickup_state_version + 1

        pickup = await client.post(
            f"/api/v1/shipments/{shipment_id}/handoffs/pickup/confirm-pickup",
            headers={
                **context["courier_auth"],
                "Idempotency-Key": f"offline-pickup-{context['suffix']}",
            },
            json={"container_codes": [item["code"] for item in context["containers"]]},
        )
        assert pickup.status_code == 200, pickup.text
        async with get_session_factory()() as session:
            pickup_confirmed_version, transit_container_version = (
                await session.execute(
                    text(
                        "SELECT s.pickup_state_version, c.state_version FROM shipments s "
                        "JOIN transport_containers c ON c.shipment_id=s.id "
                        "WHERE s.id=:shipment_id AND c.id=:container_id"
                    ),
                    {"shipment_id": shipment_id, "container_id": container["id"]},
                )
            ).one()
        assert pickup_confirmed_version == pickup_pending_version + 1
        assert transit_container_version == initial.state_version + 1

        initiated = await client.post(
            f"{container_url}/confirm-delivery",
            headers={
                **context["courier_auth"],
                "Idempotency-Key": f"offline-delivery-{context['suffix']}",
            },
            json={"container_code": container["code"]},
        )
        assert initiated.status_code == 200, initiated.text
        withdrawn = await client.post(
            f"{container_url}/withdraw",
            headers={
                **context["courier_auth"],
                "Idempotency-Key": f"offline-delivery-withdraw-{context['suffix']}",
            },
            json={"reason": "Courier will retry delivery"},
        )
        assert withdrawn.status_code == 200, withdrawn.text

        async with get_session_factory()() as session:
            final_pickup_version, final_container_version = (
                await session.execute(
                    text(
                        "SELECT s.pickup_state_version, c.state_version FROM shipments s "
                        "JOIN transport_containers c ON c.shipment_id=s.id "
                        "WHERE s.id=:shipment_id AND c.id=:container_id"
                    ),
                    {"shipment_id": shipment_id, "container_id": container["id"]},
                )
            ).one()

        assert final_pickup_version == pickup_confirmed_version
        assert final_container_version == transit_container_version + 2


async def offline_ready(client, *, container_count=1):
    context = await create_pickup_fixture(client, container_count=container_count)
    release = await client.post(
        f"/api/v1/shipments/{context['shipment_id']}/handoffs/pickup/confirm-release",
        headers={**context["sender_auth"], "Idempotency-Key": str(uuid4())},
        json={"container_codes": [c["code"] for c in context["containers"]]},
    )
    assert release.status_code == 200, release.text
    response = await client.get(
        f"/api/v1/courier/shipments/{context['shipment_id']}/offline-manifest",
        headers=context["courier_auth"],
    )
    assert response.status_code == 200, response.text
    return context, response.json()


def pickup_command(snapshot):
    return {
        "client_command_id": str(uuid4()),
        "shipment_id": snapshot["shipment_id"],
        "action": "COURIER_CONFIRM_PICKUP",
        "container_codes": snapshot["container_codes"],
        "expected_manifest_version": snapshot["manifest_revision"],
        "expected_target_state_version": snapshot["pickup_state_version"],
        "client_performed_at": "2000-01-01T00:00:00Z",
        "snapshot_id": snapshot["snapshot_id"],
    }


def delivery_command(snapshot, *, predecessor=None, container_index=0):
    container = snapshot["containers"][container_index]
    command = {
        "client_command_id": str(uuid4()),
        "shipment_id": snapshot["shipment_id"],
        "action": "COURIER_INITIATE_CONTAINER_DELIVERY",
        "container_id": container["id"],
        "container_code": container["code"],
        "expected_manifest_version": snapshot["manifest_revision"],
        "expected_target_state_version": container["state_version"],
        "client_performed_at": "2099-01-01T00:00:00Z",
        "snapshot_id": snapshot["snapshot_id"],
    }
    if predecessor:
        command["depends_on_command_id"] = predecessor
    return command


async def sync_commands(client, context, commands):
    response = await client.post(
        "/api/v1/courier/sync", headers=context["courier_auth"], json={"commands": commands}
    )
    assert response.status_code == 200, response.text
    return response.json()["results"]


async def offline_counts(command_id):
    async with get_session_factory()() as session:
        return tuple(
            [
                await session.scalar(text(sql), {"key": command_id})
                for sql in (
                    "SELECT count(*) FROM idempotency_records "
                    "WHERE operation='courier_offline_command' AND key=:key",
                    "SELECT count(*) FROM offline_command_tombstones WHERE client_command_id=:key",
                    "SELECT count(*) FROM audit_events "
                    "WHERE operation='courier_offline_command' AND request_key=:key",
                )
            ]
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("split_batch", [False, True])
async def test_sync_linked_pickup_delivery_replay_and_authoritative_time(split_batch):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context, snapshot = await offline_ready(client)
        pickup = pickup_command(snapshot)
        delivery = delivery_command(snapshot, predecessor=pickup["client_command_id"])
        started = datetime.now(UTC)
        if split_batch:
            first = await sync_commands(client, context, [pickup])
            second = await sync_commands(client, context, [pickup, delivery])
            assert second[0]["outcome"] == "ALREADY_APPLIED"
            results = [first[0], second[1]]
        else:
            results = await sync_commands(client, context, [pickup, delivery])
        assert [r["outcome"] for r in results] == ["APPLIED", "APPLIED"]
        assert [r["server_status"] for r in results] == ["IN_TRANSIT", "DELIVERY_PENDING"]
        for command, result in zip([pickup, delivery], results, strict=True):
            assert datetime.fromisoformat(result["recorded_at"].replace("Z", "+00:00")) >= started
            assert datetime.fromisoformat(
                result["client_performed_at"].replace("Z", "+00:00")
            ) == datetime.fromisoformat(command["client_performed_at"].replace("Z", "+00:00"))
            assert await offline_counts(command["client_command_id"]) == (1, 1, 1)
        replay = await sync_commands(client, context, [pickup, delivery])
        assert [r["outcome"] for r in replay] == ["ALREADY_APPLIED", "ALREADY_APPLIED"]
        assert [r["original_outcome"] for r in replay] == ["APPLIED", "APPLIED"]
        assert [r["recorded_at"] for r in replay] == [r["recorded_at"] for r in results]
        async with get_session_factory()() as session:
            count = await session.scalar(
                text("SELECT count(*) FROM handoffs WHERE shipment_id=:id"),
                {"id": context["shipment_id"]},
            )
        assert count == 2


@pytest.mark.asyncio
async def test_sync_missing_predecessor_is_retryable_then_applies_in_array_order():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context, snapshot = await offline_ready(client)
        pickup = pickup_command(snapshot)
        delivery = delivery_command(snapshot, predecessor=pickup["client_command_id"])
        results = await sync_commands(client, context, [delivery, pickup])
        assert [r["outcome"] for r in results] == ["RETRYABLE", "APPLIED"]
        assert results[0]["recorded_at"] is None
        assert await offline_counts(delivery["client_command_id"]) == (0, 0, 0)
        assert (await sync_commands(client, context, [delivery]))[0]["outcome"] == "APPLIED"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode", ["unlinked", "rejected", "different_snapshot", "delivery_predecessor"]
)
async def test_sync_delivery_rejects_invalid_causal_dependency(mode):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context, snapshot = await offline_ready(client)
        pickup = pickup_command(snapshot)
        if mode == "rejected":
            pickup["container_codes"] = ["wrong-sealed-code"]
        first = (await sync_commands(client, context, [pickup]))[0]
        assert first["outcome"] == ("REJECTED" if mode == "rejected" else "APPLIED")
        if mode == "different_snapshot":
            response = await client.get(
                f"/api/v1/courier/shipments/{context['shipment_id']}/offline-manifest",
                headers=context["courier_auth"],
            )
            snapshot = response.json()
        delivery = delivery_command(
            snapshot, predecessor=None if mode == "unlinked" else pickup["client_command_id"]
        )
        if mode == "delivery_predecessor":
            assert (await sync_commands(client, context, [delivery]))[0]["outcome"] == "APPLIED"
            delivery = delivery_command(snapshot, predecessor=delivery["client_command_id"])
        result = (await sync_commands(client, context, [delivery]))[0]
        assert result["outcome"] == "REJECTED"
        assert result["error"]["code"] in {"OFFLINE_COMMAND_STALE", "INVALID_DEPENDENCY"}
        assert await offline_counts(delivery["client_command_id"]) == (1, 1, 1)
        replay = (await sync_commands(client, context, [delivery]))[0]
        assert replay["outcome"] == "ALREADY_APPLIED"
        assert replay["original_outcome"] == "REJECTED"
        assert replay["error"] == result["error"]


@pytest.mark.asyncio
async def test_sync_changed_payload_cannot_reuse_applied_or_rejected_id():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context, snapshot = await offline_ready(client)
        pickup = pickup_command(snapshot)
        assert (await sync_commands(client, context, [pickup]))[0]["outcome"] == "APPLIED"
        changed = {**pickup, "client_performed_at": "2001-01-01T00:00:00Z"}
        result = (await sync_commands(client, context, [changed]))[0]
        assert result["outcome"] == "REJECTED"
        assert result["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"
        assert await offline_counts(pickup["client_command_id"]) == (1, 1, 1)
        rejected = pickup_command(snapshot)
        assert (await sync_commands(client, context, [rejected]))[0]["outcome"] == "REJECTED"
        rejected["container_codes"] = ["changed-code"]
        assert (await sync_commands(client, context, [rejected]))[0]["error"][
            "code"
        ] == "IDEMPOTENCY_KEY_REUSED"


@pytest.mark.asyncio
@pytest.mark.parametrize("revocation", ["membership", "unassigned_actor", "user"])
async def test_sync_checks_current_authorization_before_replay(revocation):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context, snapshot = await offline_ready(client)
        pickup = pickup_command(snapshot)
        assert (await sync_commands(client, context, [pickup]))[0]["outcome"] == "APPLIED"
        auth = context["courier_auth"]
        if revocation == "unassigned_actor":
            _, auth = await authorize_unassigned_courier(context["shipment_id"], client)
        else:
            async with get_session_factory()() as session:
                sql = (
                    "UPDATE memberships SET is_active=false WHERE user_id=:id AND role='COURIER'"
                    if revocation == "membership"
                    else "UPDATE users SET is_active=false WHERE id=:id"
                )
                await session.execute(text(sql), {"id": context["courier_id"]})
                await session.commit()
        response = await client.post(
            "/api/v1/courier/sync", headers=auth, json={"commands": [pickup]}
        )
        if revocation == "user":
            assert response.status_code == 401
        else:
            assert response.status_code == 200
            result = response.json()["results"][0]
            assert result["outcome"] == "REJECTED"
            assert result["error"]["code"] == "RESOURCE_NOT_FOUND"
            assert result["recorded_at"] is None
        assert await offline_counts(pickup["client_command_id"]) == (1, 1, 1)


@pytest.mark.asyncio
async def test_sync_old_server_snapshot_requires_stored_review_without_custody_mutation(
    monkeypatch,
):
    from app.modules.shipments import offline

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context, snapshot = await offline_ready(client)
        command = pickup_command(snapshot)
        assert hasattr(offline, "server_now"), "sync server clock seam is missing"
        clock = offline.server_now

        async def old_clock(session):
            return await clock(session) + timedelta(hours=25)

        monkeypatch.setattr(offline, "server_now", old_clock)
        command["client_performed_at"] = datetime.now(UTC).isoformat()
        result = (await sync_commands(client, context, [command]))[0]
        assert result["outcome"] == "REVIEW_REQUIRED"
        assert result["error"]["code"] == "OFFLINE_SNAPSHOT_EXPIRED"
        assert await offline_counts(command["client_command_id"]) == (1, 1, 1)
        monkeypatch.setattr(offline, "server_now", clock)
        replay = (await sync_commands(client, context, [command]))[0]
        assert replay["original_outcome"] == "REVIEW_REQUIRED"
        async with get_session_factory()() as session:
            assert (
                await session.scalar(
                    text("SELECT status FROM shipments WHERE id=:id"),
                    {"id": context["shipment_id"]},
                )
                == "PICKUP_PENDING"
            )


@pytest.mark.asyncio
async def test_sync_pickup_aba_is_stale_and_unrelated_shipment_update_is_eligible():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context, snapshot = await offline_ready(client)
        async with get_session_factory()() as session:
            await session.execute(
                text("UPDATE shipments SET version=version+1 WHERE id=:id"),
                {"id": context["shipment_id"]},
            )
            await session.commit()
        assert (await sync_commands(client, context, [pickup_command(snapshot)]))[0][
            "outcome"
        ] == "APPLIED"
        assert (await sync_commands(client, context, [pickup_command(snapshot)]))[0]["error"][
            "code"
        ] == "OFFLINE_COMMAND_STALE"
        context, snapshot = await offline_ready(client)
        url = f"/api/v1/shipments/{context['shipment_id']}/handoffs/pickup"
        withdrawn = await client.post(
            f"{url}/withdraw",
            headers={**context["sender_auth"], "Idempotency-Key": str(uuid4())},
            json={"reason": "No physical release"},
        )
        assert withdrawn.status_code == 200, withdrawn.text
        release = await client.post(
            f"{url}/confirm-release",
            headers={**context["sender_auth"], "Idempotency-Key": str(uuid4())},
            json={"container_codes": snapshot["container_codes"]},
        )
        assert release.status_code == 200, release.text
        result = (await sync_commands(client, context, [pickup_command(snapshot)]))[0]
        assert result["error"]["code"] == "OFFLINE_COMMAND_STALE"


@pytest.mark.asyncio
async def test_sync_delivery_target_aba_invalidates_link_and_new_snapshot_can_apply():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context, snapshot = await offline_ready(client)
        pickup = pickup_command(snapshot)
        assert (await sync_commands(client, context, [pickup]))[0]["outcome"] == "APPLIED"
        container = snapshot["containers"][0]
        url = (
            f"/api/v1/shipments/{context['shipment_id']}/containers/"
            f"{container['id']}/handoffs/delivery"
        )
        for action, body in [
            ("confirm-delivery", {"container_code": container["code"]}),
            ("withdraw", {"reason": "Retry"}),
        ]:
            response = await client.post(
                f"{url}/{action}",
                headers={**context["courier_auth"], "Idempotency-Key": str(uuid4())},
                json=body,
            )
            assert response.status_code == 200, response.text
        result = (
            await sync_commands(
                client,
                context,
                [delivery_command(snapshot, predecessor=pickup["client_command_id"])],
            )
        )[0]
        assert result["error"]["code"] == "OFFLINE_COMMAND_STALE"
        fresh = await client.get(
            f"/api/v1/courier/shipments/{context['shipment_id']}/offline-manifest",
            headers=context["courier_auth"],
        )
        async with get_session_factory()() as session:
            await session.execute(
                text("UPDATE shipments SET version=version+1 WHERE id=:id"),
                {"id": context["shipment_id"]},
            )
            await session.commit()
        assert (await sync_commands(client, context, [delivery_command(fresh.json())]))[0][
            "outcome"
        ] == "APPLIED"


@pytest.mark.asyncio
async def test_sync_batch_validates_all_commands_before_any_commit():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context, snapshot = await offline_ready(client)
        pickup = pickup_command(snapshot)
        for commands in [
            [],
            [pickup] * 21,
            [pickup, {**pickup, "action": "UNKNOWN"}],
            [{**pickup, "client_performed_at": "2026-01-01"}],
            [{**pickup, "container_codes": ["duplicate", "duplicate"]}],
        ]:
            response = await client.post(
                "/api/v1/courier/sync", headers=context["courier_auth"], json={"commands": commands}
            )
            assert response.status_code == 422, response.text
            assert await offline_counts(pickup["client_command_id"]) == (0, 0, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["transient", "audit"])
async def test_sync_later_failure_rolls_back_only_current_command(monkeypatch, fault):
    from sqlalchemy import event
    from sqlalchemy.orm import Session

    from app.modules.shipments import offline

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context, snapshot = await offline_ready(client)
        pickup = pickup_command(snapshot)
        delivery = delivery_command(snapshot, predecessor=pickup["client_command_id"])
        assert hasattr(offline, "initiate_container_delivery_command"), (
            "offline command integration missing"
        )
        if fault == "transient":
            real = offline.initiate_container_delivery_command

            async def interrupted(*args, **kwargs):
                await real(*args, **kwargs)
                from sqlalchemy.exc import OperationalError

                raise OperationalError("connection interrupted", {}, Exception("transient"))

            monkeypatch.setattr(offline, "initiate_container_delivery_command", interrupted)
        else:
            from app.modules.audit.models import AuditEvent

            def fail_delivery_audit(session, flush_context, instances):
                if any(
                    isinstance(row, AuditEvent) and row.request_key == delivery["client_command_id"]
                    for row in session.new
                ):
                    raise RuntimeError("injected audit failure")

            event.listen(Session, "before_flush", fail_delivery_audit)
        try:
            results = await sync_commands(client, context, [pickup, delivery])
        finally:
            if fault == "audit":
                event.remove(Session, "before_flush", fail_delivery_audit)
        assert [r["outcome"] for r in results] == ["APPLIED", "RETRYABLE"]
        assert await offline_counts(pickup["client_command_id"]) == (1, 1, 1)
        assert await offline_counts(delivery["client_command_id"]) == (0, 0, 0)
        async with get_session_factory()() as session:
            assert (
                await session.scalar(
                    text("SELECT delivery_status FROM transport_containers WHERE id=:id"),
                    {"id": snapshot["containers"][0]["id"]},
                )
                == "IN_TRANSIT"
            )
        monkeypatch.undo()
        assert (await sync_commands(client, context, [delivery]))[0]["outcome"] == "APPLIED"


@pytest.mark.asyncio
async def test_sync_compaction_preserves_tombstone_identity_replay_and_minimum_retention():
    from app.modules.idempotency import service

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context, snapshot = await offline_ready(client)
        command = pickup_command(snapshot)
        original = (await sync_commands(client, context, [command]))[0]
        assert hasattr(service, "compact_offline_replays"), "offline compaction missing"
        async with get_session_factory()() as session:
            # A corrupt/early expiry must not shorten the promised 90-day retention.
            await session.execute(
                text(
                    "UPDATE idempotency_records SET offline_retention_expires_at=now() "
                    "WHERE operation='courier_offline_command' AND key=:id"
                ),
                {"id": command["client_command_id"]},
            )
            assert await service.compact_offline_replays(session) == 0
            await session.commit()
            before = (
                (
                    await session.execute(
                        text(
                            "SELECT * FROM offline_command_tombstones WHERE client_command_id=:id"
                        ),
                        {"id": command["client_command_id"]},
                    )
                )
                .mappings()
                .one()
            )
            assert (
                await service.compact_offline_replays(
                    session, as_of=datetime.now(UTC) + timedelta(days=91)
                )
                == 1
            )
            await session.commit()
            after = (
                (
                    await session.execute(
                        text(
                            "SELECT * FROM offline_command_tombstones WHERE client_command_id=:id"
                        ),
                        {"id": command["client_command_id"]},
                    )
                )
                .mappings()
                .one()
            )
            assert dict(before) == dict(after)
        assert await offline_counts(command["client_command_id"]) == (0, 1, 1)
        replay = (await sync_commands(client, context, [command]))[0]
        assert replay["outcome"] == "ALREADY_APPLIED"
        assert replay["original_outcome"] == "APPLIED"
        assert replay["recorded_at"] == original["recorded_at"]
        changed = {**command, "container_codes": ["changed"]}
        assert (await sync_commands(client, context, [changed]))[0]["error"][
            "code"
        ] == "IDEMPOTENCY_KEY_REUSED"
        assert await offline_counts(command["client_command_id"]) == (0, 1, 1)
        from app.cli import build_parser

        assert (
            build_parser().parse_args(["compact-offline-replays"]).command
            == "compact-offline-replays"
        )


def test_sync_runtime_openapi_conforms_to_historical_design_excerpt():
    contract = json.loads(
        (Path(__file__).resolve().parents[3] / "openapi/labtrack-v1.json").read_text()
    )
    runtime = app.openapi()
    path = "/courier/sync"
    assert f"/api/v1{path}" in runtime["paths"], "sync route is missing"
    actual = runtime["paths"][f"/api/v1{path}"]["post"]
    expected = contract["paths"][path]["post"]
    assert actual["operationId"] == expected["operationId"] == "syncCourierOfflineCommands"
    assert actual["requestBody"] == expected["requestBody"]
    assert actual["responses"] == expected["responses"]
    for name in [
        "CourierOfflinePickupCommand",
        "CourierOfflineContainerDeliveryCommand",
        "CourierSyncRequest",
        "CourierSyncError",
        "CourierSyncResult",
        "CourierSyncResponse",
    ]:
        assert runtime["components"]["schemas"][name] == contract["components"]["schemas"][name]


@pytest.mark.asyncio
@pytest.mark.parametrize("same_id", [False, True])
async def test_sync_concurrent_devices_apply_only_one_pickup(same_id):
    import asyncio

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context, snapshot = await offline_ready(client)
        first = pickup_command(snapshot)
        second = first if same_id else pickup_command(snapshot)
        results = await asyncio.gather(
            sync_commands(client, context, [first]), sync_commands(client, context, [second])
        )
        assert sorted(r[0]["outcome"] for r in results) == (
            ["ALREADY_APPLIED", "APPLIED"] if same_id else ["APPLIED", "REJECTED"]
        )
        if not same_id:
            assert (
                next(r[0] for r in results if r[0]["outcome"] == "REJECTED")["error"]["code"]
                == "OFFLINE_COMMAND_STALE"
            )
        async with get_session_factory()() as session:
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM handoffs WHERE shipment_id=:id "
                        "AND kind='PICKUP' AND status='CONFIRMED'"
                    ),
                    {"id": context["shipment_id"]},
                )
                == 1
            )
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM audit_events "
                        "WHERE operation='courier_offline_command' AND resource_id=:id "
                        "AND details->>'outcome'='APPLIED'"
                    ),
                    {"id": context["shipment_id"]},
                )
                == 1
            )


@pytest.mark.asyncio
async def test_sync_link_allows_other_container_delivery_but_rejects_spoofed_versions():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context, snapshot = await offline_ready(client, container_count=2)
        pickup = pickup_command(snapshot)
        commands = [pickup] + [
            delivery_command(snapshot, predecessor=pickup["client_command_id"], container_index=i)
            for i in range(2)
        ]
        assert [r["outcome"] for r in await sync_commands(client, context, commands)] == [
            "APPLIED"
        ] * 3
        forged = delivery_command(snapshot, predecessor=pickup["client_command_id"])
        forged["expected_target_state_version"] += 1
        assert (await sync_commands(client, context, [forged]))[0]["error"][
            "code"
        ] == "OFFLINE_COMMAND_STALE"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field", ["snapshot_id", "expected_manifest_version", "expected_target_state_version"]
)
async def test_sync_rejects_untrusted_snapshot_tokens_without_mutation(field):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context, snapshot = await offline_ready(client)
        command = pickup_command(snapshot)
        command[field] = str(uuid4()) if field == "snapshot_id" else command[field] + 1
        result = (await sync_commands(client, context, [command]))[0]
        assert result["outcome"] == "REJECTED"
        assert result["error"]["code"] == (
            "INVALID_SNAPSHOT" if field == "snapshot_id" else "OFFLINE_COMMAND_STALE"
        )
        assert await offline_counts(command["client_command_id"]) == (1, 1, 1)
        async with get_session_factory()() as session:
            assert (
                await session.scalar(
                    text("SELECT status FROM shipments WHERE id=:id"),
                    {"id": context["shipment_id"]},
                )
                == "PICKUP_PENDING"
            )


@pytest.mark.asyncio
async def test_sync_definite_failure_after_mutation_rolls_back_before_recording(monkeypatch):
    from fastapi import HTTPException

    from app.modules.shipments import offline

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context, snapshot = await offline_ready(client)
        real = offline.confirm_pickup_command

        async def rejected_after_change(*args, **kwargs):
            await real(*args, **kwargs)
            raise HTTPException(409, "Injected definite rejection after mutation")

        monkeypatch.setattr(offline, "confirm_pickup_command", rejected_after_change)
        command = pickup_command(snapshot)
        result = (await sync_commands(client, context, [command]))[0]
        assert result["outcome"] == "REJECTED"
        assert await offline_counts(command["client_command_id"]) == (1, 1, 1)
        async with get_session_factory()() as session:
            assert (
                await session.scalar(
                    text("SELECT status FROM shipments WHERE id=:id"),
                    {"id": context["shipment_id"]},
                )
                == "PICKUP_PENDING"
            )
            assert (
                await session.scalar(
                    text("SELECT delivery_status FROM transport_containers WHERE id=:id"),
                    {"id": snapshot["containers"][0]["id"]},
                )
                == "PREPARED"
            )
        monkeypatch.undo()
        assert (await sync_commands(client, context, [command]))[0][
            "original_outcome"
        ] == "REJECTED"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "age_delta,expected",
    [(timedelta(hours=24), "APPLIED"), (timedelta(hours=24, microseconds=1), "REVIEW_REQUIRED")],
)
async def test_sync_snapshot_age_uses_exact_server_boundary(monkeypatch, age_delta, expected):
    from app.modules.shipments import offline

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context, snapshot = await offline_ready(client)
        issued = datetime.fromisoformat(snapshot["issued_at"].replace("Z", "+00:00"))

        async def boundary_clock(session):
            return issued + age_delta

        monkeypatch.setattr(offline, "server_now", boundary_clock)
        result = (await sync_commands(client, context, [pickup_command(snapshot)]))[0]
        assert result["outcome"] == expected


@pytest.mark.asyncio
async def test_sync_revocation_between_commands_denies_replay_in_same_batch(monkeypatch):
    from app.modules.shipments import offline

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context, snapshot = await offline_ready(client)
        command = pickup_command(snapshot)
        real = offline.sync_offline_command
        calls = 0

        async def revoke_after_commit(*args, **kwargs):
            nonlocal calls
            result = await real(*args, **kwargs)
            calls += 1
            if calls == 1:
                async with get_session_factory()() as session:
                    await session.execute(
                        text(
                            "UPDATE memberships SET is_active=false "
                            "WHERE user_id=:id AND role='COURIER'"
                        ),
                        {"id": context["courier_id"]},
                    )
                    await session.commit()
            return result

        monkeypatch.setattr(offline, "sync_offline_command", revoke_after_commit)
        first, second = await sync_commands(client, context, [command, command])
        assert first["outcome"] == "APPLIED"
        assert second["outcome"] == "REJECTED"
        assert second["error"]["code"] == "RESOURCE_NOT_FOUND"
        assert second["recorded_at"] is None
        assert await offline_counts(command["client_command_id"]) == (1, 1, 1)


def test_offline_static_contract_has_no_dangling_local_references():
    contract = json.loads(
        (Path(__file__).resolve().parents[3] / "openapi/labtrack-v1.json").read_text()
    )

    def check_refs(value):
        if isinstance(value, dict):
            if "$ref" in value and value["$ref"].startswith("#/"):
                target = contract
                for segment in value["$ref"][2:].split("/"):
                    assert segment in target, f"Dangling reference: {value['$ref']}"
                    target = target[segment]
            for child in value.values():
                check_refs(child)
        elif isinstance(value, list):
            for child in value:
                check_refs(child)

    check_refs(contract)


@pytest.mark.asyncio
async def test_sync_other_actor_predecessor_is_terminal_without_exposing_foreign_outcome():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        foreign_context, foreign_snapshot = await offline_ready(client)
        foreign_pickup = pickup_command(foreign_snapshot)
        assert (await sync_commands(client, foreign_context, [foreign_pickup]))[0][
            "outcome"
        ] == "APPLIED"
        context, snapshot = await offline_ready(client)
        delivery = delivery_command(snapshot, predecessor=foreign_pickup["client_command_id"])
        result = (await sync_commands(client, context, [delivery]))[0]
        assert result["outcome"] == "REJECTED"
        assert result["error"]["code"] == "INVALID_DEPENDENCY"
        assert result["server_status"] == "PICKUP_PENDING"
        assert await offline_counts(delivery["client_command_id"]) == (1, 1, 1)


@pytest.mark.asyncio
async def test_sync_delivery_cannot_depend_on_itself():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context, snapshot = await offline_ready(client)
        command = delivery_command(snapshot)
        command["depends_on_command_id"] = command["client_command_id"]
        result = (await sync_commands(client, context, [command]))[0]
        assert result["outcome"] == "REJECTED"
        assert result["error"]["code"] == "INVALID_DEPENDENCY"
        assert await offline_counts(command["client_command_id"]) == (1, 1, 1)


@pytest.mark.asyncio
async def test_sync_compaction_during_replay_never_releases_command_identity(monkeypatch):
    import asyncio

    from app.modules.idempotency.service import compact_offline_replays
    from app.modules.shipments import offline

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context, snapshot = await offline_ready(client)
        command = pickup_command(snapshot)
        original = (await sync_commands(client, context, [command]))[0]
        looked_up = asyncio.Event()
        resume_replay = asyncio.Event()
        real_lock = offline.lock_offline_command

        async def pause_after_identity_lookup(*args, **kwargs):
            tombstone = await real_lock(*args, **kwargs)
            looked_up.set()
            await asyncio.wait_for(resume_replay.wait(), timeout=10)
            return tombstone

        monkeypatch.setattr(offline, "lock_offline_command", pause_after_identity_lookup)
        replay_task = asyncio.create_task(sync_commands(client, context, [command]))
        try:
            await asyncio.wait_for(looked_up.wait(), timeout=10)
            async with get_session_factory()() as session:
                assert (
                    await compact_offline_replays(
                        session, as_of=datetime.now(UTC) + timedelta(days=91)
                    )
                    == 1
                )
                await session.commit()
        finally:
            resume_replay.set()
        replay = (await replay_task)[0]
        assert replay["outcome"] == "ALREADY_APPLIED"
        assert replay["original_outcome"] == "APPLIED"
        assert replay["recorded_at"] == original["recorded_at"]
        assert await offline_counts(command["client_command_id"]) == (0, 1, 1)
        monkeypatch.undo()
        # The upper batch boundary is valid and every duplicate stays a replay.
        results = await sync_commands(client, context, [command] * 20)
        assert len(results) == 20
        assert {result["outcome"] for result in results} == {"ALREADY_APPLIED"}
        changed = {**command, "client_performed_at": "2002-01-01T00:00:00Z"}
        assert (await sync_commands(client, context, [changed]))[0]["error"][
            "code"
        ] == "IDEMPOTENCY_KEY_REUSED"
        assert await offline_counts(command["client_command_id"]) == (0, 1, 1)
