import asyncio
import json
from datetime import UTC, datetime, timedelta
from importlib import import_module, util
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import get_settings
from app.core.security import hash_password
from app.db.session import get_session_factory
from app.main import app
from app.modules.audit.models import AuditEvent
from app.modules.idempotency.models import IdempotencyRecord
from app.modules.identity.access import MembershipGrant, Principal
from app.modules.identity.models import Membership, Organization, Region, User
from app.modules.shipments.models import Handoff, Shipment, TransportContainer

PASSWORD = "Pickup Test Password 2026!"


def shared_courier_command(name: str):
    module_name = "app.modules.shipments.commands"
    assert util.find_spec(module_name) is not None, "shared custody commands module is missing"
    return getattr(import_module(module_name), name)


def test_stage_c_openapi_shapes_match_the_design_contract() -> None:
    contract_path = Path(__file__).resolve().parents[3] / "openapi" / "labtrack-v1.json"
    contract = json.loads(contract_path.read_text())
    generated = app.openapi()
    paths = (
        "/shipments/{shipment_id}/handoffs/pickup/confirm-release",
        "/shipments/{shipment_id}/handoffs/pickup/confirm-pickup",
        "/shipments/{shipment_id}/handoffs/pickup/withdraw",
        "/shipments/{shipment_id}/cancel",
    )
    for path in paths:
        expected_operation = contract["paths"][path]["post"]
        actual_operation = generated["paths"][f"/api/v1{path}"]["post"]
        assert set(actual_operation["responses"]) == set(expected_operation["responses"])
        if path.endswith("/withdraw"):
            assert actual_operation["description"] == expected_operation["description"]
        expected_request = expected_operation["requestBody"]["content"]["application/json"][
            "schema"
        ]["$ref"].rsplit("/", 1)[1]
        actual_request = actual_operation["requestBody"]["content"]["application/json"]["schema"][
            "$ref"
        ].rsplit("/", 1)[1]
        assert actual_request == expected_request
        for status in ("401", "403", "404", "409", "422", "500", "503"):
            assert status in actual_operation["responses"]
        expected_response = expected_operation["responses"]["200"]["content"]["application/json"][
            "schema"
        ]["$ref"].rsplit("/", 1)[1]
        actual_response = actual_operation["responses"]["200"]["content"]["application/json"][
            "schema"
        ]["$ref"].rsplit("/", 1)[1]
        expected_schema = contract["components"]["schemas"][expected_response]
        actual_schema = generated["components"]["schemas"][actual_response]
        assert set(actual_schema["properties"]) == set(expected_schema["properties"])
        assert set(actual_schema["required"]) == set(expected_schema["required"])

    static = contract["components"]["schemas"]
    runtime = generated["components"]["schemas"]
    for name in ("ContainerCodesRequest", "ReasonRequest"):
        assert set(runtime[name]["properties"]) == set(static[name]["properties"])
        assert set(runtime[name]["required"]) == set(static[name]["required"])
        assert runtime[name]["additionalProperties"] is False
    codes_schema = runtime["ContainerCodesRequest"]["properties"]["container_codes"]
    assert (
        codes_schema["minItems"]
        == static["ContainerCodesRequest"]["properties"]["container_codes"]["minItems"]
    )
    assert codes_schema["uniqueItems"] is True
    runtime_handoff = runtime["HandoffResponse"]
    static_handoff = static["Handoff"]
    assert set(runtime_handoff["properties"]) == set(static_handoff["properties"])
    assert set(runtime_handoff["required"]) == set(static_handoff["required"])
    assert runtime_handoff["additionalProperties"] is False
    static_error = static["ErrorResponse"]["properties"]["error"]
    runtime_error = runtime["APIError"]["properties"]
    assert set(runtime_error) == set(static_error["properties"])
    assert set(runtime["APIError"]["required"]) == set(static_error["required"])
    for status in ("401", "403", "404", "409", "422", "500", "503"):
        error_ref = generated["paths"][
            "/api/v1/shipments/{shipment_id}/handoffs/pickup/confirm-release"
        ]["post"]["responses"][status]["content"]["application/json"]["schema"]["$ref"]
        error_schema = generated["components"]["schemas"][error_ref.rsplit("/", 1)[1]]
        assert "error" in error_schema["properties"]


