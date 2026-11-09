"""Custody incident and first-party attestation acceptance tests."""

import asyncio
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from test_delivery_api import confirm_pickup
from test_pickup_api import PASSWORD, create_pickup_fixture

from app.core.config import get_settings
from app.core.security import hash_password
from app.db.session import get_engine, get_session_factory
from app.main import app
from app.modules.audit.models import AuditEvent
from app.modules.idempotency.models import IdempotencyRecord
from app.modules.identity.models import Membership, User
from app.modules.shipments.models import CustodyIncident, Shipment


def openapi_schema_shape(
    schema: dict, components: dict, *, include_constraints: bool = True
) -> dict:
    if "$ref" in schema:
        name = schema["$ref"].rsplit("/", 1)[1]
        return openapi_schema_shape(
            components[name], components, include_constraints=include_constraints
        )
    if "oneOf" in schema or "anyOf" in schema:
        key = "oneOf" if "oneOf" in schema else "anyOf"
        branches = [
            openapi_schema_shape(branch, components, include_constraints=include_constraints)
            for branch in schema[key]
        ]
        return {
            "union": sorted(branches, key=lambda branch: json.dumps(branch, sort_keys=True)),
            # A discriminator is a runtime dispatch hint. A static oneOf can express
            # the same contract with a const property on each branch.
            "discriminator": None,
        }
    schema_type = schema.get("type")
    if isinstance(schema_type, list) and "null" in schema_type:
        nonnull_schema = dict(schema)
        nonnull_types = [value for value in schema_type if value != "null"]
        nonnull_schema["type"] = nonnull_types[0] if len(nonnull_types) == 1 else nonnull_types
        if "enum" in nonnull_schema:
            nonnull_schema["enum"] = [
                value for value in nonnull_schema["enum"] if value is not None
            ]
        return {
            "union": sorted(
                [
                    openapi_schema_shape(
                        nonnull_schema, components, include_constraints=include_constraints
                    ),
                    {"type": "null"},
                ],
                key=lambda branch: json.dumps(branch, sort_keys=True),
            ),
            "discriminator": None,
        }

    result = {}
    if schema_type is not None:
        result["type"] = schema_type
    elif "const" in schema:
        const_type = type(schema["const"])
        result["type"] = {
            str: "string",
            int: "integer",
            float: "number",
            bool: "boolean",
        }[const_type]
    schema_keys = ["enum", "const", "format", "additionalProperties"]
    if include_constraints:
        schema_keys.extend(
            ("minimum", "maximum", "minLength", "maxLength", "minItems", "maxItems", "uniqueItems")
        )
    for key in schema_keys:
        if key in schema:
            result[key] = sorted(schema[key], key=repr) if key == "enum" else schema[key]
    if "required" in schema:
        result["required"] = sorted(schema["required"])
    if "properties" in schema:
        result["properties"] = {
            name: openapi_schema_shape(value, components, include_constraints=include_constraints)
            for name, value in schema["properties"].items()
        }
    if "items" in schema:
        result["items"] = openapi_schema_shape(
            schema["items"], components, include_constraints=include_constraints
        )
    return result


def test_stage_e_runtime_openapi_conforms_to_historical_design_excerpts() -> None:
    contract_path = Path(__file__).resolve().parents[3] / "openapi" / "labtrack-v1.json"
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    runtime = app.openapi()
    paged_paths = (
        "/shipments/{shipment_id}/container-issues",
        "/shipments/{shipment_id}/container-issues/{issue_id}/proposals",
        "/shipments/{shipment_id}/custody-incidents",
    )

    for path in paged_paths:
        expected = contract["paths"][path]["get"]
        actual = runtime["paths"][f"/api/v1{path}"]["get"]
        assert actual["operationId"] == expected["operationId"]
        expected_query = {
            parameter["name"]: parameter["schema"]
            for parameter in expected["parameters"]
            if parameter["in"] == "query"
        }
        actual_query = {
            parameter["name"]: parameter["schema"]
            for parameter in actual["parameters"]
            if parameter["in"] == "query"
        }
        assert set(expected_query) == set(actual_query) == {"limit", "offset"}
        for name in ("limit", "offset"):
            for constraint in ("default", "minimum"):
                assert actual_query[name][constraint] == expected_query[name][constraint]
        assert actual_query["limit"]["maximum"] == expected_query["limit"]["maximum"]

    static_schemas = contract["components"]["schemas"]
    assert static_schemas["CustodyAttestation"]["properties"]["container_code"]["type"] == [
        "string",
        "null",
    ]
    assert "container_code" in static_schemas["CustodyAttestation"]["required"]
    assert (
        "PARTIALLY_DELIVERED"
        in static_schemas["CreateContainerIssueResponse"]["properties"]["shipment_status"]["enum"]
    )
    runtime_schemas = runtime["components"]["schemas"]
    runtime_code_types = {
        variant["type"]
        for variant in runtime_schemas["CustodyAttestationResponse"]["properties"][
            "container_code"
        ]["anyOf"]
    }
    assert runtime_code_types == {"string", "null"}
    assert set(
        runtime_schemas["CreateContainerIssueResponse"]["properties"]["shipment_status"]["enum"]
    ) == set(
        static_schemas["CreateContainerIssueResponse"]["properties"]["shipment_status"]["enum"]
    )

    stage_e_operations = (
        ("POST", "/shipments/{shipment_id}/container-issues", "reportContainerIssue"),
        ("GET", "/shipments/{shipment_id}/container-issues", "listContainerIssues"),
        (
            "POST",
            "/shipments/{shipment_id}/container-issues/{issue_id}/proposals",
            "proposeContainerIssueResolution",
        ),
        (
            "GET",
            "/shipments/{shipment_id}/container-issues/{issue_id}/proposals",
            "listContainerIssueResolutionProposals",
        ),
        (
            "POST",
            "/shipments/{shipment_id}/container-issues/{issue_id}/proposals/{proposal_id}/decide",
            "decideContainerIssueResolution",
        ),
        (
            "POST",
            "/shipments/{shipment_id}/container-issues/{issue_id}/proposals/{proposal_id}/withdraw",
            "withdrawContainerIssueResolutionProposal",
        ),
        ("POST", "/shipments/{shipment_id}/custody-incidents", "createCustodyIncident"),
        ("GET", "/shipments/{shipment_id}/custody-incidents", "listCustodyIncidents"),
        (
            "POST",
            "/shipments/{shipment_id}/custody-incidents/{incident_id}/attest",
            "attestCustodyIncident",
        ),
        (
            "POST",
            "/shipments/{shipment_id}/custody-incidents/{incident_id}/resolve",
            "resolveCustodyIncident",
        ),
        (
            "POST",
            "/shipments/{shipment_id}/containers/{container_id}/exceptional-receipts",
            "startExceptionalContainerReceipt",
        ),
    )
    for method, path, operation_id in stage_e_operations:
        static_operation = contract["paths"][path][method.lower()]
        runtime_operation = runtime["paths"][f"/api/v1{path}"][method.lower()]
        assert runtime_operation["operationId"] == static_operation["operationId"] == operation_id
        assert set(runtime_operation["responses"]) == set(static_operation["responses"])
        if "requestBody" in static_operation:
            static_request = static_operation["requestBody"]["content"]["application/json"][
                "schema"
            ]
            runtime_request = runtime_operation["requestBody"]["content"]["application/json"][
                "schema"
            ]
            assert openapi_schema_shape(static_request, static_schemas) == openapi_schema_shape(
                runtime_request, runtime_schemas
            )
        success_status = next(
            status for status in static_operation["responses"] if status.startswith("2")
        )
        static_response = static_operation["responses"][success_status]["content"][
            "application/json"
        ]["schema"]
        runtime_response = runtime_operation["responses"][success_status]["content"][
            "application/json"
        ]["schema"]
        assert openapi_schema_shape(
            static_response, static_schemas, include_constraints=False
        ) == openapi_schema_shape(runtime_response, runtime_schemas, include_constraints=False)


