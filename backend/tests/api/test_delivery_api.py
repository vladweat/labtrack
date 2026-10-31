"""Normal per-container Delivery and partial-receipt acceptance tests."""

import json
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from test_pickup_api import PASSWORD, create_pickup_fixture

from app.core.security import hash_password
from app.db.session import get_session_factory
from app.main import app
from app.modules.audit.models import AuditEvent
from app.modules.identity.models import Membership, Organization, User
from app.modules.shipments.models import ContainerItem, Shipment, TransportContainer


def test_delivery_schema_is_an_additive_migration_after_pickup() -> None:
    backend = Path(__file__).resolve().parents[2]
    config = Config(str(backend / "alembic.ini"))
    config.set_main_option("script_location", str(backend / "alembic"))
    head = ScriptDirectory.from_config(config).get_current_head()
    assert head == "0009_stage_g_offline"


async def authorize_lab_operator(shipment_id: str, client: AsyncClient) -> dict[str, str]:
    suffix = uuid4().hex[:10]
    async with get_session_factory()() as session:
        shipment = await session.get(Shipment, shipment_id)
        assert shipment is not None
        destination = await session.get(Organization, shipment.destination_organization_id)
        assert destination is not None
        operator = User(
            email=f"lab-operator-{suffix}@example.test",
            display_name="Delivery Lab Operator",
            password_hash=hash_password(PASSWORD),
        )
        session.add(operator)
        await session.flush()
        session.add(
            Membership(
                user_id=operator.id,
                organization_id=destination.id,
                role="LAB_OPERATOR",
            )
        )
        await session.commit()
        email = operator.email
    login = await client.post(
        "/api/v1/auth/token",
        json={"email": email, "password": PASSWORD},
    )
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


async def confirm_pickup(context: dict, client: AsyncClient) -> None:
    codes = [container["code"] for container in context["containers"]]
    release = await client.post(
        f"/api/v1/shipments/{context['shipment_id']}/handoffs/pickup/confirm-release",
        headers={
            **context["sender_auth"],
            "Idempotency-Key": f"delivery-release-{context['suffix']}",
        },
        json={"container_codes": codes},
    )
    assert release.status_code == 200, release.text
    pickup = await client.post(
        f"/api/v1/shipments/{context['shipment_id']}/handoffs/pickup/confirm-pickup",
        headers={
            **context["courier_auth"],
            "Idempotency-Key": f"delivery-pickup-{context['suffix']}",
        },
        json={"container_codes": codes},
    )
    assert pickup.status_code == 200, pickup.text


@pytest.mark.asyncio
async def test_delivery_openapi_paths_match_the_design_contract() -> None:
    contract = json.loads(
        (Path(__file__).resolve().parents[3] / "openapi" / "labtrack-v1.json").read_text(
            encoding="utf-8"
        )
    )
    generated = app.openapi()
    paths = (
        "/shipments/{shipment_id}/containers/{container_id}/handoffs/delivery/confirm-delivery",
        "/shipments/{shipment_id}/containers/{container_id}/handoffs/delivery/confirm-receipt",
        "/shipments/{shipment_id}/containers/{container_id}/handoffs/delivery/withdraw",
        "/shipments/{shipment_id}/containers/{container_id}/handoffs/delivery",
    )
    for path in paths:
        expected = contract["paths"][path]
        method = "get" if path.endswith("/delivery") else "post"
        actual = generated["paths"].get(f"/api/v1{path}", {}).get(method)
        assert actual is not None, f"runtime operation missing: {method.upper()} {path}"
        expected_operation = expected[method]
        assert actual["operationId"] == expected_operation["operationId"]
        assert set(actual["responses"]) == set(expected_operation["responses"])
        if "requestBody" in expected_operation:
            expected_request = expected_operation["requestBody"]["content"]["application/json"][
                "schema"
            ]["$ref"].rsplit("/", 1)[1]
            actual_request = actual["requestBody"]["content"]["application/json"]["schema"][
                "$ref"
            ].rsplit("/", 1)[1]
            assert actual_request == expected_request
        expected_response = expected_operation["responses"]["200"]["content"]["application/json"][
            "schema"
        ]["$ref"].rsplit("/", 1)[1]
        actual_response = actual["responses"]["200"]["content"]["application/json"]["schema"][
            "$ref"
        ].rsplit("/", 1)[1]
        assert actual_response == expected_response
        assert set(generated["components"]["schemas"][actual_response]["properties"]) == set(
            contract["components"]["schemas"][expected_response]["properties"]
        )