async def create_pickup_fixture(
    client: AsyncClient, *, shared_actor: bool = False, container_count: int = 1
) -> dict:
    async with get_session_factory()() as session:
        region = await session.scalar(select(Region).where(Region.code == "PILOT"))
        destination = await session.scalar(
            select(Organization).where(Organization.code == "CENTRAL_LAB")
        )
        assert region is not None and destination is not None
        suffix = uuid4().hex[:10]
        sender_org = Organization(
            region_id=region.id,
            code=f"PICKUP-{suffix}",
            name="Pickup test sender",
            organization_type="HEALTHCARE_FACILITY",
        )
        sender = User(
            email=f"sender-{suffix}@example.test",
            display_name="Pickup Sender",
            password_hash=hash_password(PASSWORD),
        )
        courier = (
            sender
            if shared_actor
            else User(
                email=f"courier-{suffix}@example.test",
                display_name="Pickup Courier",
                password_hash=hash_password(PASSWORD),
            )
        )
        session.add_all([sender_org, sender, courier])
        await session.flush()
        memberships = [
            Membership(
                user_id=sender.id,
                organization_id=sender_org.id,
                role="SENDER_OPERATOR",
            )
        ]
        memberships.append(Membership(user_id=courier.id, region_id=region.id, role="COURIER"))
        session.add_all(memberships)
        await session.commit()
        sender_id = sender.id
        sender_org_id = sender_org.id
        courier_id = courier.id
        destination_id = destination.id
        sender_email = sender.email
        courier_email = courier.email

    sender_login = await client.post(
        "/api/v1/auth/token", json={"email": sender_email, "password": PASSWORD}
    )
    courier_login = await client.post(
        "/api/v1/auth/token", json={"email": courier_email, "password": PASSWORD}
    )
    assert sender_login.status_code == courier_login.status_code == 200
    sender_auth = {"Authorization": f"Bearer {sender_login.json()['access_token']}"}
    courier_auth = {"Authorization": f"Bearer {courier_login.json()['access_token']}"}

    samples = []
    for index in range(container_count):
        created_sample = await client.post(
            "/api/v1/samples",
            headers={**sender_auth, "Idempotency-Key": f"pickup-sample-{suffix}-{index}"},
            json={
                "origin_organization_id": str(sender_org_id),
                "material_type": "BLOOD",
                "collected_at": None,
            },
        )
        assert created_sample.status_code == 201, created_sample.text
        samples.append(created_sample.json())
    shipment_response = await client.post(
        "/api/v1/shipments",
        headers={**sender_auth, "Idempotency-Key": f"pickup-shipment-{suffix}"},
        json={
            "sender_org_id": str(sender_org_id),
            "destination_org_id": str(destination_id),
            "expected_delivery_at": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
        },
    )
    assert shipment_response.status_code == 201, shipment_response.text
    shipment_id = shipment_response.json()["id"]
    assigned = await client.patch(
        f"/api/v1/shipments/{shipment_id}/courier",
        headers={**sender_auth, "Idempotency-Key": f"pickup-assign-{suffix}"},
        json={"courier_user_id": str(courier_id)},
    )
    assert assigned.status_code == 200, assigned.text
    containers = []
    for index, sample in enumerate(samples):
        container_response = await client.post(
            f"/api/v1/shipments/{shipment_id}/containers",
            headers={**sender_auth, "Idempotency-Key": f"pickup-container-{suffix}-{index}"},
        )
        assert container_response.status_code == 201, container_response.text
        container = container_response.json()
        containers.append(container)
        item_response = await client.post(
            f"/api/v1/shipments/{shipment_id}/containers/{container['id']}/items",
            headers={**sender_auth, "Idempotency-Key": f"pickup-item-{suffix}-{index}"},
            json={"sample_id": sample["id"]},
        )
        assert item_response.status_code == 201, item_response.text
    sealed = await client.post(
        f"/api/v1/shipments/{shipment_id}/seal",
        headers={**sender_auth, "Idempotency-Key": f"pickup-seal-{suffix}"},
    )
    assert sealed.status_code == 200, sealed.text

    return {
        "shipment_id": shipment_id,
        "container_id": containers[0]["id"],
        "container_code": containers[0]["code"],
        "containers": containers,
        "sender_id": sender_id,
        "courier_id": courier_id,
        "sender_auth": sender_auth,
        "courier_auth": courier_auth,
        "suffix": suffix,
    }