async def authorize_region(
    region_id: str, client: AsyncClient, *, return_user_id: bool = False
) -> dict[str, str] | tuple[dict[str, str], str]:
    suffix = uuid4().hex[:10]
    async with get_session_factory()() as session:
        user = User(
            email=f"incident-region-{suffix}@example.test",
            display_name="Incident Regional Operator",
            password_hash=hash_password(PASSWORD),
        )
        session.add(user)
        await session.flush()
        session.add(Membership(user_id=user.id, region_id=region_id, role="REGIONAL_OPERATIONS"))
        user_id = str(user.id)
        await session.commit()
        email = user.email
    login = await client.post("/api/v1/auth/token", json={"email": email, "password": PASSWORD})
    assert login.status_code == 200, login.text
    auth = {"Authorization": f"Bearer {login.json()['access_token']}"}
    return (auth, user_id) if return_user_id else auth


async def authorize_lab(
    shipment_id: str,
    client: AsyncClient,
    *,
    manager: bool = False,
    also_operator: bool = False,
    region_id: str | None = None,
    return_user_id: bool = False,
) -> dict[str, str] | tuple[dict[str, str], str]:
    suffix = uuid4().hex[:10]
    async with get_session_factory()() as session:
        shipment = await session.get(Shipment, shipment_id)
        assert shipment is not None
        user = User(
            email=f"incident-lab-{suffix}@example.test",
            display_name="Incident Lab Witness",
            password_hash=hash_password(PASSWORD),
        )
        session.add(user)
        await session.flush()
        roles = ["LAB_MANAGER" if manager else "LAB_OPERATOR"]
        if manager and also_operator:
            roles.append("LAB_OPERATOR")
        session.add_all(
            [
                Membership(
                    user_id=user.id,
                    organization_id=shipment.destination_organization_id,
                    role=role,
                )
                for role in roles
            ]
            + (
                [Membership(user_id=user.id, region_id=region_id, role="REGIONAL_OPERATIONS")]
                if region_id is not None
                else []
            )
        )
        user_id = str(user.id)
        await session.commit()
        email = user.email
    login = await client.post("/api/v1/auth/token", json={"email": email, "password": PASSWORD})
    assert login.status_code == 200, login.text
    auth = {"Authorization": f"Bearer {login.json()['access_token']}"}
    return (auth, user_id) if return_user_id else auth


async def shipment_region(shipment_id: str) -> str:
    async with get_session_factory()() as session:
        value = await session.scalar(
            text("SELECT region_id FROM shipments WHERE id=:id"), {"id": shipment_id}
        )
        return str(value)


async def open_delivery_incident(context: dict, regional_auth: dict, client: AsyncClient):
    return await client.post(
        f"/api/v1/shipments/{context['shipment_id']}/custody-incidents",
        headers={**regional_auth, "Idempotency-Key": f"delivery-incident-{context['suffix']}"},
        json={
            "phase": "DELIVERY",
            "kind": "COURIER_DELIVERY_CONFIRMATION_UNAVAILABLE",
            "container_id": context["container_id"],
            "reason": "Courier cannot complete handoff confirmation",
        },
    )


@pytest.mark.asyncio
async def test_regional_operator_opens_and_lists_scoped_incident() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context = await create_pickup_fixture(client)
        await confirm_pickup(context, client)
        regional_auth = await authorize_region(
            await shipment_region(context["shipment_id"]), client
        )
        created = await open_delivery_incident(context, regional_auth, client)
        listed = await client.get(
            f"/api/v1/shipments/{context['shipment_id']}/custody-incidents",
            headers=regional_auth,
        )

    assert created.status_code == 201, created.text
    assert created.json()["status"] == "OPEN"
    assert created.json()["phase"] == "DELIVERY"
    assert created.json()["container_id"] == context["container_id"]
    assert listed.status_code == 200
    assert listed.json()["total"] == 1


@pytest.mark.asyncio
async def test_incident_create_key_cannot_replay_to_another_shipment() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        first_context = await create_pickup_fixture(client)
        second_context = await create_pickup_fixture(client)
        regional_auth = await authorize_region(
            await shipment_region(first_context["shipment_id"]), client
        )
        key = f"cross-shipment-incident-{uuid4().hex}"
        payload = {
            "phase": "PICKUP",
            "kind": "PICKUP_UNCONFIRMED",
            "container_id": None,
            "reason": "Courier did not confirm pickup",
        }
        first = await client.post(
            f"/api/v1/shipments/{first_context['shipment_id']}/custody-incidents",
            headers={**regional_auth, "Idempotency-Key": key},
            json=payload,
        )
        second = await client.post(
            f"/api/v1/shipments/{second_context['shipment_id']}/custody-incidents",
            headers={**regional_auth, "Idempotency-Key": key},
            json=payload,
        )
        second_incidents = await client.get(
            f"/api/v1/shipments/{second_context['shipment_id']}/custody-incidents",
            headers=regional_auth,
        )

    assert first.status_code == 201, first.text
    assert first.json()["shipment_id"] == first_context["shipment_id"]
    assert second.status_code == 409, second.text
    assert second_incidents.status_code == 200, second_incidents.text
    assert second_incidents.json()["total"] == 0


@pytest.mark.asyncio
async def test_incident_create_persists_path_scope_separately_from_body_hash() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context = await create_pickup_fixture(client)
        regional_auth, actor_id = await authorize_region(
            await shipment_region(context["shipment_id"]), client, return_user_id=True
        )
        key = f"incident-scope-persistence-{uuid4().hex}"
        payload = {
            "phase": "PICKUP",
            "kind": "PICKUP_UNCONFIRMED",
            "container_id": None,
            "reason": "Persist route scope apart from the validated body",
        }
        created = await client.post(
            f"/api/v1/shipments/{context['shipment_id']}/custody-incidents",
            headers={**regional_auth, "Idempotency-Key": key},
            json=payload,
        )

    body_hash = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()
    async with get_session_factory()() as session:
        record = await session.scalar(
            select(IdempotencyRecord).where(
                IdempotencyRecord.actor_user_id == UUID(actor_id),
                IdempotencyRecord.operation == "shipment.custody_incident.create",
                IdempotencyRecord.key == key,
            )
        )
        audit = await session.scalar(
            select(AuditEvent).where(
                AuditEvent.actor_user_id == UUID(actor_id),
                AuditEvent.operation == "shipment.custody_incident.create",
                AuditEvent.request_key == key,
            )
        )

    assert created.status_code == 201, created.text
    assert record is not None
    assert record.request_hash == body_hash
    assert record.request_context == {"shipment_id": context["shipment_id"]}
    assert audit is not None
    assert audit.details["shipment_id"] == context["shipment_id"]


