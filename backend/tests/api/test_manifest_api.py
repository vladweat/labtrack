from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app.core.security import hash_password
from app.db.session import get_session_factory
from app.main import app
from app.modules.identity.models import Membership, Organization, Region, User
from app.modules.shipments.models import Shipment


@pytest.mark.asyncio
async def test_issue_reasons_are_a_fixed_catalog() -> None:
    async with get_session_factory()() as session:
        user = User(
            email="reason-reader@example.test",
            display_name="Reason Reader",
            password_hash=hash_password("Manifest Test Password 2026!"),
        )
        session.add(user)
        await session.commit()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        login = await client.post(
            "/api/v1/auth/token",
            json={
                "email": "reason-reader@example.test",
                "password": "Manifest Test Password 2026!",
            },
        )
        response = await client.get(
            "/api/v1/issue-reasons",
            headers={"Authorization": f"Bearer {login.json()['access_token']}"},
        )

    assert response.status_code == 200
    reasons = {
        item["code"]: (item["scope"], item["kind"], item["label"])
        for item in response.json()["items"]
    }
    assert reasons == {
        "SAMPLE_NOT_FOUND": ("SAMPLE", "MISSING", "Пробирка не обнаружена"),
        "SAMPLE_LABEL_MISMATCH": (
            "SAMPLE",
            "REJECTED",
            "Маркировка пробирки не соответствует",
        ),
        "SAMPLE_CONTAINER_COMPROMISED": (
            "SAMPLE",
            "REJECTED",
            "Нарушена целостность контейнера с пробиркой",
        ),
        "CONTAINER_NOT_PRESENT": ("CONTAINER", "MISSING", "Контейнер не предъявлен"),
        "CONTAINER_SEAL_BROKEN": ("CONTAINER", "REJECTED", "Пломба контейнера нарушена"),
        "CONTAINER_LABEL_MISMATCH": (
            "CONTAINER",
            "REJECTED",
            "Маркировка контейнера не соответствует",
        ),
    }
    async with get_session_factory()() as session:
        with pytest.raises(DBAPIError, match="Issue reason catalog is read-only"):
            await session.execute(
                text("UPDATE issue_reason_codes SET label='changed' WHERE code='SAMPLE_NOT_FOUND'")
            )
        await session.rollback()


@pytest.mark.asyncio
async def test_sample_write_requires_idempotency_key() -> None:
    async with get_session_factory()() as session:
        region = Region(code="MANIFEST-REGION", name="Manifest region")
        organization = Organization(
            region=region,
            code="MANIFEST-SENDER",
            name="Manifest sender",
            organization_type="HEALTHCARE_FACILITY",
        )
        user = User(
            email="manifest-sender@example.test",
            display_name="Manifest Sender",
            password_hash=hash_password("Manifest Test Password 2026!"),
        )
        session.add_all([region, organization, user])
        await session.flush()
        session.add(
            Membership(user_id=user.id, organization_id=organization.id, role="SENDER_OPERATOR")
        )
        await session.commit()
        organization_id = organization.id

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token_response = await client.post(
            "/api/v1/auth/token",
            json={
                "email": "manifest-sender@example.test",
                "password": "Manifest Test Password 2026!",
            },
        )
        assert token_response.status_code == 200
        response = await client.post(
            "/api/v1/samples",
            headers={"Authorization": f"Bearer {token_response.json()['access_token']}"},
            json={
                "origin_organization_id": str(organization_id),
                "material_type": "BLOOD",
                "collected_at": None,
            },
        )

    assert response.status_code == 400
    assert response.json()["error"]["message"] == "Idempotency-Key header is required"