@pytest.mark.asyncio
async def test_database_rejects_unconfirmed_direct_pickup_state_changes() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context = await create_pickup_fixture(client)

    async with get_session_factory()() as session:
        with pytest.raises(
            DBAPIError, match="shipment status transition requires a Pickup handoff"
        ):
            await session.execute(
                text("UPDATE shipments SET status='PICKUP_PENDING' WHERE id=:id"),
                {"id": context["shipment_id"]},
            )
        await session.rollback()
        with pytest.raises(DBAPIError, match="confirmed Pickup is required before transit"):
            await session.execute(
                text(
                    "UPDATE transport_containers SET delivery_status='IN_TRANSIT', "
                    "state_version=state_version+1 WHERE id=:id"
                ),
                {"id": context["container_id"]},
            )
        await session.rollback()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        orphan_context = await create_pickup_fixture(client)
    async with get_session_factory()() as session:
        await session.execute(
            text(
                "INSERT INTO handoffs "
                "(id, shipment_id, kind, container_id, attempt_no, status, initiated_by) "
                "VALUES (:id, :shipment_id, 'PICKUP', NULL, 1, 'PENDING', :sender_id)"
            ),
            {
                "id": uuid4(),
                "shipment_id": orphan_context["shipment_id"],
                "sender_id": orphan_context["sender_id"],
            },
        )
        with pytest.raises(DBAPIError, match="Active Pickup handoff must match shipment state"):
            await session.commit()
        await session.rollback()


@pytest.mark.asyncio
async def test_database_handoff_requires_a_pending_start_and_distinct_confirming_actor() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context = await create_pickup_fixture(client)

    async with get_session_factory()() as session:
        handoff_id = uuid4()
        await session.execute(
            text(
                "INSERT INTO handoffs "
                "(id, shipment_id, kind, container_id, attempt_no, status, initiated_by) "
                "VALUES (:id, :shipment_id, 'PICKUP', NULL, 1, 'PENDING', :sender_id)"
            ),
            {
                "id": handoff_id,
                "shipment_id": context["shipment_id"],
                "sender_id": context["sender_id"],
            },
        )
        await session.execute(
            text("UPDATE shipments SET status='PICKUP_PENDING' WHERE id=:id"),
            {"id": context["shipment_id"]},
        )
        await session.commit()
        with pytest.raises(
            DBAPIError, match="Pickup confirmation requires a different assigned Courier"
        ):
            await session.execute(
                text(
                    "UPDATE handoffs SET status='CONFIRMED', confirmed_by=:sender_id, "
                    "confirmed_at=now() WHERE id=:id"
                ),
                {"id": handoff_id, "sender_id": context["sender_id"]},
            )
        await session.rollback()