@pytest.mark.asyncio
async def test_incident_resolution_key_cannot_replay_to_another_incident() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        first_context = await create_pickup_fixture(client)
        second_context = await create_pickup_fixture(client)
        regional_auth = await authorize_region(
            await shipment_region(first_context["shipment_id"]), client
        )
        incident_payload = {
            "phase": "PICKUP",
            "kind": "PICKUP_UNCONFIRMED",
            "container_id": None,
            "reason": "Courier did not confirm pickup",
        }
        first_incident = await client.post(
            f"/api/v1/shipments/{first_context['shipment_id']}/custody-incidents",
            headers={**regional_auth, "Idempotency-Key": f"open-first-{uuid4().hex}"},
            json=incident_payload,
        )
        second_incident = await client.post(
            f"/api/v1/shipments/{second_context['shipment_id']}/custody-incidents",
            headers={**regional_auth, "Idempotency-Key": f"open-second-{uuid4().hex}"},
            json=incident_payload,
        )
        assert first_incident.status_code == second_incident.status_code == 201

        resolution_payload = {
            "resolution": "ESCALATED_NO_CUSTODY_CHANGE",
            "reason": "Escalated for regional review",
        }
        key = f"cross-incident-resolution-{uuid4().hex}"
        first_resolution = await client.post(
            f"/api/v1/shipments/{first_context['shipment_id']}/custody-incidents/"
            f"{first_incident.json()['id']}/resolve",
            headers={**regional_auth, "Idempotency-Key": key},
            json=resolution_payload,
        )
        second_resolution = await client.post(
            f"/api/v1/shipments/{second_context['shipment_id']}/custody-incidents/"
            f"{second_incident.json()['id']}/resolve",
            headers={**regional_auth, "Idempotency-Key": key},
            json=resolution_payload,
        )

    async with get_session_factory()() as session:
        second_incident_row = await session.scalar(
            select(CustodyIncident).where(CustodyIncident.id == UUID(second_incident.json()["id"]))
        )

    assert first_resolution.status_code == 200, first_resolution.text
    assert first_resolution.json()["id"] == first_incident.json()["id"]
    assert second_resolution.status_code == 409, second_resolution.text
    assert second_incident_row is not None
    assert second_incident_row.status == "OPEN"
    assert second_incident_row.resolved_by is None


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
async def test_scope_migration_recovers_known_target_and_rejects_unknown_one() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        first_context = await create_pickup_fixture(client)
        second_context = await create_pickup_fixture(client)
        regional_auth, actor_id = await authorize_region(
            await shipment_region(first_context["shipment_id"]), client, return_user_id=True
        )
        payload = {
            "phase": "PICKUP",
            "kind": "PICKUP_UNCONFIRMED",
            "container_id": None,
            "reason": "Legacy incident target for migration replay",
        }
        replay_key = f"legacy-scope-replay-{uuid4().hex}"
        original = await client.post(
            f"/api/v1/shipments/{first_context['shipment_id']}/custody-incidents",
            headers={**regional_auth, "Idempotency-Key": replay_key},
            json=payload,
        )
        assert original.status_code == 201, original.text

    backend = Path(__file__).resolve().parents[2]
    env = os.environ.copy()
    await get_engine().dispose()
    downgraded = await asyncio.to_thread(
        _run_alembic, ["downgrade", "0010_notifications"], backend, env
    )
    assert downgraded.returncode == 0, downgraded.stderr

    unrecoverable_key = f"legacy-scope-unknown-{uuid4().hex}"
    request_hash = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()
    audit_event_id = uuid4()
    idempotency_record_id = uuid4()
    try:
        async with get_session_factory()() as session:
            await session.execute(
                text(
                    "INSERT INTO audit_events "
                    "(id, actor_user_id, operation, request_key, request_hash, "
                    "resource_id, details) "
                    "VALUES (:id, :actor_id, :operation, :request_key, :request_hash, "
                    ":resource_id, "
                    "CAST(:details AS jsonb))"
                ),
                {
                    "id": audit_event_id,
                    "actor_id": UUID(actor_id),
                    "operation": "shipment.custody_incident.create",
                    "request_key": unrecoverable_key,
                    "request_hash": request_hash,
                    "resource_id": original.json()["id"],
                    "details": json.dumps({"phase": "PICKUP"}),
                },
            )
            await session.execute(
                text(
                    "INSERT INTO idempotency_records "
                    "(id, actor_user_id, operation, key, request_hash, response_status, "
                    "response_body) "
                    "VALUES (:id, :actor_id, :operation, :key, :request_hash, :response_status, "
                    "CAST(:response_body AS jsonb))"
                ),
                {
                    "id": idempotency_record_id,
                    "actor_id": UUID(actor_id),
                    "operation": "shipment.custody_incident.create",
                    "key": unrecoverable_key,
                    "request_hash": request_hash,
                    "response_status": 201,
                    "response_body": json.dumps(original.json()),
                },
            )
            await session.commit()
    finally:
        await get_engine().dispose()
        upgraded = await asyncio.to_thread(_run_alembic, ["upgrade", "head"], backend, env)
        await get_engine().dispose()

    assert upgraded.returncode == 0, upgraded.stderr
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        recovered_replay = await client.post(
            f"/api/v1/shipments/{first_context['shipment_id']}/custody-incidents",
            headers={**regional_auth, "Idempotency-Key": replay_key},
            json=payload,
        )
        unknown_target_replay = await client.post(
            f"/api/v1/shipments/{second_context['shipment_id']}/custody-incidents",
            headers={**regional_auth, "Idempotency-Key": unrecoverable_key},
            json=payload,
        )

    async with get_session_factory()() as session:
        target_incident_count = await session.scalar(
            text("SELECT count(*) FROM custody_incidents WHERE shipment_id = :shipment_id"),
            {"shipment_id": UUID(second_context["shipment_id"])},
        )

    assert recovered_replay.status_code == 201, recovered_replay.text
    assert recovered_replay.json() == original.json()
    assert unknown_target_replay.status_code == 409, unknown_target_replay.text
    assert target_incident_count == 0