@pytest.mark.asyncio
async def test_manifest_flow_replays_writes_and_seals_against_direct_sql_mutation() -> None:
    async with get_session_factory()() as session:
        region = await session.scalar(select(Region).where(Region.code == "PILOT"))
        assert region is not None
        sender = Organization(
            region_id=region.id,
            code="MANIFEST-SENDER-FLOW",
            name="Manifest sender",
            organization_type="HEALTHCARE_FACILITY",
        )
        user = User(
            email="manifest-flow@example.test",
            display_name="Manifest Sender",
            password_hash=hash_password("Manifest Test Password 2026!"),
        )
        courier = User(
            email="manifest-courier@example.test",
            display_name="Manifest Courier",
            password_hash=hash_password("Manifest Test Password 2026!"),
        )
        session.add_all([sender, user, courier])
        await session.flush()
        session.add_all(
            [
                Membership(
                    user_id=user.id,
                    organization_id=sender.id,
                    role="SENDER_OPERATOR",
                ),
                Membership(user_id=courier.id, region_id=region.id, role="COURIER"),
            ]
        )
        destination = await session.scalar(
            select(Organization).where(Organization.code == "CENTRAL_LAB")
        )
        await session.commit()
        sender_id = sender.id
        destination_id = destination.id
        courier_id = courier.id

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        login_response = await client.post(
            "/api/v1/auth/token",
            json={
                "email": "manifest-flow@example.test",
                "password": "Manifest Test Password 2026!",
            },
        )
        headers = {
            "Authorization": f"Bearer {login_response.json()['access_token']}",
            "Idempotency-Key": "sample-create-1",
        }
        sample_payload = {
            "origin_organization_id": str(sender_id),
            "material_type": "BLOOD",
            "collected_at": None,
        }
        created_sample = await client.post("/api/v1/samples", headers=headers, json=sample_payload)
        replayed_sample = await client.post("/api/v1/samples", headers=headers, json=sample_payload)
        assert created_sample.status_code == replayed_sample.status_code == 201
        assert created_sample.json() == replayed_sample.json()
        sample_id = created_sample.json()["id"]
        sample_detail = await client.get(
            f"/api/v1/samples/{sample_id}",
            headers={"Authorization": headers["Authorization"]},
        )
        sample_page = await client.get(
            "/api/v1/samples", headers={"Authorization": headers["Authorization"]}
        )
        assert sample_detail.status_code == 200
        assert sample_page.status_code == 200 and sample_page.json()["total"] == 1

        headers["Idempotency-Key"] = "external-reference-create-1"
        reference_payload = {
            "system_code": "SYNTHETIC_LIS",
            "reference_kind": "ACCESSION",
            "external_id": "TEST-ACCESSION-0001",
        }
        reference_created = await client.post(
            f"/api/v1/samples/{sample_id}/external-references",
            headers=headers,
            json=reference_payload,
        )
        reference_replay = await client.post(
            f"/api/v1/samples/{sample_id}/external-references",
            headers=headers,
            json=reference_payload,
        )
        references = await client.get(
            f"/api/v1/samples/{sample_id}/external-references",
            headers={"Authorization": headers["Authorization"]},
        )
        assert reference_created.status_code == reference_replay.status_code == 201
        assert reference_created.json() == reference_replay.json()
        assert references.status_code == 200 and references.json()["total"] == 1

        headers["Idempotency-Key"] = "sample-create-1"
        async with get_session_factory()() as session:
            with pytest.raises(DBAPIError, match="Audit events are append-only"):
                await session.execute(
                    text(
                        "UPDATE audit_events SET operation='tampered' "
                        "WHERE request_key='sample-create-1'"
                    )
                )
            await session.rollback()
        changed_payload = {**sample_payload, "material_type": "URINE"}
        changed_replay = await client.post("/api/v1/samples", headers=headers, json=changed_payload)
        assert changed_replay.status_code == 409
        async with get_session_factory()() as session:
            audit_count = await session.scalar(
                text("SELECT count(*) FROM audit_events WHERE request_key='sample-create-1'")
            )
        assert audit_count == 1

        headers["Idempotency-Key"] = "sample-create-2"
        second_sample = await client.post(
            "/api/v1/samples",
            headers=headers,
            json=sample_payload,
        )
        assert second_sample.status_code == 201

        headers["Idempotency-Key"] = "shipment-create-1"
        created_shipment = await client.post(
            "/api/v1/shipments",
            headers=headers,
            json={
                "sender_org_id": str(sender_id),
                "destination_org_id": str(destination_id),
                "expected_delivery_at": None,
            },
        )
        assert created_shipment.status_code == 201, created_shipment.text
        assert created_shipment.json()["expected_delivery_at"] is None
        shipment_id = created_shipment.json()["id"]

        headers["Idempotency-Key"] = "shipment-deadline-update-1"
        deadline_response = await client.patch(
            f"/api/v1/shipments/{shipment_id}",
            headers=headers,
            json={"expected_delivery_at": (datetime.now(UTC) + timedelta(hours=2)).isoformat()},
        )
        assert deadline_response.status_code == 200

        headers["Idempotency-Key"] = "courier-invalid-1"
        invalid_courier_response = await client.patch(
            f"/api/v1/shipments/{shipment_id}/courier",
            headers=headers,
            json={"courier_user_id": str(uuid4())},
        )
        assert invalid_courier_response.status_code == 422
        headers["Idempotency-Key"] = "courier-assign-1"
        courier_response = await client.patch(
            f"/api/v1/shipments/{shipment_id}/courier",
            headers=headers,
            json={"courier_user_id": str(courier_id)},
        )
        assert courier_response.status_code == 200
        assert courier_response.json()["courier_user_id"] == str(courier_id)

        headers["Idempotency-Key"] = "container-create-1"
        container_response = await client.post(
            f"/api/v1/shipments/{shipment_id}/containers", headers=headers
        )
        assert container_response.status_code == 201, container_response.text
        container_id = container_response.json()["id"]

        headers["Idempotency-Key"] = "container-create-2"
        second_container_response = await client.post(
            f"/api/v1/shipments/{shipment_id}/containers", headers=headers
        )
        assert second_container_response.status_code == 201
        second_container_id = second_container_response.json()["id"]

        headers["Idempotency-Key"] = "container-create-empty"
        empty_container_response = await client.post(
            f"/api/v1/shipments/{shipment_id}/containers", headers=headers
        )
        assert empty_container_response.status_code == 201
        empty_container_id = empty_container_response.json()["id"]
        headers["Idempotency-Key"] = "container-delete-empty"
        deleted_empty = await client.delete(
            f"/api/v1/shipments/{shipment_id}/containers/{empty_container_id}", headers=headers
        )
        assert deleted_empty.status_code == 204

        headers["Idempotency-Key"] = "container-item-create-1"
        item_response = await client.post(
            f"/api/v1/shipments/{shipment_id}/containers/{container_id}/items",
            headers=headers,
            json={"sample_id": created_sample.json()["id"]},
        )
        assert item_response.status_code == 201, item_response.text
        async with get_session_factory()() as session:
            with pytest.raises(DBAPIError, match="duplicate key value violates unique constraint"):
                await session.execute(
                    text(
                        "INSERT INTO container_items (id, container_id, sample_id) "
                        "VALUES (:id, :container_id, :sample_id)"
                    ),
                    {
                        "id": uuid4(),
                        "container_id": second_container_id,
                        "sample_id": UUID(created_sample.json()["id"]),
                    },
                )
            await session.rollback()
        headers["Idempotency-Key"] = "container-item-create-2"
        second_item_response = await client.post(
            f"/api/v1/shipments/{shipment_id}/containers/{second_container_id}/items",
            headers=headers,
            json={"sample_id": second_sample.json()["id"]},
        )
        assert second_item_response.status_code == 201
        headers["Idempotency-Key"] = "container-item-duplicate"
        duplicate_item_response = await client.post(
            f"/api/v1/shipments/{shipment_id}/containers/{second_container_id}/items",
            headers=headers,
            json={"sample_id": created_sample.json()["id"]},
        )
        assert duplicate_item_response.status_code == 409
        manifest_response = await client.get(
            f"/api/v1/shipments/{shipment_id}/manifest",
            headers={"Authorization": headers["Authorization"]},
        )
        assert len(manifest_response.json()["containers"]) == 2
        assert all(
            len(container["items"]) == 1 for container in manifest_response.json()["containers"]
        )

        headers["Idempotency-Key"] = "shipment-seal-1"
        sealed_response = await client.post(
            f"/api/v1/shipments/{shipment_id}/seal", headers=headers
        )
        assert sealed_response.status_code == 200, sealed_response.text
        assert sealed_response.json()["status"] == "READY_FOR_PICKUP"

    async with get_session_factory()() as session:
        shipment = await session.get(Shipment, shipment_id)
        assert shipment is not None
        with pytest.raises(DBAPIError, match="Sealed shipment identity is immutable"):
            await session.execute(
                text("UPDATE shipments SET destination_organization_id=:other WHERE id=:id"),
                {"other": sender_id, "id": shipment.id},
            )
        await session.rollback()
        with pytest.raises(DBAPIError, match="Manifest is immutable after shipment seal"):
            await session.execute(
                text("DELETE FROM container_items WHERE container_id=:container_id"),
                {"container_id": container_id},
            )
        await session.rollback()
        with pytest.raises(DBAPIError, match="must be owner of table transport_containers"):
            await session.execute(text("ALTER TABLE transport_containers DISABLE TRIGGER ALL"))
        await session.rollback()
        with pytest.raises(DBAPIError, match="Shipment must be created in DRAFT"):
            await session.execute(
                text(
                    "INSERT INTO shipments (id, code, region_id, sender_organization_id, "
                    "destination_organization_id, status, version, manifest_revision, "
                    "expected_delivery_at, sealed_at, created_by_user_id, created_at, updated_at) "
                    "SELECT :new_id, 'SHP-DIRECT-BYPASS', region_id, sender_organization_id, "
                    "destination_organization_id, 'READY_FOR_PICKUP', 1, 1, expected_delivery_at, "
                    "now(), created_by_user_id, now(), now() FROM shipments WHERE id=:source_id"
                ),
                {"new_id": uuid4(), "source_id": UUID(shipment_id)},
            )
        await session.rollback()