@pytest.mark.asyncio
async def test_bilateral_pickup_is_atomic_audited_and_idempotent() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context = await create_pickup_fixture(client)
        shipment_id = context["shipment_id"]
        codes = {"container_codes": [context["container_code"]]}
        courier_release = await client.post(
            f"/api/v1/shipments/{shipment_id}/handoffs/pickup/confirm-release",
            headers={
                **context["courier_auth"],
                "Idempotency-Key": f"pickup-courier-release-{context['suffix']}",
            },
            json=codes,
        )
        assert courier_release.status_code == 404
        assert courier_release.json()["error"]["code"] == "RESOURCE_NOT_FOUND"
        assert (
            courier_release.json()["error"]["request_id"] == courier_release.headers["x-request-id"]
        )
        release_headers = {
            **context["sender_auth"],
            "Idempotency-Key": f"pickup-release-{context['suffix']}",
        }
        release = await client.post(
            f"/api/v1/shipments/{shipment_id}/handoffs/pickup/confirm-release",
            headers=release_headers,
            json=codes,
        )
        assert release.status_code == 200, release.text
        assert release.json()["shipment_status"] == "PICKUP_PENDING"
        assert release.json()["handoff"]["status"] == "PENDING"
        replay = await client.post(
            f"/api/v1/shipments/{shipment_id}/handoffs/pickup/confirm-release",
            headers=release_headers,
            json=codes,
        )
        assert replay.status_code == 200 and replay.json() == release.json()

        confirm = await client.post(
            f"/api/v1/shipments/{shipment_id}/handoffs/pickup/confirm-pickup",
            headers={
                **context["courier_auth"],
                "Idempotency-Key": f"pickup-confirm-{context['suffix']}",
            },
            json=codes,
        )
        assert confirm.status_code == 200, confirm.text
        assert confirm.json()["shipment_status"] == "IN_TRANSIT"
        assert confirm.json()["handoff"]["status"] == "CONFIRMED"
        assert confirm.json()["container_id"] is None
        assert confirm.json()["container_status"] is None
        assert confirm.json()["handoff"]["container_id"] is None
        assert confirm.json()["handoff"]["withdrawal_reason"] is None
        assert "superseded_by_incident_id" in confirm.json()["handoff"]
        second_replay = await client.post(
            f"/api/v1/shipments/{shipment_id}/handoffs/pickup/confirm-pickup",
            headers={
                **context["courier_auth"],
                "Idempotency-Key": f"pickup-confirm-{context['suffix']}",
            },
            json=codes,
        )
        assert second_replay.status_code == 200
        assert second_replay.json() == confirm.json()
        cancel_after_pickup = await client.post(
            f"/api/v1/shipments/{shipment_id}/cancel",
            headers={
                **context["sender_auth"],
                "Idempotency-Key": f"pickup-cancel-confirmed-{context['suffix']}",
            },
            json={"reason": "Cannot cancel after custody transfer"},
        )
        assert cancel_after_pickup.status_code == 409

        async with get_session_factory()() as session:
            courier_membership = await session.scalar(
                select(Membership).where(
                    Membership.user_id == context["courier_id"],
                    Membership.role == "COURIER",
                )
            )
            assert courier_membership is not None
            courier_membership.is_active = False
            await session.commit()
        revoked_replay = await client.post(
            f"/api/v1/shipments/{shipment_id}/handoffs/pickup/confirm-pickup",
            headers={
                **context["courier_auth"],
                "Idempotency-Key": f"pickup-confirm-{context['suffix']}",
            },
            json=codes,
        )
        assert revoked_replay.status_code == 404

    async with get_session_factory()() as session:
        shipment = await session.get(Shipment, shipment_id)
        container = await session.get(TransportContainer, context["container_id"])
        assert shipment is not None and shipment.status == "IN_TRANSIT" and shipment.version == 7
        assert container is not None and container.delivery_status == "IN_TRANSIT"
        assert container.state_version == 2
        handoff_id = confirm.json()["handoff"]["id"]
        audit_rows = list(
            await session.scalars(select(AuditEvent).where(AuditEvent.resource_id == handoff_id))
        )
        assert len(audit_rows) == 2