@pytest.mark.asyncio
async def test_pickup_attestation_requires_active_origin_sender() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context = await create_pickup_fixture(client)
        regional_auth = await authorize_region(
            await shipment_region(context["shipment_id"]), client
        )
        created = await client.post(
            f"/api/v1/shipments/{context['shipment_id']}/custody-incidents",
            headers={**regional_auth, "Idempotency-Key": f"pickup-incident-{context['suffix']}"},
            json={
                "phase": "PICKUP",
                "kind": "PICKUP_UNCONFIRMED",
                "reason": "Courier did not confirm pickup",
            },
        )
        attested = await client.post(
            f"/api/v1/shipments/{context['shipment_id']}/custody-incidents/"
            f"{created.json().get('id', '00000000-0000-0000-0000-000000000000')}/attest",
            headers={
                **context["sender_auth"],
                "Idempotency-Key": f"sender-attest-{context['suffix']}",
            },
            json={
                "statement": "All sealed containers remain at origin",
                "physical_release_occurred": False,
                "containers_at_origin": True,
            },
        )

    assert created.status_code == 201, created.text
    assert attested.status_code == 201, attested.text
    assert attested.json()["user_id"] == str(context["sender_id"])
    assert attested.json()["role_at_time"] == "SENDER_OPERATOR"
    assert attested.json()["physical_release_occurred"] is False


@pytest.mark.asyncio
async def test_delivery_attestation_requires_active_destination_lab_member() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context = await create_pickup_fixture(client)
        await confirm_pickup(context, client)
        regional_auth = await authorize_region(
            await shipment_region(context["shipment_id"]), client
        )
        incident = await open_delivery_incident(context, regional_auth, client)
        lab_auth = await authorize_lab(context["shipment_id"], client)
        response = await client.post(
            f"/api/v1/shipments/{context['shipment_id']}/custody-incidents/"
            f"{incident.json().get('id', '00000000-0000-0000-0000-000000000000')}/attest",
            headers={**lab_auth, "Idempotency-Key": f"lab-attest-{context['suffix']}"},
            json={
                "container_code": context["container_code"],
                "statement": "I personally observed the sealed container at the lab",
                "physically_present": True,
            },
        )

    assert incident.status_code == 201, incident.text
    assert response.status_code == 201, response.text
    assert response.json()["container_code"] == context["container_code"]
    assert response.json()["physically_present"] is True
    assert response.json()["role_at_time"] == "LAB_OPERATOR"


@pytest.mark.asyncio
async def test_attestation_actor_is_jwt_identity_and_unique() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context = await create_pickup_fixture(client)
        await confirm_pickup(context, client)
        regional_auth = await authorize_region(
            await shipment_region(context["shipment_id"]), client
        )
        incident = await open_delivery_incident(context, regional_auth, client)
        lab_auth = await authorize_lab(context["shipment_id"], client)
        path = (
            f"/api/v1/shipments/{context['shipment_id']}/custody-incidents/"
            f"{incident.json().get('id', '00000000-0000-0000-0000-000000000000')}/attest"
        )
        payload = {
            "container_code": context["container_code"],
            "statement": "I am physically present with this container",
            "physically_present": True,
        }
        first = await client.post(
            path,
            headers={**lab_auth, "Idempotency-Key": f"first-attest-{context['suffix']}"},
            json=payload,
        )
        duplicate = await client.post(
            path,
            headers={**lab_auth, "Idempotency-Key": f"second-attest-{context['suffix']}"},
            json=payload,
        )

    assert first.status_code == 201, first.text
    assert first.json()["user_id"] != context["courier_id"]
    assert duplicate.status_code == 409


@pytest.mark.asyncio
async def test_incident_phase_kind_container_combinations_are_validated() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context = await create_pickup_fixture(client)
        regional_auth = await authorize_region(
            await shipment_region(context["shipment_id"]), client
        )
        pickup_with_container = await client.post(
            f"/api/v1/shipments/{context['shipment_id']}/custody-incidents",
            headers={**regional_auth, "Idempotency-Key": f"invalid-pickup-{context['suffix']}"},
            json={
                "phase": "PICKUP",
                "kind": "PICKUP_UNCONFIRMED",
                "container_id": context["container_id"],
                "reason": "Invalid Pickup scope",
            },
        )
        delivery_without_container = await client.post(
            f"/api/v1/shipments/{context['shipment_id']}/custody-incidents",
            headers={**regional_auth, "Idempotency-Key": f"invalid-delivery-{context['suffix']}"},
            json={
                "phase": "DELIVERY",
                "kind": "PICKUP_UNCONFIRMED",
                "reason": "Invalid phase and kind",
            },
        )

    assert pickup_with_container.status_code == 409
    assert delivery_without_container.status_code in (409, 422)


@pytest.mark.asyncio
async def test_database_guards_custody_tables_and_attestation_immutability() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context = await create_pickup_fixture(client)
        await confirm_pickup(context, client)
        regional_auth = await authorize_region(
            await shipment_region(context["shipment_id"]), client
        )
        incident = await open_delivery_incident(context, regional_auth, client)
        lab_auth = await authorize_lab(context["shipment_id"], client, manager=True)
        attestation = await client.post(
            f"/api/v1/shipments/{context['shipment_id']}/custody-incidents/"
            f"{incident.json()['id']}/attest",
            headers={**lab_auth, "Idempotency-Key": f"immutable-attest-{context['suffix']}"},
            json={
                "container_code": context["container_code"],
                "statement": "The container is physically present",
                "physically_present": True,
            },
        )

    assert attestation.status_code == 201, attestation.text
    async with get_session_factory()() as session:
        names = await session.execute(
            text(
                "SELECT tablename FROM pg_tables WHERE schemaname='public' AND tablename IN "
                "('custody_incidents','custody_attestations','container_receipt_evidence')"
            )
        )
        assert {row[0] for row in names} == {
            "custody_incidents",
            "custody_attestations",
            "container_receipt_evidence",
        }
        with pytest.raises(DBAPIError):
            await session.execute(
                text("UPDATE custody_attestations SET statement='rewritten' WHERE id=:id"),
                {"id": attestation.json()["id"]},
            )


@pytest.mark.asyncio
async def test_exceptional_receipt_starts_review_without_receiving_container() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context = await create_pickup_fixture(client)
        await confirm_pickup(context, client)
        regional_auth = await authorize_region(
            await shipment_region(context["shipment_id"]), client
        )
        path = (
            f"/api/v1/shipments/{context['shipment_id']}/containers/"
            f"{context['container_id']}/exceptional-receipts"
        )
        started = await client.post(
            path,
            headers={**regional_auth, "Idempotency-Key": f"exceptional-start-{context['suffix']}"},
            json={"reason": "Courier cannot complete delivery confirmation"},
        )
        manifest = await client.get(
            f"/api/v1/shipments/{context['shipment_id']}/manifest", headers=context["sender_auth"]
        )

    assert started.status_code == 201, started.text
    assert started.json()["phase"] == "DELIVERY"
    assert started.json()["status"] == "OPEN"
    assert manifest.json()["containers"][0]["delivery_status"] == "IN_TRANSIT"
    assert manifest.json()["status"] == "IN_TRANSIT"