@pytest.mark.asyncio
async def test_delivery_receipt_is_bilateral_partial_and_does_not_set_sample_results() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context = await create_pickup_fixture(client, container_count=3)
        await confirm_pickup(context, client)
        lab_auth = await authorize_lab_operator(context["shipment_id"], client)
        shipment_id = context["shipment_id"]
        first_container = context["containers"][0]
        url = (
            f"/api/v1/shipments/{shipment_id}/containers/{first_container['id']}/handoffs/delivery"
        )
        courier_headers = {
            **context["courier_auth"],
            "Idempotency-Key": f"delivery-init-{context['suffix']}",
        }
        initiated = await client.post(
            f"{url}/confirm-delivery",
            headers=courier_headers,
            json={"container_code": first_container["code"]},
        )
        assert initiated.status_code == 200, initiated.text
        replayed = await client.post(
            f"{url}/confirm-delivery",
            headers=courier_headers,
            json={"container_code": first_container["code"]},
        )
        assert replayed.status_code == 200
        assert replayed.json() == initiated.json()

        receipt = await client.post(
            f"{url}/confirm-receipt",
            headers={
                **lab_auth,
                "Idempotency-Key": f"delivery-receipt-{context['suffix']}",
            },
            json={"container_code": first_container["code"]},
        )
        assert receipt.status_code == 200, receipt.text
        body = receipt.json()
        assert body["shipment_status"] == "PARTIALLY_DELIVERED"
        assert body["container_status"] == "RECEIVED"
        assert body["handoff"]["status"] == "CONFIRMED"
        assert body["handoff"]["initiated_by"] == str(context["courier_id"])
        assert body["handoff"]["confirmed_by"] != str(context["courier_id"])

        history = await client.get(url, headers=context["courier_auth"])
        assert history.status_code == 200
        assert history.json()["container_id"] == first_container["id"]
        assert [item["status"] for item in history.json()["items"]] == ["CONFIRMED"]

        for index, next_container in enumerate(context["containers"][1:], start=1):
            next_url = (
                f"/api/v1/shipments/{shipment_id}/containers/{next_container['id']}"
                "/handoffs/delivery"
            )
            next_initiated = await client.post(
                f"{next_url}/confirm-delivery",
                headers={
                    **context["courier_auth"],
                    "Idempotency-Key": f"delivery-init-{context['suffix']}-{index}",
                },
                json={"container_code": next_container["code"]},
            )
            assert next_initiated.status_code == 200, next_initiated.text
            next_receipt = await client.post(
                f"{next_url}/confirm-receipt",
                headers={
                    **lab_auth,
                    "Idempotency-Key": f"delivery-receipt-{context['suffix']}-{index}",
                },
                json={"container_code": next_container["code"]},
            )
            assert next_receipt.status_code == 200, next_receipt.text
        assert next_receipt.json()["shipment_status"] == "RECEIVING"

        manifest = await client.get(f"/api/v1/shipments/{shipment_id}/manifest", headers=lab_auth)
        assert manifest.status_code == 200
        assert manifest.json()["containers"][0]["receipt_evidence_method"] == "NORMAL"

    async with get_session_factory()() as session:
        container = await session.get(TransportContainer, first_container["id"])
        assert container is not None
        assert container.received_at is not None
        assert container.received_by == UUID(body["handoff"]["confirmed_by"])
        items = list(
            await session.scalars(
                select(ContainerItem).where(ContainerItem.container_id == container.id)
            )
        )
        assert items and all(item.receipt_result is None for item in items)
        sessions = await session.scalar(
            text("SELECT count(*) FROM receiving_sessions WHERE status = 'OPEN'")
        )
        assert sessions == 1
        receipt_events = await session.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.resource_id == body["handoff"]["id"])
        )
        assert receipt_events == 2
        evidence = await session.execute(
            text(
                "SELECT method, confirmed_delivery_handoff_id, verified_by "
                "FROM container_receipt_evidence WHERE container_id=:container_id"
            ),
            {"container_id": first_container["id"]},
        )
        evidence_row = evidence.one()
        assert evidence_row.method == "NORMAL"
        assert str(evidence_row.confirmed_delivery_handoff_id) == body["handoff"]["id"]
        assert str(evidence_row.verified_by) == body["handoff"]["confirmed_by"]
        with pytest.raises(DBAPIError):
            await session.execute(
                text(
                    "UPDATE container_receipt_evidence SET verified_by=:actor_id "
                    "WHERE container_id=:container_id"
                ),
                {"actor_id": context["courier_id"], "container_id": first_container["id"]},
            )
        await session.rollback()
        with pytest.raises(DBAPIError):
            await session.execute(
                text(
                    "UPDATE handoffs SET status='SUPERSEDED', superseded_at=now() "
                    "WHERE id=:handoff_id"
                ),
                {"handoff_id": body["handoff"]["id"]},
            )