@pytest.mark.asyncio
async def test_pickup_requires_complete_codes_different_actor_and_withdrawal_before_cancel() -> (
    None
):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context = await create_pickup_fixture(client, shared_actor=True)
        shipment_id = context["shipment_id"]
        release_url = f"/api/v1/shipments/{shipment_id}/handoffs/pickup/confirm-release"
        release_headers = {
            **context["sender_auth"],
            "Idempotency-Key": f"pickup-release-{context['suffix']}",
        }
        incomplete = await client.post(
            release_url,
            headers=release_headers,
            json={"container_codes": ["NOT-THE-CONTAINER"]},
        )
        assert incomplete.status_code == 409
        assert incomplete.json()["error"]["code"] == "MANIFEST_MISMATCH"
        duplicate_codes = await client.post(
            release_url,
            headers={
                **context["sender_auth"],
                "Idempotency-Key": f"pickup-duplicate-codes-{context['suffix']}",
            },
            json={
                "container_codes": [
                    context["container_code"],
                    context["container_code"],
                ]
            },
        )
        assert duplicate_codes.status_code == 422
        assert duplicate_codes.json()["error"]["code"] == "VALIDATION_ERROR"
        released = await client.post(
            release_url,
            headers=release_headers,
            json={"container_codes": [context["container_code"]]},
        )
        assert released.status_code == 200, released.text
        same_actor_confirm = await client.post(
            f"/api/v1/shipments/{shipment_id}/handoffs/pickup/confirm-pickup",
            headers={
                **context["sender_auth"],
                "Idempotency-Key": f"pickup-confirm-{context['suffix']}",
            },
            json={"container_codes": [context["container_code"]]},
        )
        assert same_actor_confirm.status_code == 409
        cancel_pending = await client.post(
            f"/api/v1/shipments/{shipment_id}/cancel",
            headers={
                **context["sender_auth"],
                "Idempotency-Key": f"pickup-cancel-pending-{context['suffix']}",
            },
            json={"reason": "Physical transfer has not occurred"},
        )
        assert cancel_pending.status_code == 409
        withdrawn = await client.post(
            f"/api/v1/shipments/{shipment_id}/handoffs/pickup/withdraw",
            headers={
                **context["sender_auth"],
                "Idempotency-Key": f"pickup-withdraw-{context['suffix']}",
            },
            json={"reason": "Courier did not arrive"},
        )
        assert withdrawn.status_code == 200, withdrawn.text
        assert withdrawn.json()["handoff"]["status"] == "WITHDRAWN"
        handoff_id = withdrawn.json()["handoff"]["id"]
        retry = await client.post(
            release_url,
            headers={
                **context["sender_auth"],
                "Idempotency-Key": f"pickup-release-retry-{context['suffix']}",
            },
            json={"container_codes": [context["container_code"]]},
        )
        assert retry.status_code == 200, retry.text
        assert retry.json()["handoff"]["attempt_no"] == 2
        retry_withdrawal = await client.post(
            f"/api/v1/shipments/{shipment_id}/handoffs/pickup/withdraw",
            headers={
                **context["sender_auth"],
                "Idempotency-Key": f"pickup-withdraw-retry-{context['suffix']}",
            },
            json={"reason": "Second attempt was also cancelled before transfer"},
        )
        assert retry_withdrawal.status_code == 200, retry_withdrawal.text
        cancelled = await client.post(
            f"/api/v1/shipments/{shipment_id}/cancel",
            headers={
                **context["sender_auth"],
                "Idempotency-Key": f"pickup-cancel-{context['suffix']}",
            },
            json={"reason": "No physical custody transfer"},
        )
        assert cancelled.status_code == 200, cancelled.text
        assert cancelled.json()["status"] == "CANCELLED"

    async with get_session_factory()() as session:
        withdrawal_audit = await session.scalar(
            select(AuditEvent).where(
                AuditEvent.resource_id == handoff_id,
                AuditEvent.operation.like("%.handoffs.pickup.withdraw"),
            )
        )
        assert withdrawal_audit is not None
        assert withdrawal_audit.details == {
            "shipment_id": shipment_id,
            "reason": "Courier did not arrive",
            "physical_release_occurred": False,
            "containers_remain_at_origin": True,
        }


@pytest.mark.asyncio
async def test_concurrent_pickup_confirmations_with_distinct_keys_commit_once() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context = await create_pickup_fixture(client)
        shipment_id = context["shipment_id"]
        codes = {"container_codes": [context["container_code"]]}
        release = await client.post(
            f"/api/v1/shipments/{shipment_id}/handoffs/pickup/confirm-release",
            headers={
                **context["sender_auth"],
                "Idempotency-Key": f"pickup-release-{context['suffix']}",
            },
            json=codes,
        )
        assert release.status_code == 200, release.text
        url = f"/api/v1/shipments/{shipment_id}/handoffs/pickup/confirm-pickup"

        async def confirm(index: int):
            return await client.post(
                url,
                headers={
                    **context["courier_auth"],
                    "Idempotency-Key": f"pickup-race-{context['suffix']}-{index}",
                },
                json=codes,
            )

        first, second = await asyncio.gather(confirm(1), confirm(2))
        assert sorted([first.status_code, second.status_code]) == [200, 409]

    async with get_session_factory()() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(Handoff)
                .where(Handoff.shipment_id == shipment_id, Handoff.status == "CONFIRMED")
            )
            == 1
        )