@pytest.mark.asyncio
async def test_exceptional_review_requires_confirmed_pickup_and_eligible_container() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context = await create_pickup_fixture(client)
        regional_auth = await authorize_region(
            await shipment_region(context["shipment_id"]), client
        )
        path = (
            f"/api/v1/shipments/{context['shipment_id']}/containers/"
            f"{context['container_id']}/exceptional-receipts"
        )
        unconfirmed_pickup = await client.post(
            path,
            headers={
                **regional_auth,
                "Idempotency-Key": f"exceptional-no-pickup-{context['suffix']}",
            },
            json={"reason": "Attempt before pickup"},
        )
        await confirm_pickup(context, client)
        started = await client.post(
            path,
            headers={
                **regional_auth,
                "Idempotency-Key": f"exceptional-eligible-{context['suffix']}",
            },
            json={"reason": "Courier cannot complete delivery confirmation"},
        )
        duplicate = await client.post(
            path,
            headers={
                **regional_auth,
                "Idempotency-Key": f"exceptional-eligible-{context['suffix']}",
            },
            json={"reason": "Courier cannot complete delivery confirmation"},
        )

    assert unconfirmed_pickup.status_code == 409
    assert started.status_code == 201, started.text
    assert duplicate.status_code == 201
    assert duplicate.json() == started.json()


async def attest_delivery(
    context: dict, incident_id: str, lab_auth: dict, client: AsyncClient, *, key: str
):
    return await client.post(
        f"/api/v1/shipments/{context['shipment_id']}/custody-incidents/{incident_id}/attest",
        headers={**lab_auth, "Idempotency-Key": key},
        json={
            "container_code": context["container_code"],
            "statement": "I personally observed the physical container at the lab",
            "physically_present": True,
        },
    )


@pytest.mark.asyncio
async def test_exceptional_resolution_is_atomic_and_never_records_regional_receiver() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context = await create_pickup_fixture(client, container_count=2)
        await confirm_pickup(context, client)
        regional_auth = await authorize_region(
            await shipment_region(context["shipment_id"]), client
        )
        target = context["containers"][0]
        delivery_url = (
            f"/api/v1/shipments/{context['shipment_id']}/containers/{target['id']}"
            "/handoffs/delivery"
        )
        pending = await client.post(
            f"{delivery_url}/confirm-delivery",
            headers={
                **context["courier_auth"],
                "Idempotency-Key": f"exceptional-pending-{context['suffix']}",
            },
            json={"container_code": target["code"]},
        )
        started = await client.post(
            f"/api/v1/shipments/{context['shipment_id']}/containers/{target['id']}"
            "/exceptional-receipts",
            headers={**regional_auth, "Idempotency-Key": f"exceptional-review-{context['suffix']}"},
            json={"reason": "Courier device failed at the Lab"},
        )
        manager_auth = await authorize_lab(context["shipment_id"], client, manager=True)
        operator_auth = await authorize_lab(context["shipment_id"], client)
        first = await attest_delivery(
            context,
            started.json()["id"],
            manager_auth,
            client,
            key=f"witness-a-{context['suffix']}",
        )
        second = await attest_delivery(
            context,
            started.json()["id"],
            operator_auth,
            client,
            key=f"witness-b-{context['suffix']}",
        )
        resolved = await client.post(
            f"/api/v1/shipments/{context['shipment_id']}/custody-incidents/{started.json()['id']}/resolve",
            headers={
                **regional_auth,
                "Idempotency-Key": f"resolve-exceptional-{context['suffix']}",
            },
            json={
                "resolution": "EXCEPTIONAL_RECEIPT_VERIFIED",
                "reason": "Two Lab witnesses verified",
            },
        )
        replay = await client.post(
            f"/api/v1/shipments/{context['shipment_id']}/custody-incidents/{started.json()['id']}/resolve",
            headers={
                **regional_auth,
                "Idempotency-Key": f"resolve-exceptional-{context['suffix']}",
            },
            json={
                "resolution": "EXCEPTIONAL_RECEIPT_VERIFIED",
                "reason": "Two Lab witnesses verified",
            },
        )
        manifest = await client.get(
            f"/api/v1/shipments/{context['shipment_id']}/manifest", headers=context["sender_auth"]
        )

    assert pending.status_code == 200, pending.text
    assert started.status_code == 201, started.text
    assert first.status_code == second.status_code == 201
    assert resolved.status_code == 200, resolved.text
    assert replay.status_code == 200
    assert replay.json() == resolved.json()
    assert resolved.json()["resolution"] == "EXCEPTIONAL_RECEIPT_VERIFIED"
    assert manifest.json()["containers"][0]["delivery_status"] == "RECEIVED"
    assert manifest.json()["containers"][0]["receipt_evidence_method"] == "EXCEPTIONAL"
    assert manifest.json()["containers"][1]["delivery_status"] == "IN_TRANSIT"
    assert manifest.json()["status"] == "PARTIALLY_DELIVERED"
    async with get_session_factory()() as session:
        receipt = await session.execute(
            text(
                "SELECT method, resolved_incident_id, verified_by "
                "FROM container_receipt_evidence WHERE container_id=:id"
            ),
            {"id": target["id"]},
        )
        evidence = receipt.one()
        recipient = await session.scalar(
            text("SELECT received_by FROM transport_containers WHERE id=:id"), {"id": target["id"]}
        )
        handoff_status = await session.scalar(
            text("SELECT status FROM handoffs WHERE container_id=:id AND kind='DELIVERY'"),
            {"id": target["id"]},
        )
        open_sessions = await session.scalar(
            text("SELECT count(*) FROM receiving_sessions WHERE shipment_id=:id AND status='OPEN'"),
            {"id": context["shipment_id"]},
        )
        evidence_count = await session.scalar(
            text("SELECT count(*) FROM container_receipt_evidence WHERE container_id=:id"),
            {"id": target["id"]},
        )
        audit_count = await session.scalar(
            text(
                "SELECT count(*) FROM audit_events "
                "WHERE operation='shipment.custody_incident.resolve' "
                "AND request_key=:key"
            ),
            {"key": f"resolve-exceptional-{context['suffix']}"},
        )
    assert evidence.method == "EXCEPTIONAL"
    assert str(evidence.resolved_incident_id) == started.json()["id"]
    assert str(evidence.verified_by) != first.json()["user_id"]
    assert recipient is None
    assert handoff_status == "SUPERSEDED"
    assert open_sessions == 1
    assert evidence_count == audit_count == 1


@pytest.mark.asyncio
async def test_non_custody_resolution_leaves_pickup_unchanged() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context = await create_pickup_fixture(client)
        regional_auth = await authorize_region(
            await shipment_region(context["shipment_id"]), client
        )
        created = await client.post(
            f"/api/v1/shipments/{context['shipment_id']}/custody-incidents",
            headers={
                **regional_auth,
                "Idempotency-Key": f"no-transfer-incident-{context['suffix']}",
            },
            json={"phase": "PICKUP", "kind": "PICKUP_UNCONFIRMED", "reason": "Pickup disputed"},
        )
        attested = await client.post(
            f"/api/v1/shipments/{context['shipment_id']}/custody-incidents/{created.json()['id']}/attest",
            headers={
                **context["sender_auth"],
                "Idempotency-Key": f"no-transfer-attest-{context['suffix']}",
            },
            json={
                "statement": "No container left our origin",
                "physical_release_occurred": False,
                "containers_at_origin": True,
            },
        )
        resolved = await client.post(
            f"/api/v1/shipments/{context['shipment_id']}/custody-incidents/{created.json()['id']}/resolve",
            headers={
                **regional_auth,
                "Idempotency-Key": f"no-transfer-resolve-{context['suffix']}",
            },
            json={"resolution": "NO_PHYSICAL_TRANSFER", "reason": "Sender confirms no release"},
        )
        shipment = await client.get(
            f"/api/v1/shipments/{context['shipment_id']}", headers=context["sender_auth"]
        )

    assert created.status_code == attested.status_code == 201
    assert resolved.status_code == 200, resolved.text
    assert shipment.json()["status"] == "READY_FOR_PICKUP"