@pytest.mark.asyncio
async def test_delivery_withdrawal_reason_and_retry_attempt_number() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context = await create_pickup_fixture(client)
        await confirm_pickup(context, client)
        container = context["containers"][0]
        url = (
            f"/api/v1/shipments/{context['shipment_id']}/containers/{container['id']}"
            "/handoffs/delivery"
        )
        first = await client.post(
            f"{url}/confirm-delivery",
            headers={
                **context["courier_auth"],
                "Idempotency-Key": f"delivery-first-{context['suffix']}",
            },
            json={"container_code": container["code"]},
        )
        assert first.status_code == 200, first.text
        withdrawn = await client.post(
            f"{url}/withdraw",
            headers={
                **context["courier_auth"],
                "Idempotency-Key": f"delivery-withdraw-{context['suffix']}",
            },
            json={"reason": "Контейнер не был предъявлен лаборатории"},
        )
        assert withdrawn.status_code == 200, withdrawn.text
        assert withdrawn.json()["container_status"] == "IN_TRANSIT"
        assert withdrawn.json()["handoff"]["withdrawal_reason"] == (
            "Контейнер не был предъявлен лаборатории"
        )
        second = await client.post(
            f"{url}/confirm-delivery",
            headers={
                **context["courier_auth"],
                "Idempotency-Key": f"delivery-second-{context['suffix']}",
            },
            json={"container_code": container["code"]},
        )
        assert second.status_code == 200, second.text
        assert second.json()["handoff"]["attempt_no"] == 2

    async with get_session_factory()() as session:
        events = list(
            await session.scalars(
                select(AuditEvent).where(AuditEvent.resource_id == first.json()["handoff"]["id"])
            )
        )
        assert any(
            event.details == {"reason": "Контейнер не был предъявлен лаборатории"}
            for event in events
        )


@pytest.mark.asyncio
async def test_delivery_rejects_wrong_code_and_courier_self_receipt() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context = await create_pickup_fixture(client)
        await confirm_pickup(context, client)
        container = context["containers"][0]
        url = (
            f"/api/v1/shipments/{context['shipment_id']}/containers/{container['id']}"
            "/handoffs/delivery"
        )
        wrong_code = await client.post(
            f"{url}/confirm-delivery",
            headers={
                **context["courier_auth"],
                "Idempotency-Key": f"delivery-wrong-{context['suffix']}",
            },
            json={"container_code": "LT-C-WRONG"},
        )
        assert wrong_code.status_code == 409
        initiated = await client.post(
            f"{url}/confirm-delivery",
            headers={
                **context["courier_auth"],
                "Idempotency-Key": f"delivery-self-{context['suffix']}",
            },
            json={"container_code": container["code"]},
        )
        assert initiated.status_code == 200, initiated.text
        async with get_session_factory()() as session:
            shipment = await session.get(Shipment, context["shipment_id"])
            assert shipment is not None
            session.add(
                Membership(
                    user_id=context["courier_id"],
                    organization_id=shipment.destination_organization_id,
                    role="LAB_OPERATOR",
                )
            )
            await session.commit()
        self_receipt = await client.post(
            f"{url}/confirm-receipt",
            headers={
                **context["courier_auth"],
                "Idempotency-Key": f"delivery-self-confirm-{context['suffix']}",
            },
            json={"container_code": container["code"]},
        )
        assert self_receipt.status_code == 409


@pytest.mark.asyncio
async def test_receiving_session_schema_has_one_open_session_per_shipment() -> None:
    async with get_session_factory()() as session:
        table_name = await session.scalar(
            text("SELECT to_regclass('public.receiving_sessions')::text")
        )
        assert table_name == "receiving_sessions"
        index = await session.scalar(
            text(
                "SELECT indexdef FROM pg_indexes WHERE schemaname='public' "
                "AND indexname='uq_receiving_session_open'"
            )
        )
        assert index is not None and "status" in index and "OPEN" in index


@pytest.mark.asyncio
async def test_database_rejects_direct_receipt_without_a_confirmed_delivery() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context = await create_pickup_fixture(client)
        await confirm_pickup(context, client)

    async with get_session_factory()() as session:
        with pytest.raises(
            DBAPIError, match="Container receipt requires verified normal or exceptional evidence"
        ):
            await session.execute(
                text(
                    "UPDATE transport_containers SET delivery_status='RECEIVED', "
                    "received_at=now(), received_by=:courier, state_version=state_version+1 "
                    "WHERE id=:container"
                ),
                {"courier": context["courier_id"], "container": context["container_id"]},
            )
