"""Scoped organization courier discovery and destination Shipment listings."""

import json
from pathlib import Path
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.core.security import hash_password
from app.db.session import get_session_factory
from app.main import app
from app.modules.identity.models import Membership, Organization, Region, User

PASSWORD = "Resource Discovery Test Password 2026!"
PASSWORD_HASH = hash_password(PASSWORD)


async def _login(client: AsyncClient, email: str) -> dict[str, str]:
    response = await client.post("/api/v1/auth/token", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _user(email: str, display_name: str, *, active: bool = True) -> User:
    return User(
        email=email,
        display_name=display_name,
        password_hash=PASSWORD_HASH,
        is_active=active,
    )


@pytest.mark.asyncio
async def test_sender_can_list_only_active_couriers_in_organization_active_region() -> None:
    suffix = uuid4().hex[:10]
    async with get_session_factory()() as session:
        region = Region(code=f"CD-{suffix}", name="Courier discovery region", is_active=True)
        foreign_region = Region(code=f"XCD-{suffix}", name="Foreign courier region", is_active=True)
        organization = Organization(
            region=region,
            code=f"CS-{suffix}",
            name="Courier discovery sender",
            organization_type="HEALTHCARE_FACILITY",
            is_active=True,
        )
        sender = _user(f"sender-{suffix}@example.test", "Discovery Sender")
        alpha_a = _user(f"alpha-a-{suffix}@example.test", "Alpha Courier")
        alpha_b = _user(f"alpha-b-{suffix}@example.test", "Alpha Courier")
        zed = _user(f"zed-{suffix}@example.test", "Zed Courier")
        inactive_user = _user(
            f"inactive-user-{suffix}@example.test", "Inactive User Courier", active=False
        )
        inactive_membership = _user(
            f"inactive-grant-{suffix}@example.test", "Inactive Grant Courier"
        )
        foreign_courier = _user(f"foreign-{suffix}@example.test", "Foreign Courier")
        session.add_all(
            [
                region,
                foreign_region,
                organization,
                sender,
                alpha_a,
                alpha_b,
                zed,
                inactive_user,
                inactive_membership,
                foreign_courier,
            ]
        )
        await session.flush()
        session.add_all(
            [
                Membership(
                    user_id=sender.id, organization_id=organization.id, role="SENDER_OPERATOR"
                ),
                Membership(user_id=alpha_a.id, region_id=region.id, role="COURIER"),
                Membership(user_id=alpha_b.id, region_id=region.id, role="COURIER"),
                Membership(user_id=zed.id, region_id=region.id, role="COURIER"),
                Membership(user_id=inactive_user.id, region_id=region.id, role="COURIER"),
                Membership(
                    user_id=inactive_membership.id,
                    region_id=region.id,
                    role="COURIER",
                    is_active=False,
                ),
                Membership(user_id=foreign_courier.id, region_id=foreign_region.id, role="COURIER"),
            ]
        )
        sender_email = sender.email
        organization_id = organization.id
        alpha_ids = sorted((alpha_a.id, alpha_b.id), key=str)
        zed_id = zed.id
        await session.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        auth = await _login(client, sender_email)
        first_page = await client.get(
            f"/api/v1/organizations/{organization_id}/couriers?limit=2&offset=0",
            headers=auth,
        )
        second_page = await client.get(
            f"/api/v1/organizations/{organization_id}/couriers?limit=2&offset=2",
            headers=auth,
        )

    assert first_page.status_code == second_page.status_code == 200
    assert first_page.json() == {
        "items": [
            {"id": str(courier_id), "display_name": "Alpha Courier"} for courier_id in alpha_ids
        ],
        "total": 3,
        "limit": 2,
        "offset": 0,
    }
    assert second_page.json() == {
        "items": [{"id": str(zed_id), "display_name": "Zed Courier"}],
        "total": 3,
        "limit": 2,
        "offset": 2,
    }
    assert all("email" not in item for item in first_page.json()["items"])


@pytest.mark.asyncio
async def test_sender_courier_discovery_masks_other_organization_with_404() -> None:
    suffix = uuid4().hex[:10]
    async with get_session_factory()() as session:
        region = await session.scalar(select(Region).where(Region.code == "PILOT"))
        assert region is not None
        organizations = [
            Organization(
                region_id=region.id,
                code=f"SCOPE-{suffix}-{index}",
                name=f"Courier scope sender {index}",
                organization_type="HEALTHCARE_FACILITY",
                is_active=True,
            )
            for index in range(2)
        ]
        sender = _user(f"scope-sender-{suffix}@example.test", "Scoped Sender")
        session.add_all([*organizations, sender])
        await session.flush()
        session.add(
            Membership(
                user_id=sender.id,
                organization_id=organizations[0].id,
                role="SENDER_OPERATOR",
            )
        )
        sender_email = sender.email
        wrong_organization_id = organizations[1].id
        await session.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        auth = await _login(client, sender_email)
        response = await client.get(
            f"/api/v1/organizations/{wrong_organization_id}/couriers", headers=auth
        )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "RESOURCE_NOT_FOUND"
    assert response.json()["error"]["message"] == "Resource not found"


@pytest.mark.asyncio
async def test_lab_operator_lists_only_shipments_for_its_destination_organization() -> None:
    suffix = uuid4().hex[:10]
    async with get_session_factory()() as session:
        region = await session.scalar(select(Region).where(Region.code == "PILOT"))
        destination = await session.scalar(
            select(Organization).where(Organization.code == "CENTRAL_LAB")
        )
        assert region is not None and destination is not None
        sender_org = Organization(
            region_id=region.id,
            code=f"LS-{suffix}",
            name="Shipment discovery sender",
            organization_type="HEALTHCARE_FACILITY",
            is_active=True,
        )
        unrelated_sender_org = Organization(
            region_id=region.id,
            code=f"US-{suffix}",
            name="Unrelated shipment sender",
            organization_type="HEALTHCARE_FACILITY",
            is_active=True,
        )
        unrelated_lab = Organization(
            region_id=region.id,
            code=f"UL-{suffix}",
            name="Unrelated destination Lab",
            organization_type="LABORATORY",
            is_active=True,
        )
        sender = _user(f"shipment-sender-{suffix}@example.test", "Shipment Sender")
        unrelated_sender = _user(f"unrelated-sender-{suffix}@example.test", "Unrelated Sender")
        lab_operator = _user(f"destination-lab-{suffix}@example.test", "Destination Lab")
        lab_manager = _user(f"destination-manager-{suffix}@example.test", "Destination Manager")
        unrelated_lab_operator = _user(f"unrelated-lab-{suffix}@example.test", "Unrelated Lab")
        courier = _user(f"assigned-courier-{suffix}@example.test", "Assigned Courier")
        session.add_all(
            [
                sender_org,
                unrelated_sender_org,
                unrelated_lab,
                sender,
                unrelated_sender,
                lab_operator,
                lab_manager,
                unrelated_lab_operator,
                courier,
            ]
        )
        await session.flush()
        session.add_all(
            [
                Membership(
                    user_id=sender.id, organization_id=sender_org.id, role="SENDER_OPERATOR"
                ),
                Membership(
                    user_id=unrelated_sender.id,
                    organization_id=unrelated_sender_org.id,
                    role="SENDER_OPERATOR",
                ),
                Membership(
                    user_id=lab_operator.id,
                    organization_id=destination.id,
                    role="LAB_OPERATOR",
                ),
                Membership(
                    user_id=lab_manager.id,
                    organization_id=destination.id,
                    role="LAB_MANAGER",
                ),
                Membership(
                    user_id=unrelated_lab_operator.id,
                    organization_id=unrelated_lab.id,
                    role="LAB_OPERATOR",
                ),
                Membership(user_id=courier.id, region_id=region.id, role="COURIER"),
            ]
        )
        await session.commit()
        sender_email = sender.email
        unrelated_sender_email = unrelated_sender.email
        lab_operator_email = lab_operator.email
        lab_manager_email = lab_manager.email
        unrelated_lab_email = unrelated_lab_operator.email
        courier_email = courier.email
        sender_org_id = sender_org.id
        destination_id = destination.id
        courier_id = courier.id

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        sender_auth = await _login(client, sender_email)
        unrelated_sender_auth = await _login(client, unrelated_sender_email)
        lab_auth = await _login(client, lab_operator_email)
        manager_auth = await _login(client, lab_manager_email)
        unrelated_lab_auth = await _login(client, unrelated_lab_email)
        courier_auth = await _login(client, courier_email)
        created = await client.post(
            "/api/v1/shipments",
            headers={**sender_auth, "Idempotency-Key": f"resource-discovery-{suffix}"},
            json={"sender_org_id": str(sender_org_id), "destination_org_id": str(destination_id)},
        )
        assert created.status_code == 201, created.text
        shipment_id = created.json()["id"]
        assigned = await client.patch(
            f"/api/v1/shipments/{shipment_id}/courier",
            headers={**sender_auth, "Idempotency-Key": f"resource-assign-{suffix}"},
            json={"courier_user_id": str(courier_id)},
        )
        assert assigned.status_code == 200, assigned.text
        sender_shipments = await client.get("/api/v1/shipments", headers=sender_auth)
        destination_shipments = await client.get("/api/v1/shipments", headers=lab_auth)
        manager_shipments = await client.get("/api/v1/shipments", headers=manager_auth)
        courier_shipments = await client.get("/api/v1/shipments", headers=courier_auth)
        unrelated_lab_shipments = await client.get("/api/v1/shipments", headers=unrelated_lab_auth)
        unrelated_sender_shipments = await client.get(
            "/api/v1/shipments", headers=unrelated_sender_auth
        )

    for response in (
        sender_shipments,
        destination_shipments,
        manager_shipments,
        courier_shipments,
        unrelated_lab_shipments,
        unrelated_sender_shipments,
    ):
        assert response.status_code == 200
    for response in (sender_shipments, destination_shipments, manager_shipments, courier_shipments):
        assert shipment_id in {item["id"] for item in response.json()["items"]}
    assert shipment_id not in {item["id"] for item in unrelated_lab_shipments.json()["items"]}
    assert shipment_id not in {item["id"] for item in unrelated_sender_shipments.json()["items"]}


def test_resource_discovery_openapi_conforms_to_historical_design_excerpt() -> None:
    contract_path = Path(__file__).resolve().parents[3] / "openapi" / "labtrack-v1.json"
    contract = json.loads(contract_path.read_text())
    runtime = app.openapi()
    operations = (
        ("/organizations/{organization_id}/couriers", "listOrganizationCouriers"),
        ("/shipments", "listShipments"),
    )

    for path, operation_id in operations:
        expected = contract["paths"][path]["get"]
        actual = runtime["paths"][f"/api/v1{path}"]["get"]
        assert actual["operationId"] == expected["operationId"] == operation_id
        assert actual["summary"] == expected["summary"]
        assert actual["description"] == expected.get("description")
        assert set(actual["responses"]) == set(expected["responses"])
        assert actual["responses"]["200"].get("headers", {}) == expected["responses"]["200"].get(
            "headers", {}
        )

        expected_parameters = {
            (parameter["name"], parameter["in"]): parameter
            for parameter in expected.get("parameters", [])
        }
        actual_parameters = {
            (parameter["name"], parameter["in"]): parameter
            for parameter in actual.get("parameters", [])
        }
        assert actual_parameters.keys() == expected_parameters.keys()
        for key, expected_parameter in expected_parameters.items():
            actual_parameter = actual_parameters[key]
            assert actual_parameter.get("required", False) == expected_parameter.get(
                "required", False
            )
            for field in ("type", "format", "minimum", "maximum", "default"):
                if field in expected_parameter["schema"]:
                    assert (
                        actual_parameter["schema"].get(field) == expected_parameter["schema"][field]
                    )

        expected_response = expected["responses"]["200"]["content"]["application/json"]["schema"]
        actual_response = actual["responses"]["200"]["content"]["application/json"]["schema"]

        def resolve_schema(schema: dict, components: dict) -> dict:
            while "$ref" in schema:
                schema = components[schema["$ref"].rsplit("/", 1)[1]]
            return schema

        expected_schema = resolve_schema(expected_response, contract["components"]["schemas"])
        actual_schema = resolve_schema(actual_response, runtime["components"]["schemas"])
        assert set(actual_schema["properties"]) == set(expected_schema["properties"])
        assert set(actual_schema["required"]) == set(expected_schema["required"])
        assert actual_schema.get("additionalProperties") is False

        expected_item = resolve_schema(
            expected_schema["properties"]["items"]["items"], contract["components"]["schemas"]
        )
        actual_item = resolve_schema(
            actual_schema["properties"]["items"]["items"], runtime["components"]["schemas"]
        )
        assert set(actual_item["properties"]) == set(expected_item["properties"])
        assert set(actual_item["required"]) == set(expected_item["required"])
        assert actual_item.get("additionalProperties") is False

    courier_response = contract["components"]["schemas"]["PagedOrganizationCourierResponse"]
    courier_item_ref = courier_response["properties"]["items"]["items"]["$ref"]
    courier_item = contract["components"]["schemas"][courier_item_ref.rsplit("/", 1)[1]]
    assert set(courier_item["properties"]) == {"id", "display_name"}
    assert "email" not in courier_item["properties"]