@pytest.mark.asyncio
async def test_delivery_referral_requires_active_lab_manager_and_leaves_custody_unchanged() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context = await create_pickup_fixture(client)
        await confirm_pickup(context, client)
        regional_auth = await authorize_region(
            await shipment_region(context["shipment_id"]), client
        )
        incident = await open_delivery_incident(context, regional_auth, client)
        resolve_url = (
            f"/api/v1/shipments/{context['shipment_id']}/custody-incidents/"
            f"{incident.json()['id']}/resolve"
        )
        without_lab_evidence = await client.post(
            resolve_url,
            headers={**regional_auth, "Idempotency-Key": f"referral-no-lab-{context['suffix']}"},
            json={
                "resolution": "CONTAINER_ISSUE_REFERRED",
                "reason": "Regional review refers the stalled delivery",
            },
        )
        manifest = await client.get(
            f"/api/v1/shipments/{context['shipment_id']}/manifest", headers=context["sender_auth"]
        )

    assert without_lab_evidence.status_code == 409
    assert manifest.json()["status"] == "IN_TRANSIT"
    assert manifest.json()["containers"][0]["delivery_status"] == "IN_TRANSIT"


@pytest.mark.asyncio
async def test_manager_referral_resolution_records_no_custody_change() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context = await create_pickup_fixture(client)
        await confirm_pickup(context, client)
        regional_auth = await authorize_region(
            await shipment_region(context["shipment_id"]), client
        )
        incident = await open_delivery_incident(context, regional_auth, client)
        manager_auth = await authorize_lab(context["shipment_id"], client, manager=True)
        attestation = await attest_delivery(
            context,
            incident.json()["id"],
            manager_auth,
            client,
            key=f"referral-lab-{context['suffix']}",
        )
        resolved = await client.post(
            f"/api/v1/shipments/{context['shipment_id']}/custody-incidents/"
            f"{incident.json()['id']}/resolve",
            headers={**regional_auth, "Idempotency-Key": f"referral-resolve-{context['suffix']}"},
            json={
                "resolution": "CONTAINER_ISSUE_REFERRED",
                "reason": "Lab Manager confirms issue review is appropriate",
            },
        )
        manifest = await client.get(
            f"/api/v1/shipments/{context['shipment_id']}/manifest", headers=context["sender_auth"]
        )

    assert attestation.status_code == 201, attestation.text
    assert resolved.status_code == 200, resolved.text
    assert manifest.json()["status"] == "IN_TRANSIT"
    assert manifest.json()["containers"][0]["delivery_status"] == "IN_TRANSIT"


@pytest.mark.asyncio
async def test_no_physical_transfer_cannot_reverse_a_confirmed_pickup() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context = await create_pickup_fixture(client)
        await confirm_pickup(context, client)
        regional_auth = await authorize_region(
            await shipment_region(context["shipment_id"]), client
        )
        incident = await client.post(
            f"/api/v1/shipments/{context['shipment_id']}/custody-incidents",
            headers={
                **regional_auth,
                "Idempotency-Key": f"confirmed-pickup-incident-{context['suffix']}",
            },
            json={
                "phase": "PICKUP",
                "kind": "COURIER_UNAVAILABLE_AFTER_PICKUP",
                "reason": "Sender disputes the physical Pickup",
            },
        )
        attestation = await client.post(
            f"/api/v1/shipments/{context['shipment_id']}/custody-incidents/"
            f"{incident.json()['id']}/attest",
            headers={
                **context["sender_auth"],
                "Idempotency-Key": f"confirmed-pickup-sender-{context['suffix']}",
            },
            json={
                "statement": "Our team states the containers stayed at origin",
                "physical_release_occurred": False,
                "containers_at_origin": True,
            },
        )
        resolved = await client.post(
            f"/api/v1/shipments/{context['shipment_id']}/custody-incidents/"
            f"{incident.json()['id']}/resolve",
            headers={
                **regional_auth,
                "Idempotency-Key": f"confirmed-pickup-resolve-{context['suffix']}",
            },
            json={
                "resolution": "NO_PHYSICAL_TRANSFER",
                "reason": "This outcome must not contradict confirmed Pickup",
            },
        )
        shipment = await client.get(
            f"/api/v1/shipments/{context['shipment_id']}", headers=context["sender_auth"]
        )

    assert incident.status_code == attestation.status_code == 201
    assert resolved.status_code == 409
    assert shipment.json()["status"] == "IN_TRANSIT"


@pytest.mark.asyncio
async def test_two_lab_operators_without_a_manager_cannot_resolve_exceptional_receipt() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context = await create_pickup_fixture(client)
        await confirm_pickup(context, client)
        regional_auth = await authorize_region(
            await shipment_region(context["shipment_id"]), client
        )
        incident = await open_delivery_incident(context, regional_auth, client)
        first_auth = await authorize_lab(context["shipment_id"], client)
        second_auth = await authorize_lab(context["shipment_id"], client)
        first = await attest_delivery(
            context,
            incident.json()["id"],
            first_auth,
            client,
            key=f"operator-a-{context['suffix']}",
        )
        second = await attest_delivery(
            context,
            incident.json()["id"],
            second_auth,
            client,
            key=f"operator-b-{context['suffix']}",
        )
        resolved = await client.post(
            f"/api/v1/shipments/{context['shipment_id']}/custody-incidents/"
            f"{incident.json()['id']}/resolve",
            headers={**regional_auth, "Idempotency-Key": f"two-operators-{context['suffix']}"},
            json={
                "resolution": "EXCEPTIONAL_RECEIPT_VERIFIED",
                "reason": "Two operators saw the container",
            },
        )
        manifest = await client.get(
            f"/api/v1/shipments/{context['shipment_id']}/manifest", headers=context["sender_auth"]
        )

    assert first.status_code == second.status_code == 201
    assert resolved.status_code == 409
    assert manifest.json()["containers"][0]["delivery_status"] == "IN_TRANSIT"