@pytest.mark.asyncio
async def test_audit_failure_rolls_back_pickup_handoff_state_and_idempotency() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context = await create_pickup_fixture(client)
    shipment_id = context["shipment_id"]
    key = f"pickup-audit-fail-{context['suffix']}"
    operation = f"shipments.{shipment_id}.handoffs.pickup.confirm-release"
    owner_engine = create_async_engine(get_settings().migration_database_url)
    try:
        async with owner_engine.begin() as connection:
            await connection.execute(
                text("""
                CREATE FUNCTION reject_pickup_release_audit() RETURNS trigger AS $$
                BEGIN
                  IF NEW.operation LIKE '%.handoffs.pickup.confirm-release' THEN
                    RAISE EXCEPTION 'Injected Pickup audit failure';
                  END IF;
                  RETURN NEW;
                END; $$ LANGUAGE plpgsql
                """)
            )
            await connection.execute(
                text("""
                CREATE TRIGGER trg_test_reject_pickup_release_audit
                BEFORE INSERT ON audit_events FOR EACH ROW
                EXECUTE FUNCTION reject_pickup_release_audit()
                """)
            )

        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as client:
            response = await client.post(
                f"/api/v1/shipments/{shipment_id}/handoffs/pickup/confirm-release",
                headers={
                    **context["sender_auth"],
                    "Idempotency-Key": key,
                },
                json={"container_codes": [context["container_code"]]},
            )
        assert response.status_code == 500
        assert response.json()["error"]["code"] == "INTERNAL_ERROR"
        assert response.json()["error"]["message"] == "Internal server error"
    finally:
        async with owner_engine.begin() as connection:
            await connection.execute(
                text("DROP TRIGGER IF EXISTS trg_test_reject_pickup_release_audit ON audit_events")
            )
            await connection.execute(text("DROP FUNCTION IF EXISTS reject_pickup_release_audit()"))
        await owner_engine.dispose()

    async with get_session_factory()() as session:
        shipment = await session.get(Shipment, shipment_id)
        assert shipment is not None and shipment.status == "READY_FOR_PICKUP"
        assert (
            await session.scalar(
                select(func.count()).select_from(Handoff).where(Handoff.shipment_id == shipment_id)
            )
            == 0
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(IdempotencyRecord)
                .where(
                    IdempotencyRecord.actor_user_id == context["sender_id"],
                    IdempotencyRecord.operation == operation,
                    IdempotencyRecord.key == key,
                )
            )
            == 0
        )


@pytest.mark.asyncio
async def test_shared_pickup_command_preserves_courier_confirmation_contract() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context = await create_pickup_fixture(client, container_count=2)
        codes = [item["code"] for item in context["containers"]]
        release = await client.post(
            f"/api/v1/shipments/{context['shipment_id']}/handoffs/pickup/confirm-release",
            headers={
                **context["sender_auth"],
                "Idempotency-Key": f"shared-pickup-release-{context['suffix']}",
            },
            json={"container_codes": codes},
        )
        assert release.status_code == 200, release.text

        confirm_pickup_command = shared_courier_command("confirm_pickup_command")
        async with get_session_factory()() as session:
            shipment = await session.get(Shipment, UUID(context["shipment_id"]))
            courier = await session.get(User, context["courier_id"])
            assert shipment is not None and courier is not None
            principal = Principal(
                user_id=courier.id,
                email=courier.email,
                display_name=courier.display_name,
                memberships=(
                    MembershipGrant("COURIER", organization_id=None, region_id=shipment.region_id),
                ),
            )
            status_code, body = await confirm_pickup_command(
                session, principal, UUID(context["shipment_id"]), codes
            )
            await session.commit()

        assert status_code == 200
        assert body["shipment_id"] == context["shipment_id"]
        assert body["shipment_status"] == "IN_TRANSIT"
        assert body["handoff"]["kind"] == "PICKUP"
        assert body["handoff"]["status"] == "CONFIRMED"
        assert body["handoff"]["initiated_by"] == str(context["sender_id"])
        assert body["handoff"]["confirmed_by"] == str(context["courier_id"])
        assert body["container_id"] is None
        assert body["container_status"] is None

        async with get_session_factory()() as session:
            statuses = list(
                await session.scalars(
                    select(TransportContainer.delivery_status).where(
                        TransportContainer.shipment_id == context["shipment_id"]
                    )
                )
            )
            assert statuses == ["IN_TRANSIT"] * len(context["containers"])
