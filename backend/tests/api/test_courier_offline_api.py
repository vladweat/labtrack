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


def test_offline_manifest_runtime_openapi_matches_static_contract() -> None:
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
    revision = script.get_revision(script.get_current_head())

    assert revision is not None
    assert revision.revision == "0009_stage_g_offline"
    assert revision.down_revision == "0008_stage_f_integrity"


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