@pytest.mark.asyncio
async def test_one_account_with_both_lab_roles_counts_as_one_witness() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context = await create_pickup_fixture(client)
        await confirm_pickup(context, client)
        regional_auth = await authorize_region(
            await shipment_region(context["shipment_id"]), client
        )
        incident = await open_delivery_incident(context, regional_auth, client)
        manager_auth = await authorize_lab(
            context["shipment_id"], client, manager=True, also_operator=True
        )
        first = await attest_delivery(
            context,
            incident.json()["id"],
            manager_auth,
            client,
            key=f"dual-first-{context['suffix']}",
        )
        duplicate = await attest_delivery(
            context,
            incident.json()["id"],
            manager_auth,
            client,
            key=f"dual-second-{context['suffix']}",
        )
        resolved = await client.post(
            f"/api/v1/shipments/{context['shipment_id']}/custody-incidents/"
            f"{incident.json()['id']}/resolve",
            headers={**regional_auth, "Idempotency-Key": f"dual-resolve-{context['suffix']}"},
            json={
                "resolution": "EXCEPTIONAL_RECEIPT_VERIFIED",
                "reason": "One user has two Lab roles",
            },
        )

    assert first.status_code == 201, first.text
    assert first.json()["role_at_time"] == "LAB_MANAGER"
    assert duplicate.status_code == 409
    assert resolved.status_code == 409


@pytest.mark.asyncio
async def test_witness_membership_is_revalidated_when_resolving() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context = await create_pickup_fixture(client)
        await confirm_pickup(context, client)
        regional_auth = await authorize_region(
            await shipment_region(context["shipment_id"]), client
        )
        incident = await open_delivery_incident(context, regional_auth, client)
        manager_auth, manager_id = await authorize_lab(
            context["shipment_id"], client, manager=True, return_user_id=True
        )
        operator_auth = await authorize_lab(context["shipment_id"], client)
        first = await attest_delivery(
            context,
            incident.json()["id"],
            manager_auth,
            client,
            key=f"active-manager-{context['suffix']}",
        )
        second = await attest_delivery(
            context,
            incident.json()["id"],
            operator_auth,
            client,
            key=f"active-operator-{context['suffix']}",
        )
        async with get_session_factory()() as session:
            await session.execute(
                text(
                    "UPDATE memberships SET is_active=false WHERE user_id=:user_id "
                    "AND organization_id=(SELECT destination_organization_id "
                    "FROM shipments WHERE id=:shipment_id) "
                    "AND role='LAB_MANAGER'"
                ),
                {"user_id": manager_id, "shipment_id": context["shipment_id"]},
            )
            await session.commit()
        resolved = await client.post(
            f"/api/v1/shipments/{context['shipment_id']}/custody-incidents/"
            f"{incident.json()['id']}/resolve",
            headers={**regional_auth, "Idempotency-Key": f"revoked-manager-{context['suffix']}"},
            json={
                "resolution": "EXCEPTIONAL_RECEIPT_VERIFIED",
                "reason": "Resolve after manager scope is revoked",
            },
        )
        manifest = await client.get(
            f"/api/v1/shipments/{context['shipment_id']}/manifest", headers=context["sender_auth"]
        )

    assert first.status_code == second.status_code == 201
    assert resolved.status_code == 409
    assert manifest.json()["containers"][0]["delivery_status"] == "IN_TRANSIT"


@pytest.mark.asyncio
async def test_regional_resolver_cannot_be_one_of_the_witnesses() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context = await create_pickup_fixture(client)
        await confirm_pickup(context, client)
        region_id = await shipment_region(context["shipment_id"])
        incident = await open_delivery_incident(
            context, await authorize_region(region_id, client), client
        )
        resolver_auth = await authorize_lab(
            context["shipment_id"],
            client,
            manager=True,
            region_id=region_id,
        )
        operator_auth = await authorize_lab(context["shipment_id"], client)
        first = await attest_delivery(
            context,
            incident.json()["id"],
            resolver_auth,
            client,
            key=f"resolver-witness-{context['suffix']}",
        )
        second = await attest_delivery(
            context,
            incident.json()["id"],
            operator_auth,
            client,
            key=f"other-witness-{context['suffix']}",
        )
        resolved = await client.post(
            f"/api/v1/shipments/{context['shipment_id']}/custody-incidents/"
            f"{incident.json()['id']}/resolve",
            headers={**resolver_auth, "Idempotency-Key": f"witness-resolver-{context['suffix']}"},
            json={
                "resolution": "EXCEPTIONAL_RECEIPT_VERIFIED",
                "reason": "Resolver is not independent",
            },
        )

    assert first.status_code == second.status_code == 201
    assert resolved.status_code == 409


@pytest.mark.asyncio
async def test_exceptional_and_normal_receipt_race_creates_one_evidence_and_session() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context = await create_pickup_fixture(client)
        await confirm_pickup(context, client)
        regional_auth = await authorize_region(
            await shipment_region(context["shipment_id"]), client
        )
        target = context["containers"][0]
        delivery_url = (
            f"/api/v1/shipments/{context['shipment_id']}/containers/{target['id']}"
            "/handoffs/delivery"
        )
        initiated = await client.post(
            f"{delivery_url}/confirm-delivery",
            headers={
                **context["courier_auth"],
                "Idempotency-Key": f"race-init-{context['suffix']}",
            },
            json={"container_code": target["code"]},
        )
        started = await client.post(
            f"/api/v1/shipments/{context['shipment_id']}/containers/{target['id']}"
            "/exceptional-receipts",
            headers={**regional_auth, "Idempotency-Key": f"race-start-{context['suffix']}"},
            json={"reason": "Courier confirmation unavailable"},
        )
        manager_auth = await authorize_lab(context["shipment_id"], client, manager=True)
        operator_auth = await authorize_lab(context["shipment_id"], client)
        first = await attest_delivery(
            context,
            started.json()["id"],
            manager_auth,
            client,
            key=f"race-manager-{context['suffix']}",
        )
        second = await attest_delivery(
            context,
            started.json()["id"],
            operator_auth,
            client,
            key=f"race-operator-{context['suffix']}",
        )
        normal_request = client.post(
            f"{delivery_url}/confirm-receipt",
            headers={**manager_auth, "Idempotency-Key": f"race-normal-{context['suffix']}"},
            json={"container_code": target["code"]},
        )
        exceptional_request = client.post(
            f"/api/v1/shipments/{context['shipment_id']}/custody-incidents/"
            f"{started.json()['id']}/resolve",
            headers={**regional_auth, "Idempotency-Key": f"race-exception-{context['suffix']}"},
            json={
                "resolution": "EXCEPTIONAL_RECEIPT_VERIFIED",
                "reason": "Two current witnesses verified receipt",
            },
        )
        normal, exceptional = await asyncio.gather(normal_request, exceptional_request)

    assert initiated.status_code == 200, initiated.text
    assert started.status_code == 201, started.text
    assert first.status_code == second.status_code == 201
    assert sorted([normal.status_code, exceptional.status_code]) == [200, 409]
    async with get_session_factory()() as session:
        evidence = await session.execute(
            text("SELECT method FROM container_receipt_evidence WHERE container_id=:id"),
            {"id": target["id"]},
        )
        receipt_methods = list(evidence.scalars())
        open_sessions = await session.scalar(
            text("SELECT count(*) FROM receiving_sessions WHERE shipment_id=:id AND status='OPEN'"),
            {"id": context["shipment_id"]},
        )
        container_state = await session.scalar(
            text("SELECT delivery_status FROM transport_containers WHERE id=:id"),
            {"id": target["id"]},
        )
    assert len(receipt_methods) == 1
    assert receipt_methods[0] in {"NORMAL", "EXCEPTIONAL"}
    assert open_sessions == 1
    assert container_state == "RECEIVED"


@pytest.mark.asyncio
async def test_database_rejects_exceptional_evidence_without_current_manager_witness() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context = await create_pickup_fixture(client)
        await confirm_pickup(context, client)
        regional_auth, resolver_id = await authorize_region(
            await shipment_region(context["shipment_id"]), client, return_user_id=True
        )
        incident = await open_delivery_incident(context, regional_auth, client)
        manager_auth, manager_id = await authorize_lab(
            context["shipment_id"], client, manager=True, also_operator=True, return_user_id=True
        )
        operator_a = await authorize_lab(context["shipment_id"], client)
        operator_b = await authorize_lab(context["shipment_id"], client)
        attestations = [
            await attest_delivery(
                context,
                incident.json()["id"],
                auth,
                client,
                key=f"sql-witness-{index}-{context['suffix']}",
            )
            for index, auth in enumerate((manager_auth, operator_a, operator_b))
        ]

    assert all(attestation.status_code == 201 for attestation in attestations)
    async with get_session_factory()() as session:
        await session.execute(
            text(
                "UPDATE custody_incidents SET status='RESOLVED', resolved_by=:resolver_id, "
                "resolved_at=now(), resolution='EXCEPTIONAL_RECEIPT_VERIFIED' WHERE id=:incident_id"
            ),
            {"resolver_id": resolver_id, "incident_id": incident.json()["id"]},
        )
        await session.execute(
            text(
                "UPDATE memberships SET is_active=false WHERE user_id=:manager_id "
                "AND organization_id=(SELECT destination_organization_id "
                "FROM shipments WHERE id=:shipment_id) "
                "AND role='LAB_MANAGER'"
            ),
            {"manager_id": manager_id, "shipment_id": context["shipment_id"]},
        )
        with pytest.raises(DBAPIError):
            await session.execute(
                text(
                    "INSERT INTO container_receipt_evidence "
                    "(id, container_id, method, resolved_incident_id, verified_by) "
                    "VALUES (:id, :container_id, 'EXCEPTIONAL', :incident_id, :resolver_id)"
                ),
                {
                    "id": str(uuid4()),
                    "container_id": context["container_id"],
                    "incident_id": incident.json()["id"],
                    "resolver_id": resolver_id,
                },
            )


@pytest.mark.asyncio
async def test_audit_failure_rolls_back_exceptional_receipt_and_idempotency() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
    ) as client:
        context = await create_pickup_fixture(client)
        await confirm_pickup(context, client)
        regional_auth = await authorize_region(
            await shipment_region(context["shipment_id"]), client
        )
        target = context["containers"][0]
        delivery_url = (
            f"/api/v1/shipments/{context['shipment_id']}/containers/{target['id']}"
            "/handoffs/delivery"
        )
        initiated = await client.post(
            f"{delivery_url}/confirm-delivery",
            headers={
                **context["courier_auth"],
                "Idempotency-Key": f"rollback-init-{context['suffix']}",
            },
            json={"container_code": target["code"]},
        )
        started = await client.post(
            f"/api/v1/shipments/{context['shipment_id']}/containers/{target['id']}"
            "/exceptional-receipts",
            headers={**regional_auth, "Idempotency-Key": f"rollback-start-{context['suffix']}"},
            json={"reason": "Courier confirmation unavailable"},
        )
        manager_auth = await authorize_lab(context["shipment_id"], client, manager=True)
        operator_auth = await authorize_lab(context["shipment_id"], client)
        first = await attest_delivery(
            context,
            started.json()["id"],
            manager_auth,
            client,
            key=f"rollback-manager-{context['suffix']}",
        )
        second = await attest_delivery(
            context,
            started.json()["id"],
            operator_auth,
            client,
            key=f"rollback-operator-{context['suffix']}",
        )

        engine = create_async_engine(get_settings().migration_database_url)
        trigger = "trg_test_reject_custody_resolution_audit"
        function = "test_reject_custody_resolution_audit"
        request_key = f"rollback-resolve-{context['suffix']}"
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        f"CREATE FUNCTION {function}() RETURNS trigger AS $$ BEGIN "
                        "IF NEW.operation='shipment.custody_incident.resolve' THEN "
                        "RAISE EXCEPTION 'injected custody audit failure'; END IF; "
                        "RETURN NEW; END; $$ LANGUAGE plpgsql"
                    )
                )
                await connection.execute(
                    text(
                        f"CREATE TRIGGER {trigger} BEFORE INSERT ON audit_events "
                        f"FOR EACH ROW EXECUTE FUNCTION {function}()"
                    )
                )
            failed = await client.post(
                f"/api/v1/shipments/{context['shipment_id']}/custody-incidents/"
                f"{started.json()['id']}/resolve",
                headers={**regional_auth, "Idempotency-Key": request_key},
                json={
                    "resolution": "EXCEPTIONAL_RECEIPT_VERIFIED",
                    "reason": "Injected audit failure must abort this operation",
                },
            )
            async with engine.begin() as connection:
                incident_status = await connection.scalar(
                    text("SELECT status FROM custody_incidents WHERE id=:incident_id"),
                    {"incident_id": started.json()["id"]},
                )
                container_status = await connection.scalar(
                    text("SELECT delivery_status FROM transport_containers WHERE id=:id"),
                    {"id": target["id"]},
                )
                handoff_status = await connection.scalar(
                    text("SELECT status FROM handoffs WHERE container_id=:id AND kind='DELIVERY'"),
                    {"id": target["id"]},
                )
                receipt_count = await connection.scalar(
                    text("SELECT count(*) FROM container_receipt_evidence WHERE container_id=:id"),
                    {"id": target["id"]},
                )
                replay_count = await connection.scalar(
                    text(
                        "SELECT count(*) FROM idempotency_records WHERE operation="
                        "'shipment.custody_incident.resolve' AND key=:request_key"
                    ),
                    {"request_key": request_key},
                )
                audit_count = await connection.scalar(
                    text(
                        "SELECT count(*) FROM audit_events WHERE operation="
                        "'shipment.custody_incident.resolve' AND request_key=:request_key"
                    ),
                    {"request_key": request_key},
                )
        finally:
            async with engine.begin() as connection:
                await connection.execute(text(f"DROP TRIGGER IF EXISTS {trigger} ON audit_events"))
                await connection.execute(text(f"DROP FUNCTION IF EXISTS {function}()"))
            await engine.dispose()

    assert initiated.status_code == 200, initiated.text
    assert started.status_code == 201, started.text
    assert first.status_code == second.status_code == 201
    assert failed.status_code == 500
    assert incident_status == "UNDER_REVIEW"
    assert container_status == "DELIVERY_PENDING"
    assert handoff_status == "PENDING"
    assert receipt_count == replay_count == audit_count == 0
