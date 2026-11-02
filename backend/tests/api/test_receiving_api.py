"""Receipt revision persistence and PostgreSQL receiving guards."""

import asyncio
import json
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, inspect, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from test_container_issues_api import (
    authorize_lab_manager,
    authorize_regional_operator,
    create_issue,
)
from test_delivery_api import authorize_lab_operator, confirm_pickup
from test_pickup_api import PASSWORD, create_pickup_fixture

from app.core.config import get_settings
from app.core.logging import JsonFormatter
from app.core.security import hash_password
from app.db.base import Base
from app.db.session import get_db_session, get_session_factory
from app.main import app
from app.modules.audit.models import AuditEvent
from app.modules.idempotency.models import IdempotencyRecord
from app.modules.identity.access import get_current_principal
from app.modules.identity.models import Membership, Organization, User
from app.modules.shipments.models import (
    ContainerItem,
    ReceiptRevision,
    Shipment,
    TransportContainer,
)
from app.modules.shipments.models import (
    ReceivingSession as ReceivingSessionModel,
)

RECEIPT_GUARD_ERROR = (
    "Receipt result requires a received container with verified receipt evidence "
    "and an OPEN receiving session"
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result,reason,comment,inactive,valid",
    [
        pytest.param(
            "REJECTED", "SAMPLE_LABEL_MISMATCH", "Label differs", False, True, id="valid-rejected"
        ),
        pytest.param("MISSING", "SAMPLE_NOT_FOUND", None, False, True, id="valid-missing"),
        pytest.param("ACCEPTED", None, None, False, True, id="accepted-without-reason"),
        pytest.param("REJECTED", "CONTAINER_SEAL_BROKEN", None, False, False, id="wrong-scope"),
        pytest.param("REJECTED", "SAMPLE_NOT_FOUND", None, False, False, id="wrong-kind"),
        pytest.param("REJECTED", "SAMPLE_LABEL_MISMATCH", None, True, False, id="inactive"),
        pytest.param("ACCEPTED", "SAMPLE_NOT_FOUND", None, False, False, id="accepted-with-reason"),
        pytest.param(
            "ACCEPTED", None, "Unexpected comment", False, False, id="accepted-with-comment"
        ),
        pytest.param("MISSING", None, None, False, False, id="missing-without-reason"),
        pytest.param(None, "SAMPLE_NOT_FOUND", None, False, False, id="reason-without-result"),
        pytest.param(None, None, "Unexpected comment", False, False, id="comment-without-result"),
    ],
)
async def test_database_validates_receipt_result_reason_pairs(
    result: str | None, reason: str | None, comment: str | None, inactive: bool, valid: bool
) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context = await create_pickup_fixture(client)
        await receive_first_container(context, client)
    item_id = await item_id_for_container(context["container_id"])
    engine = create_async_engine(get_settings().migration_database_url)
    try:
        async with engine.connect() as connection:
            if inactive:
                # Owner-only fixture setup; restore the catalog guard before the
                # result write and roll back the catalog change with this test.
                await connection.execute(
                    text(
                        "ALTER TABLE issue_reason_codes DISABLE TRIGGER trg_issue_reason_read_only"
                    )
                )
                await connection.execute(
                    text("UPDATE issue_reason_codes SET is_active=false WHERE code=:reason"),
                    {"reason": reason},
                )
                await connection.execute(
                    text("ALTER TABLE issue_reason_codes ENABLE TRIGGER trg_issue_reason_read_only")
                )
            statement = text(
                "UPDATE container_items SET receipt_result=:result, receipt_reason_code=:reason, "
                "receipt_comment=:comment, receipt_recorded_at=CASE WHEN :has_result "
                "THEN clock_timestamp() ELSE NULL END WHERE id=:item_id"
            )
            parameters = {
                "result": result,
                "reason": reason,
                "comment": comment,
                "item_id": item_id,
                "has_result": result is not None,
            }
            if valid:
                await connection.execute(statement, parameters)
                row = (
                    await connection.execute(
                        text(
                            "SELECT receipt_result, receipt_reason_code, receipt_comment "
                            "FROM container_items WHERE id=:item_id"
                        ),
                        {"item_id": item_id},
                    )
                ).one()
                assert tuple(row) == (result, reason, comment)
                await connection.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
            else:
                with pytest.raises(DBAPIError, match="Receipt result reason"):
                    await connection.execute(statement, parameters)
            await connection.rollback()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_database_validates_receipt_reason_on_insert() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context = await create_pickup_fixture(client)
    async with get_session_factory()() as session:
        # The result guard runs before manifest immutability and uniqueness checks.
        with pytest.raises(DBAPIError, match="Receipt result reason"):
            await session.execute(
                text(
                    "INSERT INTO container_items "
                    "(id, container_id, sample_id, receipt_reason_code) "
                    "SELECT :id, container_id, sample_id, 'SAMPLE_NOT_FOUND' "
                    "FROM container_items WHERE container_id=:container_id"
                ),
                {"id": uuid4(), "container_id": context["container_id"]},
            )
        await session.rollback()


@pytest.mark.asyncio
async def test_database_rejects_session_rotation_before_completion() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context = await create_pickup_fixture(client)
        await receive_first_container(context, client)
        await authorize_lab_manager(context["shipment_id"], client)
    async with get_session_factory()() as session:
        manager_id = await session.scalar(
            select(Membership.user_id)
            .join(Shipment, Shipment.destination_organization_id == Membership.organization_id)
            .where(Shipment.id == context["shipment_id"], Membership.role == "LAB_MANAGER")
        )
        assert manager_id is not None
        await session.execute(
            text(
                "UPDATE receiving_sessions SET status='CLOSED', closed_by=:actor, "
                "closed_at=clock_timestamp() WHERE shipment_id=:shipment_id AND status='OPEN'"
            ),
            {"actor": manager_id, "shipment_id": context["shipment_id"]},
        )
        with pytest.raises(
            DBAPIError, match="Reopened receiving session requires a completed Shipment"
        ):
            await session.execute(
                text(
                    "INSERT INTO receiving_sessions "
                    "(id, shipment_id, sequence, status, opened_by, opened_at, reopen_reason) "
                    "VALUES (:id, :shipment_id, 2, 'OPEN', :actor, "
                    "clock_timestamp(), 'Premature reopen')"
                ),
                {"id": uuid4(), "shipment_id": context["shipment_id"], "actor": manager_id},
            )
            await session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
        await session.rollback()


def test_stage_f_migration_is_additive_after_stage_e() -> None:
    backend = Path(__file__).resolve().parents[2]
    config = Config(str(backend / "alembic.ini"))
    config.set_main_option("script_location", str(backend / "alembic"))
    script = ScriptDirectory.from_config(config)
    head = script.get_current_head()
    revision = script.get_revision(head)
    stage_g_revision = script.get_revision("0009_stage_g_offline")
    stage_f_revision = script.get_revision("0006_stage_f")

    assert revision.revision == "0010_notifications"
    assert revision.down_revision == "0009_stage_g_offline"
    assert stage_g_revision.down_revision == "0008_stage_f_integrity"
    assert script.get_revision("0007_stage_f_lifecycle").down_revision == "0006_stage_f"
    assert stage_f_revision.down_revision == "0005_stage_e"


def test_stage_f_static_and_runtime_openapi_contracts_align() -> None:
    contract_path = Path(__file__).resolve().parents[3] / "openapi" / "labtrack-v1.json"
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    runtime = app.openapi()
    operations = (
        (
            "PATCH",
            "/shipments/{shipment_id}/receiving/items/{item_id}",
            "recordReceipt",
            "ReceiptResultRequest",
            "ContainerItem",
            "ContainerItem",
        ),
        (
            "POST",
            "/shipments/{shipment_id}/receiving/complete",
            "completeReceiving",
            None,
            "Shipment",
            "ShipmentResponse",
        ),
        (
            "POST",
            "/shipments/{shipment_id}/receiving/reopen",
            "reopenReceiving",
            "ReopenReceivingRequest",
            "Shipment",
            "ShipmentResponse",
        ),
        (
            "GET",
            "/shipments/{shipment_id}/receiving/history",
            "getReceivingHistory",
            None,
            "ReceivingHistory",
            "ReceivingHistory",
        ),
    )
    expected_statuses = {"200", "401", "403", "404", "409", "422", "503"}
    static_schemas = contract["components"]["schemas"]
    runtime_schemas = runtime["components"]["schemas"]

    for (
        method,
        path,
        operation_id,
        request_ref,
        static_response_ref,
        runtime_response_ref,
    ) in operations:
        static_operation = contract["paths"][path][method.lower()]
        runtime_operation = runtime["paths"][f"/api/v1{path}"][method.lower()]
        assert static_operation["operationId"] == runtime_operation["operationId"] == operation_id
        assert set(static_operation["responses"]) == set(runtime_operation["responses"])
        assert set(static_operation["responses"]) == expected_statuses

        if request_ref is not None:
            static_request_ref = static_operation["requestBody"]["content"]["application/json"][
                "schema"
            ]["$ref"].rsplit("/", 1)[1]
            runtime_request_ref = runtime_operation["requestBody"]["content"]["application/json"][
                "schema"
            ]["$ref"].rsplit("/", 1)[1]
            assert static_request_ref == runtime_request_ref == request_ref
        elif operation_id == "completeReceiving":
            static_body = static_operation["requestBody"]
            runtime_body = runtime_operation["requestBody"]
            assert static_body.get("required", False) is False
            assert runtime_body.get("required", False) is False
            static_body_schema = static_body["content"]["application/json"]["schema"]
            assert static_body_schema == {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            }
            runtime_body_schema = runtime_body["content"]["application/json"]["schema"]
            assert runtime_body_schema == {"$ref": "#/components/schemas/CompleteReceivingRequest"}
            runtime_body_ref = runtime_body_schema["$ref"].rsplit("/", 1)[1]
            assert "anyOf" not in runtime_body_schema
            runtime_empty_body = runtime_schemas[runtime_body_ref]
            assert {
                key: runtime_empty_body[key]
                for key in ("type", "properties", "additionalProperties")
            } == static_body_schema
        else:
            assert "requestBody" not in static_operation
            assert "requestBody" not in runtime_operation

        static_response = static_operation["responses"]["200"]["content"]["application/json"][
            "schema"
        ]["$ref"].rsplit("/", 1)[1]
        runtime_response = runtime_operation["responses"]["200"]["content"]["application/json"][
            "schema"
        ]["$ref"].rsplit("/", 1)[1]
        assert static_response == static_response_ref
        assert runtime_response == runtime_response_ref
        assert set(static_schemas[static_response]["properties"]) == set(
            runtime_schemas[runtime_response]["properties"]
        )
        assert set(static_schemas[static_response]["required"]) == set(
            runtime_schemas[runtime_response]["required"]
        )

    static_manifest_item = static_schemas["ContainerManifest"]["properties"]["items"]["items"]
    runtime_manifest_item = runtime_schemas["ContainerItemResponse"]
    assert set(static_manifest_item["properties"]) == set(runtime_manifest_item["properties"])
    assert set(static_manifest_item["required"]) == set(runtime_manifest_item["required"])
    assert set(static_manifest_item["required"]) == {
        "id",
        "sample_id",
        "sample_code",
        "material_type",
        "receipt_result",
        "receipt_reason_code",
        "receipt_comment",
    }

    for static_schema, runtime_schema in (
        (static_schemas["ContainerItem"], runtime_schemas["ContainerItem"]),
        (static_manifest_item, runtime_manifest_item),
    ):
        static_result = static_schema["properties"]["receipt_result"]
        runtime_result = runtime_schema["properties"]["receipt_result"]
        assert static_result["type"] == ["string", "null"]
        assert set(static_result["enum"]) == {"ACCEPTED", "MISSING", "REJECTED", None}
        assert {variant["type"] for variant in runtime_result["anyOf"]} == {"string", "null"}
        runtime_result_enum = next(
            set(variant["enum"]) for variant in runtime_result["anyOf"] if "enum" in variant
        )
        assert runtime_result_enum == {"ACCEPTED", "MISSING", "REJECTED"}

    static_history = contract["paths"]["/shipments/{shipment_id}/receiving/history"]["get"]
    runtime_history = runtime["paths"]["/api/v1/shipments/{shipment_id}/receiving/history"]["get"]
    static_query = {
        parameter["name"]: parameter["schema"]
        for parameter in static_history["parameters"]
        if parameter["in"] == "query"
    }
    runtime_query = {
        parameter["name"]: parameter["schema"]
        for parameter in runtime_history["parameters"]
        if parameter["in"] == "query"
    }
    assert set(static_query) == set(runtime_query) == {"limit", "offset"}
    assert (
        {name: schema["default"] for name, schema in static_query.items()}
        == {name: schema["default"] for name, schema in runtime_query.items()}
        == {"limit": 20, "offset": 0}
    )

    static_revision = static_schemas["ReceiptRevision"]
    runtime_revision = runtime_schemas["ReceiptRevision"]
    revision_fields = {
        "id",
        "receiving_session_id",
        "container_item_id",
        "old_result",
        "new_result",
        "old_reason_code",
        "new_reason_code",
        "old_comment",
        "new_comment",
        "changed_by",
        "changed_at",
    }
    assert (
        set(static_revision["properties"]) == set(runtime_revision["properties"]) == revision_fields
    )
    assert set(static_revision["required"]) == set(runtime_revision["required"]) == revision_fields
    assert static_revision["additionalProperties"] is False
    assert runtime_revision["additionalProperties"] is False
    for field in ("old_comment", "new_comment"):
        assert static_revision["properties"][field]["type"] == ["string", "null"]
        assert static_revision["properties"][field]["maxLength"] == 500
        runtime_types = {
            variant["type"] for variant in runtime_revision["properties"][field]["anyOf"]
        }
        assert runtime_types == {"string", "null"}
        runtime_string = next(
            variant
            for variant in runtime_revision["properties"][field]["anyOf"]
            if variant.get("type") == "string"
        )
        assert runtime_string["maxLength"] == 500


@pytest.mark.asyncio
async def test_complete_receiving_accepts_empty_or_omitted_body_but_rejects_null() -> None:
    class MissingShipmentSession:
        async def get(self, _model, _shipment_id):
            return None

    async def no_principal():
        return None

    async def missing_shipment_session():
        return MissingShipmentSession()

    original_overrides = app.dependency_overrides.copy()
    app.dependency_overrides[get_current_principal] = no_principal
    app.dependency_overrides[get_db_session] = missing_shipment_session
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            url = f"/api/v1/shipments/{uuid4()}/receiving/complete"
            headers = {"Idempotency-Key": "complete-body-shape"}
            omitted = await client.post(url, headers=headers)
            empty_object = await client.post(url, headers=headers, json={})
            explicit_null = await client.post(
                url,
                headers={**headers, "content-type": "application/json"},
                content="null",
            )
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(original_overrides)

    assert omitted.status_code == 404, omitted.text
    assert empty_object.status_code == 404, empty_object.text
    assert explicit_null.status_code == 422, explicit_null.text


async def receive_container(
    context: dict, client: AsyncClient, container: dict, lab_auth: dict[str, str]
) -> dict:
    url = (
        f"/api/v1/shipments/{context['shipment_id']}/containers/{container['id']}/handoffs/delivery"
    )
    initiated = await client.post(
        f"{url}/confirm-delivery",
        headers={
            **context["courier_auth"],
            "Idempotency-Key": f"receiving-delivery-{context['suffix']}",
        },
        json={"container_code": container["code"]},
    )
    assert initiated.status_code == 200, initiated.text
    receipt = await client.post(
        f"{url}/confirm-receipt",
        headers={
            **lab_auth,
            "Idempotency-Key": f"receiving-receipt-{context['suffix']}",
        },
        json={"container_code": container["code"]},
    )
    assert receipt.status_code == 200, receipt.text
    return receipt.json()


async def receive_first_container(context: dict, client: AsyncClient) -> dict[str, str]:
    container = context["containers"][0]
    await confirm_pickup(context, client)
    lab_auth = await authorize_lab_operator(context["shipment_id"], client)
    await receive_container(context, client, container, lab_auth)
    return lab_auth


async def item_id_for_container(container_id: str) -> str:
    async with get_session_factory()() as session:
        item_id = await session.scalar(
            select(ContainerItem.id).where(ContainerItem.container_id == container_id)
        )
    assert item_id is not None
    return str(item_id)


def receipt_item_url(context: dict, item_id: str) -> str:
    return f"/api/v1/shipments/{context['shipment_id']}/receiving/items/{item_id}"


async def patch_receipt(
    client: AsyncClient,
    context: dict,
    item_id: str,
    authorization: dict[str, str],
    payload: dict,
    key: str,
):
    return await client.patch(
        receipt_item_url(context, item_id),
        headers={**authorization, "Idempotency-Key": key},
        json=payload,
    )


def complete_receiving_url(context: dict) -> str:
    return f"/api/v1/shipments/{context['shipment_id']}/receiving/complete"


def reopen_receiving_url(context: dict) -> str:
    return f"/api/v1/shipments/{context['shipment_id']}/receiving/reopen"


async def complete_receiving(
    client: AsyncClient,
    context: dict,
    authorization: dict[str, str],
    key: str,
):
    return await client.post(
        complete_receiving_url(context),
        headers={**authorization, "Idempotency-Key": key},
        json={},
    )


async def complete_accepted_shipment(
    client: AsyncClient, context: dict, key_prefix: str
) -> tuple[dict[str, str], dict]:
    lab_auth = await receive_first_container(context, client)
    await record_result(
        client,
        context,
        lab_auth,
        context["containers"][0],
        {"result": "ACCEPTED"},
        f"{key_prefix}-result-{context['suffix']}",
    )
    completed = await complete_receiving(
        client,
        context,
        lab_auth,
        f"{key_prefix}-complete-{context['suffix']}",
    )
    assert completed.status_code == 200, completed.text
    assert completed.json()["status"] == "COMPLETED"
    return lab_auth, completed.json()


async def reopen_receiving(
    client: AsyncClient,
    context: dict,
    authorization: dict[str, str],
    key: str,
    reason: str = "Correct a receiving classification",
):
    return await client.post(
        reopen_receiving_url(context),
        headers={**authorization, "Idempotency-Key": key},
        json={"reason": reason},
    )


async def receive_all_containers(context: dict, client: AsyncClient) -> dict[str, str]:
    await confirm_pickup(context, client)
    lab_auth = await authorize_lab_operator(context["shipment_id"], client)
    for container in context["containers"]:
        await receive_container(context, client, container, lab_auth)
    return lab_auth


async def record_result(
    client: AsyncClient,
    context: dict,
    authorization: dict[str, str],
    container: dict,
    result: dict,
    key: str,
) -> str:
    item_id = await item_id_for_container(container["id"])
    response = await patch_receipt(client, context, item_id, authorization, result, key)
    assert response.status_code == 200, response.text
    return item_id


async def close_container_undelivered(
    context: dict,
    client: AsyncClient,
    container: dict,
    manager_auth: dict[str, str] | None = None,
) -> dict[str, str]:
    issue_context = {
        **context,
        "container_id": container["id"],
        "container_code": container["code"],
    }
    issue = await create_issue(issue_context, client)
    async with get_session_factory()() as session:
        shipment = await session.get(Shipment, context["shipment_id"])
        assert shipment is not None
        region_id = str(shipment.region_id)
    regional_auth = await authorize_regional_operator(region_id, client)
    if manager_auth is None:
        manager_auth = await authorize_lab_manager(context["shipment_id"], client)
    proposals_url = (
        f"/api/v1/shipments/{context['shipment_id']}/container-issues/{issue['id']}/proposals"
    )
    proposed = await client.post(
        proposals_url,
        headers={
            **regional_auth,
            "Idempotency-Key": f"receiving-close-proposal-{context['suffix']}",
        },
        json={"action": "CLOSE_UNDELIVERED", "reason": "Courier cannot return"},
    )
    assert proposed.status_code == 201, proposed.text
    decided = await client.post(
        f"{proposals_url}/{proposed.json()['id']}/decide",
        headers={
            **manager_auth,
            "Idempotency-Key": f"receiving-close-approve-{context['suffix']}",
        },
        json={"decision": "APPROVE"},
    )
    assert decided.status_code == 200, decided.text
    assert decided.json()["container"]["delivery_status"] == "UNDELIVERED_CLOSED"
    return manager_auth


async def create_wrong_scope_lab_manager(shipment_id: str, client: AsyncClient) -> dict[str, str]:
    suffix = uuid4().hex[:10]
    async with get_session_factory()() as session:
        shipment = await session.get(Shipment, shipment_id)
        assert shipment is not None
        other_lab = Organization(
            region_id=shipment.region_id,
            code=f"OTHER-LAB-{suffix}",
            name="Other laboratory",
            organization_type="LABORATORY",
        )
        manager = User(
            email=f"wrong-scope-manager-{suffix}@example.test",
            display_name="Wrong Scope Lab Manager",
            password_hash=hash_password(PASSWORD),
        )
        session.add_all([other_lab, manager])
        await session.flush()
        session.add(
            Membership(
                user_id=manager.id,
                organization_id=other_lab.id,
                role="LAB_MANAGER",
            )
        )
        await session.commit()
        email = manager.email
    login = await client.post("/api/v1/auth/token", json={"email": email, "password": PASSWORD})
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


@pytest.mark.asyncio
async def test_receipt_revision_schema_matches_the_migrated_database() -> None:
    expected_table = Base.metadata.tables.get("receipt_revisions")
    assert expected_table is not None
    async with get_session_factory()() as session:
        table_name = await session.scalar(
            text("SELECT to_regclass('public.receipt_revisions')::text")
        )
        assert table_name == "receipt_revisions"
        actual_columns = await session.run_sync(
            lambda sync_session: {
                column["name"]
                for column in inspect(sync_session.connection()).get_columns("receipt_revisions")
            }
        )
        assert actual_columns == set(expected_table.columns.keys())


@pytest.mark.asyncio
async def test_receipt_revision_rows_cannot_be_updated_or_deleted() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context = await create_pickup_fixture(client)
        await receive_first_container(context, client)

    async with get_session_factory()() as session:
        session_id = await session.scalar(
            text("SELECT id FROM receiving_sessions WHERE shipment_id=:shipment_id"),
            {"shipment_id": context["shipment_id"]},
        )
        item_id = await session.scalar(
            text("SELECT id FROM container_items WHERE container_id=:container_id"),
            {"container_id": context["container_id"]},
        )
        revision_id = uuid4()
        await session.execute(
            text(
                "INSERT INTO receipt_revisions "
                "(id, receiving_session_id, container_item_id, old_result, new_result, changed_by) "
                "VALUES (:id, :session_id, :item_id, NULL, 'ACCEPTED', :changed_by)"
            ),
            {
                "id": revision_id,
                "session_id": session_id,
                "item_id": item_id,
                "changed_by": context["sender_id"],
            },
        )
        await session.commit()

        with pytest.raises(DBAPIError, match="Receipt revisions are append-only"):
            await session.execute(
                text("UPDATE receipt_revisions SET new_comment='rewritten' WHERE id=:id"),
                {"id": revision_id},
            )
        await session.rollback()

        with pytest.raises(DBAPIError, match="Receipt revisions are append-only"):
            await session.execute(
                text("DELETE FROM receipt_revisions WHERE id=:id"), {"id": revision_id}
            )
        await session.rollback()


@pytest.mark.asyncio
async def test_result_write_requires_verified_received_container_and_open_session() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context = await create_pickup_fixture(client)
        await confirm_pickup(context, client)

        async with get_session_factory()() as session:
            item_id = await session.scalar(
                text("SELECT id FROM container_items WHERE container_id=:container_id"),
                {"container_id": context["container_id"]},
            )
            with pytest.raises(
                DBAPIError,
                match=RECEIPT_GUARD_ERROR,
            ):
                await session.execute(
                    text(
                        "UPDATE container_items SET receipt_result='ACCEPTED', "
                        "receipt_recorded_at=now() WHERE id=:item_id"
                    ),
                    {"item_id": item_id},
                )
            await session.rollback()

        await receive_first_container(context, client)

    async with get_session_factory()() as session:
        session_id = await session.scalar(
            text(
                "SELECT id FROM receiving_sessions WHERE shipment_id=:shipment_id AND status='OPEN'"
            ),
            {"shipment_id": context["shipment_id"]},
        )
        item_id = await session.scalar(
            text("SELECT id FROM container_items WHERE container_id=:container_id"),
            {"container_id": context["container_id"]},
        )
        await session.execute(
            text(
                "UPDATE receiving_sessions SET status='CLOSED', closed_by=:closed_by, "
                "closed_at=now() WHERE id=:session_id"
            ),
            {"closed_by": context["sender_id"], "session_id": session_id},
        )
        with pytest.raises(
            DBAPIError,
            match=RECEIPT_GUARD_ERROR,
        ):
            await session.execute(
                text(
                    "UPDATE container_items SET receipt_result='ACCEPTED', "
                    "receipt_recorded_at=now() WHERE id=:item_id"
                ),
                {"item_id": item_id},
            )
        await session.rollback()


@pytest.mark.asyncio
async def test_undelivered_items_cannot_receive_results() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context = await create_pickup_fixture(client, container_count=2)
        await confirm_pickup(context, client)
        issue = await create_issue(context, client)
        async with get_session_factory()() as session:
            region_id = await session.scalar(
                text("SELECT region_id FROM shipments WHERE id=:shipment_id"),
                {"shipment_id": context["shipment_id"]},
            )
        regional_auth = await authorize_regional_operator(str(region_id), client)
        manager_auth = await authorize_lab_manager(context["shipment_id"], client)
        proposals_url = (
            f"/api/v1/shipments/{context['shipment_id']}/container-issues/{issue['id']}/proposals"
        )
        proposal = await client.post(
            proposals_url,
            headers={
                **regional_auth,
                "Idempotency-Key": f"receiving-close-{context['suffix']}",
            },
            json={"action": "CLOSE_UNDELIVERED", "reason": "Courier cannot return"},
        )
        assert proposal.status_code == 201, proposal.text
        decided = await client.post(
            f"{proposals_url}/{proposal.json()['id']}/decide",
            headers={
                **manager_auth,
                "Idempotency-Key": f"receiving-close-approve-{context['suffix']}",
            },
            json={"decision": "APPROVE"},
        )
        assert decided.status_code == 200, decided.text
        assert decided.json()["container"]["delivery_status"] == "UNDELIVERED_CLOSED"

    async with get_session_factory()() as session:
        item_id = await session.scalar(
            text("SELECT id FROM container_items WHERE container_id=:container_id"),
            {"container_id": context["container_id"]},
        )
        with pytest.raises(
            DBAPIError,
            match=RECEIPT_GUARD_ERROR,
        ):
            await session.execute(
                text(
                    "UPDATE container_items SET receipt_result='ACCEPTED', "
                    "receipt_recorded_at=now() WHERE id=:item_id"
                ),
                {"item_id": item_id},
            )
        await session.rollback()


@pytest.mark.asyncio
async def test_record_receipt_writes_projection_revisions_and_audit_for_corrections() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context = await create_pickup_fixture(client)
        lab_auth = await receive_first_container(context, client)
        item_id = await item_id_for_container(context["container_id"])

        results = [
            ({"result": "ACCEPTED"}, "receipt-accepted-"),
            (
                {
                    "result": "MISSING",
                    "reason_code": "SAMPLE_NOT_FOUND",
                    "comment": "Tube was absent from the received container",
                },
                "receipt-missing-",
            ),
            (
                {
                    "result": "REJECTED",
                    "reason_code": "SAMPLE_LABEL_MISMATCH",
                    "comment": "Label does not match the manifest",
                },
                "receipt-rejected-",
            ),
        ]
        responses = []
        for payload, key_prefix in results:
            response = await patch_receipt(
                client,
                context,
                item_id,
                lab_auth,
                payload,
                f"{key_prefix}{context['suffix']}",
            )
            assert response.status_code == 200, response.text
            responses.append(response.json())

    assert [body["receipt_result"] for body in responses] == [
        "ACCEPTED",
        "MISSING",
        "REJECTED",
    ]
    assert responses[0]["receipt_reason_code"] is None
    assert responses[0]["receipt_comment"] is None
    assert responses[-1]["receipt_reason_code"] == "SAMPLE_LABEL_MISMATCH"
    assert responses[-1]["receipt_comment"] == "Label does not match the manifest"

    operation = f"shipments.{context['shipment_id']}.receiving.items.{item_id}.record"
    async with get_session_factory()() as session:
        item = await session.get(ContainerItem, item_id)
        assert item is not None
        assert item.receipt_result == "REJECTED"
        assert item.receipt_reason_code == "SAMPLE_LABEL_MISMATCH"
        assert item.receipt_comment == "Label does not match the manifest"
        assert item.receipt_recorded_at is not None

        revisions = list(
            await session.scalars(
                select(ReceiptRevision)
                .where(ReceiptRevision.container_item_id == item_id)
                .order_by(ReceiptRevision.changed_at, ReceiptRevision.id)
            )
        )
        assert [
            (
                revision.old_result,
                revision.new_result,
                revision.old_reason_code,
                revision.new_reason_code,
                revision.old_comment,
                revision.new_comment,
            )
            for revision in revisions
        ] == [
            (None, "ACCEPTED", None, None, None, None),
            (
                "ACCEPTED",
                "MISSING",
                None,
                "SAMPLE_NOT_FOUND",
                None,
                "Tube was absent from the received container",
            ),
            (
                "MISSING",
                "REJECTED",
                "SAMPLE_NOT_FOUND",
                "SAMPLE_LABEL_MISMATCH",
                "Tube was absent from the received container",
                "Label does not match the manifest",
            ),
        ]
        audits = list(
            await session.scalars(
                select(AuditEvent)
                .where(
                    AuditEvent.operation == operation,
                    AuditEvent.resource_id == item_id,
                )
                .order_by(AuditEvent.created_at, AuditEvent.request_key)
            )
        )
        assert len(audits) == 3
        assert len({event.request_key for event in audits}) == 3
        assert all(event.actor_user_id == revisions[0].changed_by for event in audits)


@pytest.mark.asyncio
async def test_record_receipt_rejects_invalid_result_shapes_and_reason_codes() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context = await create_pickup_fixture(client)
        lab_auth = await receive_first_container(context, client)
        item_id = await item_id_for_container(context["container_id"])
        invalid_requests = [
            ({"result": "MISSING"}, 422),
            ({"result": "ACCEPTED", "reason_code": "SAMPLE_NOT_FOUND"}, 422),
            ({"result": "ACCEPTED", "comment": "Accepted results have no comment"}, 422),
            ({"result": "REJECTED", "reason_code": "SAMPLE_NOT_FOUND"}, 409),
            ({"result": "MISSING", "reason_code": "NOT_A_SAMPLE_REASON"}, 409),
            (
                {
                    "result": "MISSING",
                    "reason_code": "SAMPLE_NOT_FOUND",
                    "comment": "x" * 501,
                },
                422,
            ),
        ]
        for index, (payload, expected_status) in enumerate(invalid_requests):
            response = await patch_receipt(
                client,
                context,
                item_id,
                lab_auth,
                payload,
                f"receipt-invalid-{context['suffix']}-{index}",
            )
            assert response.status_code == expected_status, response.text

    async with get_session_factory()() as session:
        item = await session.get(ContainerItem, item_id)
        assert item is not None and item.receipt_result is None
        assert (
            await session.scalar(
                select(func.count())
                .select_from(ReceiptRevision)
                .where(ReceiptRevision.container_item_id == item_id)
            )
            == 0
        )


@pytest.mark.asyncio
async def test_received_container_remains_eligible_while_sibling_is_unresolved() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context = await create_pickup_fixture(client, container_count=2)
        await confirm_pickup(context, client)
        lab_auth = await authorize_lab_operator(context["shipment_id"], client)
        first_receipt = await receive_container(context, client, context["containers"][0], lab_auth)
        assert first_receipt["shipment_status"] == "PARTIALLY_DELIVERED"
        first_item = await item_id_for_container(context["containers"][0]["id"])
        sibling_item = await item_id_for_container(context["containers"][1]["id"])

        accepted = await patch_receipt(
            client,
            context,
            first_item,
            lab_auth,
            {"result": "ACCEPTED"},
            f"receipt-partial-first-{context['suffix']}",
        )
        blocked = await patch_receipt(
            client,
            context,
            sibling_item,
            lab_auth,
            {"result": "ACCEPTED"},
            f"receipt-partial-sibling-{context['suffix']}",
        )

    assert accepted.status_code == 200, accepted.text
    assert blocked.status_code == 409, blocked.text


@pytest.mark.asyncio
async def test_record_receipt_rejects_undelivered_container_with_an_open_sibling_session() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context = await create_pickup_fixture(client, container_count=2)
        lab_auth = await receive_first_container(context, client)
        undelivered = context["containers"][1]
        issue_context = {
            **context,
            "container_id": undelivered["id"],
            "container_code": undelivered["code"],
        }
        issue = await create_issue(issue_context, client)
        async with get_session_factory()() as session:
            shipment = await session.get(Shipment, context["shipment_id"])
            assert shipment is not None
            region_id = shipment.region_id
        regional_auth = await authorize_regional_operator(str(region_id), client)
        manager_auth = await authorize_lab_manager(context["shipment_id"], client)
        proposals_url = (
            f"/api/v1/shipments/{context['shipment_id']}/container-issues/{issue['id']}/proposals"
        )
        proposed = await client.post(
            proposals_url,
            headers={
                **regional_auth,
                "Idempotency-Key": f"receipt-undelivered-proposal-{context['suffix']}",
            },
            json={"action": "CLOSE_UNDELIVERED", "reason": "Courier cannot return"},
        )
        assert proposed.status_code == 201, proposed.text
        decided = await client.post(
            f"{proposals_url}/{proposed.json()['id']}/decide",
            headers={
                **manager_auth,
                "Idempotency-Key": f"receipt-undelivered-approve-{context['suffix']}",
            },
            json={"decision": "APPROVE"},
        )
        assert decided.status_code == 200, decided.text
        assert decided.json()["container"]["delivery_status"] == "UNDELIVERED_CLOSED"
        item_id = await item_id_for_container(undelivered["id"])

        response = await patch_receipt(
            client,
            context,
            item_id,
            lab_auth,
            {"result": "ACCEPTED"},
            f"receipt-undelivered-{context['suffix']}",
        )

    assert response.status_code == 409, response.text
    async with get_session_factory()() as session:
        item = await session.get(ContainerItem, item_id)
        assert item is not None and item.receipt_result is None
        assert (
            await session.scalar(
                select(func.count())
                .select_from(ReceiptRevision)
                .where(ReceiptRevision.container_item_id == item_id)
            )
            == 0
        )


@pytest.mark.asyncio
async def test_record_receipt_rejects_unreceived_item_without_receipt_evidence() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context = await create_pickup_fixture(client)
        await confirm_pickup(context, client)
        lab_auth = await authorize_lab_operator(context["shipment_id"], client)
        item_id = await item_id_for_container(context["container_id"])
        response = await patch_receipt(
            client,
            context,
            item_id,
            lab_auth,
            {"result": "ACCEPTED"},
            f"receipt-unreceived-{context['suffix']}",
        )

    assert response.status_code == 409, response.text
    async with get_session_factory()() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(ReceiptRevision)
                .where(ReceiptRevision.container_item_id == item_id)
            )
            == 0
        )


@pytest.mark.asyncio
async def test_record_receipt_rejects_closed_session() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context = await create_pickup_fixture(client)
        lab_auth = await receive_first_container(context, client)
        item_id = await item_id_for_container(context["container_id"])

        owner_engine = create_async_engine(get_settings().migration_database_url)
        try:
            async with owner_engine.begin() as connection:
                await connection.execute(
                    text(
                        "ALTER TABLE receiving_sessions DISABLE TRIGGER "
                        "trg_validate_delivery_session_consistency"
                    )
                )
                await connection.execute(
                    text(
                        "UPDATE receiving_sessions SET status='CLOSED', closed_by=:closed_by, "
                        "closed_at=now() WHERE shipment_id=:shipment_id AND status='OPEN'"
                    ),
                    {
                        "closed_by": context["sender_id"],
                        "shipment_id": context["shipment_id"],
                    },
                )
                await connection.execute(
                    text(
                        "ALTER TABLE receiving_sessions ENABLE TRIGGER "
                        "trg_validate_delivery_session_consistency"
                    )
                )
        finally:
            await owner_engine.dispose()

        response = await patch_receipt(
            client,
            context,
            item_id,
            lab_auth,
            {"result": "ACCEPTED"},
            f"receipt-closed-session-{context['suffix']}",
        )

    assert response.status_code == 409, response.text


@pytest.mark.asyncio
async def test_record_receipt_rejects_wrong_scope_and_inactive_lab_membership() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context = await create_pickup_fixture(client)
        lab_auth = await receive_first_container(context, client)
        item_id = await item_id_for_container(context["container_id"])

        wrong_scope = await patch_receipt(
            client,
            context,
            item_id,
            context["sender_auth"],
            {"result": "ACCEPTED"},
            f"receipt-wrong-scope-{context['suffix']}",
        )
        async with get_session_factory()() as session:
            shipment = await session.get(Shipment, context["shipment_id"])
            assert shipment is not None
            membership = await session.scalar(
                select(Membership).where(
                    Membership.organization_id == shipment.destination_organization_id,
                    Membership.role == "LAB_OPERATOR",
                )
            )
            assert membership is not None
            membership.is_active = False
            await session.commit()

        inactive_membership = await patch_receipt(
            client,
            context,
            item_id,
            lab_auth,
            {"result": "ACCEPTED"},
            f"receipt-inactive-membership-{context['suffix']}",
        )

    assert wrong_scope.status_code == 404, wrong_scope.text
    assert inactive_membership.status_code == 404, inactive_membership.text
    async with get_session_factory()() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(ReceiptRevision)
                .where(ReceiptRevision.container_item_id == item_id)
            )
            == 0
        )


@pytest.mark.asyncio
async def test_record_receipt_exact_idempotent_replay_does_not_duplicate_history() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context = await create_pickup_fixture(client)
        lab_auth = await receive_first_container(context, client)
        item_id = await item_id_for_container(context["container_id"])
        payload = {
            "result": "MISSING",
            "reason_code": "SAMPLE_NOT_FOUND",
            "comment": "Tube is missing",
        }
        key = f"receipt-replay-{context['suffix']}"
        initial = await patch_receipt(client, context, item_id, lab_auth, payload, key)
        replay = await patch_receipt(client, context, item_id, lab_auth, payload, key)
        conflicting = await patch_receipt(
            client,
            context,
            item_id,
            lab_auth,
            {"result": "ACCEPTED"},
            key,
        )
        async with get_session_factory()() as session:
            shipment = await session.get(Shipment, context["shipment_id"])
            assert shipment is not None
            membership = await session.scalar(
                select(Membership).where(
                    Membership.organization_id == shipment.destination_organization_id,
                    Membership.role == "LAB_OPERATOR",
                )
            )
            assert membership is not None
            membership.is_active = False
            await session.commit()
        replay_after_revocation = await patch_receipt(
            client, context, item_id, lab_auth, payload, key
        )

    assert initial.status_code == replay.status_code == 200
    assert replay.json() == initial.json()
    assert conflicting.status_code == 409, conflicting.text
    assert replay_after_revocation.status_code == 404, replay_after_revocation.text
    async with get_session_factory()() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(ReceiptRevision)
                .where(ReceiptRevision.container_item_id == item_id)
            )
            == 1
        )
        operation = f"shipments.{context['shipment_id']}.receiving.items.{item_id}.record"
        assert (
            await session.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(
                    AuditEvent.operation == operation,
                    AuditEvent.resource_id == item_id,
                    AuditEvent.request_key == key,
                )
            )
            == 1
        )


@pytest.mark.asyncio
async def test_audit_failure_rolls_back_receipt_projection_revision_and_idempotency() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context = await create_pickup_fixture(client)
        lab_auth = await receive_first_container(context, client)
        item_id = await item_id_for_container(context["container_id"])

    operation = f"shipments.{context['shipment_id']}.receiving.items.{item_id}.record"
    key = f"receipt-audit-fail-{context['suffix']}"
    owner_engine = create_async_engine(get_settings().migration_database_url)
    try:
        async with owner_engine.begin() as connection:
            await connection.execute(
                text("""
                CREATE FUNCTION reject_receipt_record_audit() RETURNS trigger AS $$
                BEGIN
                  IF NEW.operation LIKE '%.receiving.items.%.record' THEN
                    RAISE EXCEPTION 'Injected receipt audit failure';
                  END IF;
                  RETURN NEW;
                END; $$ LANGUAGE plpgsql
                """)
            )
            await connection.execute(
                text("""
                CREATE TRIGGER trg_test_reject_receipt_record_audit
                BEFORE INSERT ON audit_events FOR EACH ROW
                EXECUTE FUNCTION reject_receipt_record_audit()
                """)
            )

        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as client:
            response = await patch_receipt(
                client,
                context,
                item_id,
                lab_auth,
                {"result": "ACCEPTED"},
                key,
            )
        assert response.status_code == 500
        assert response.json()["error"]["code"] == "INTERNAL_ERROR"
    finally:
        async with owner_engine.begin() as connection:
            await connection.execute(
                text("DROP TRIGGER IF EXISTS trg_test_reject_receipt_record_audit ON audit_events")
            )
            await connection.execute(text("DROP FUNCTION IF EXISTS reject_receipt_record_audit()"))
        await owner_engine.dispose()

    async with get_session_factory()() as session:
        item = await session.get(ContainerItem, item_id)
        assert item is not None and item.receipt_result is None
        assert (
            await session.scalar(
                select(func.count())
                .select_from(ReceiptRevision)
                .where(ReceiptRevision.container_item_id == item_id)
            )
            == 0
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(IdempotencyRecord)
                .where(
                    IdempotencyRecord.operation == operation,
                    IdempotencyRecord.key == key,
                )
            )
            == 0
        )


@pytest.mark.asyncio
async def test_receiving_history_is_ordered_paginated_and_scoped_to_sender_and_lab() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context = await create_pickup_fixture(client)
        lab_auth = await receive_first_container(context, client)
        item_id = await item_id_for_container(context["container_id"])
        for index, payload in enumerate(
            [
                {"result": "ACCEPTED"},
                {"result": "MISSING", "reason_code": "SAMPLE_NOT_FOUND"},
                {"result": "REJECTED", "reason_code": "SAMPLE_LABEL_MISMATCH"},
            ]
        ):
            response = await patch_receipt(
                client,
                context,
                item_id,
                lab_auth,
                payload,
                f"receipt-history-{context['suffix']}-{index}",
            )
            assert response.status_code == 200, response.text

        history_url = f"/api/v1/shipments/{context['shipment_id']}/receiving/history"
        lab_page = await client.get(history_url, headers=lab_auth, params={"limit": 2})
        sender_page = await client.get(
            history_url, headers=context["sender_auth"], params={"limit": 2}
        )
        next_page = await client.get(
            history_url, headers=lab_auth, params={"limit": 2, "offset": 2}
        )
        manager_auth = await authorize_lab_manager(context["shipment_id"], client)
        manager_page = await client.get(history_url, headers=manager_auth)
        courier_page = await client.get(history_url, headers=context["courier_auth"])

        async with get_session_factory()() as session:
            shipment = await session.get(Shipment, context["shipment_id"])
            assert shipment is not None
            region_id = shipment.region_id
        regional_auth = await authorize_regional_operator(str(region_id), client)
        regional_page = await client.get(history_url, headers=regional_auth)

    assert lab_page.status_code == sender_page.status_code == manager_page.status_code == 200
    assert lab_page.json() == sender_page.json()
    assert manager_page.json()["total_revisions"] == 3
    assert lab_page.json()["total_revisions"] == 3
    assert lab_page.json()["limit"] == 2
    assert lab_page.json()["offset"] == 0
    assert [row["new_result"] for row in lab_page.json()["revisions"]] == [
        "REJECTED",
        "MISSING",
    ]
    assert [row["new_result"] for row in next_page.json()["revisions"]] == ["ACCEPTED"]
    assert [row["sequence"] for row in lab_page.json()["sessions"]] == [1]
    assert courier_page.status_code == regional_page.status_code == 404


@pytest.mark.asyncio
async def test_all_accepted_results_complete_and_replay_atomically() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context = await create_pickup_fixture(client, container_count=2)
        lab_auth = await receive_all_containers(context, client)
        for index, container in enumerate(context["containers"]):
            await record_result(
                client,
                context,
                lab_auth,
                container,
                {"result": "ACCEPTED"},
                f"complete-accepted-result-{context['suffix']}-{index}",
            )

        async with get_session_factory()() as session:
            shipment_before = await session.get(Shipment, context["shipment_id"])
            assert shipment_before is not None
            version_before = shipment_before.version
            container_states_before = list(
                await session.execute(
                    select(
                        TransportContainer.id,
                        TransportContainer.delivery_status,
                        TransportContainer.state_version,
                        TransportContainer.received_at,
                        TransportContainer.received_by,
                        TransportContainer.receipt_evidence,
                    ).where(TransportContainer.shipment_id == context["shipment_id"])
                )
            )

        key = f"receiving-complete-accepted-{context['suffix']}"
        completed = await complete_receiving(client, context, lab_auth, key)
        replay = await complete_receiving(client, context, lab_auth, key)

    assert completed.status_code == replay.status_code == 200
    assert replay.json() == completed.json()
    assert completed.json()["status"] == "COMPLETED"
    assert completed.json()["version"] == version_before + 1
    operation = f"shipments.{context['shipment_id']}.receiving.complete"
    async with get_session_factory()() as session:
        shipment = await session.get(Shipment, context["shipment_id"])
        assert shipment is not None and shipment.status == "COMPLETED"
        receiving_sessions = list(
            await session.scalars(
                select(ReceivingSessionModel).where(
                    ReceivingSessionModel.shipment_id == context["shipment_id"]
                )
            )
        )
        assert len(receiving_sessions) == 1
        assert receiving_sessions[0].status == "CLOSED"
        container_states_after = list(
            await session.execute(
                select(
                    TransportContainer.id,
                    TransportContainer.delivery_status,
                    TransportContainer.state_version,
                    TransportContainer.received_at,
                    TransportContainer.received_by,
                    TransportContainer.receipt_evidence,
                ).where(TransportContainer.shipment_id == context["shipment_id"])
            )
        )
        assert container_states_after == container_states_before
        assert (
            await session.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(
                    AuditEvent.operation == operation,
                    AuditEvent.resource_id == context["shipment_id"],
                    AuditEvent.request_key == key,
                )
            )
            == 1
        )
        record = await session.scalar(
            select(IdempotencyRecord).where(
                IdempotencyRecord.operation == operation,
                IdempotencyRecord.key == key,
            )
        )
        assert record is not None and record.response_status == 200
        assert record.response_body["id"] == completed.json()["id"]
        assert record.response_body["status"] == completed.json()["status"]
        assert record.response_body["version"] == completed.json()["version"]


@pytest.mark.asyncio
async def test_mixed_results_complete_with_issues() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context = await create_pickup_fixture(client, container_count=2)
        lab_auth = await receive_all_containers(context, client)
        await record_result(
            client,
            context,
            lab_auth,
            context["containers"][0],
            {"result": "ACCEPTED"},
            f"complete-mixed-accepted-{context['suffix']}",
        )
        await record_result(
            client,
            context,
            lab_auth,
            context["containers"][1],
            {
                "result": "MISSING",
                "reason_code": "SAMPLE_NOT_FOUND",
                "comment": "Sample was not present",
            },
            f"complete-mixed-missing-{context['suffix']}",
        )
        completed = await complete_receiving(
            client,
            context,
            lab_auth,
            f"receiving-complete-mixed-{context['suffix']}",
        )

    assert completed.status_code == 200, completed.text
    assert completed.json()["status"] == "COMPLETED_WITH_ISSUES"
    async with get_session_factory()() as session:
        receiving_session = await session.scalar(
            select(ReceivingSessionModel).where(
                ReceivingSessionModel.shipment_id == context["shipment_id"]
            )
        )
        assert receiving_session is not None and receiving_session.status == "CLOSED"


@pytest.mark.asyncio
async def test_received_plus_undelivered_completes_with_null_results() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context = await create_pickup_fixture(client, container_count=2)
        await confirm_pickup(context, client)
        lab_auth = await authorize_lab_operator(context["shipment_id"], client)
        received_container = context["containers"][0]
        undelivered_container = context["containers"][1]
        await receive_container(context, client, received_container, lab_auth)
        await close_container_undelivered(context, client, undelivered_container)
        await record_result(
            client,
            context,
            lab_auth,
            received_container,
            {"result": "ACCEPTED"},
            f"complete-partial-result-{context['suffix']}",
        )
        completed = await complete_receiving(
            client,
            context,
            lab_auth,
            f"receiving-complete-partial-{context['suffix']}",
        )

    assert completed.status_code == 200, completed.text
    assert completed.json()["status"] == "COMPLETED_WITH_ISSUES"
    async with get_session_factory()() as session:
        containers = list(
            await session.scalars(
                select(TransportContainer).where(
                    TransportContainer.shipment_id == context["shipment_id"]
                )
            )
        )
        undelivered = next(
            container
            for container in containers
            if str(container.id) == undelivered_container["id"]
        )
        assert undelivered.delivery_status == "UNDELIVERED_CLOSED"
        outcomes = list(
            await session.scalars(
                select(ContainerItem).where(ContainerItem.container_id == undelivered.id)
            )
        )
        assert len(outcomes) == 1
        assert outcomes[0].receipt_result is None
        assert outcomes[0].receipt_recorded_at is None
        assert outcomes[0].receipt_reason_code is None
        assert outcomes[0].receipt_comment is None


@pytest.mark.asyncio
async def test_completion_rejects_unresolved_container_without_partial_effects() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context = await create_pickup_fixture(client, container_count=2)
        await confirm_pickup(context, client)
        lab_auth = await authorize_lab_operator(context["shipment_id"], client)
        await receive_container(context, client, context["containers"][0], lab_auth)
        await record_result(
            client,
            context,
            lab_auth,
            context["containers"][0],
            {"result": "ACCEPTED"},
            f"complete-unresolved-result-{context['suffix']}",
        )
        response = await complete_receiving(
            client,
            context,
            lab_auth,
            f"receiving-complete-unresolved-{context['suffix']}",
        )

    assert response.status_code == 409, response.text
    operation = f"shipments.{context['shipment_id']}.receiving.complete"
    async with get_session_factory()() as session:
        shipment = await session.get(Shipment, context["shipment_id"])
        assert shipment is not None and shipment.status == "PARTIALLY_DELIVERED"
        receiving_session = await session.scalar(
            select(ReceivingSessionModel).where(
                ReceivingSessionModel.shipment_id == context["shipment_id"]
            )
        )
        assert receiving_session is not None and receiving_session.status == "OPEN"
        assert (
            await session.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.operation == operation)
            )
            == 0
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(IdempotencyRecord)
                .where(IdempotencyRecord.operation == operation)
            )
            == 0
        )


@pytest.mark.asyncio
async def test_completion_rejects_missing_received_item_result_without_partial_effects() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context = await create_pickup_fixture(client)
        lab_auth = await receive_first_container(context, client)
        response = await complete_receiving(
            client,
            context,
            lab_auth,
            f"receiving-complete-missing-result-{context['suffix']}",
        )

    assert response.status_code == 409, response.text
    operation = f"shipments.{context['shipment_id']}.receiving.complete"
    async with get_session_factory()() as session:
        shipment = await session.get(Shipment, context["shipment_id"])
        assert shipment is not None and shipment.status == "RECEIVING"
        receiving_session = await session.scalar(
            select(ReceivingSessionModel).where(
                ReceivingSessionModel.shipment_id == context["shipment_id"]
            )
        )
        assert receiving_session is not None and receiving_session.status == "OPEN"
        item = await session.scalar(
            select(ContainerItem).where(ContainerItem.container_id == context["container_id"])
        )
        assert item is not None and item.receipt_result is None
        assert (
            await session.scalar(
                select(func.count())
                .select_from(IdempotencyRecord)
                .where(IdempotencyRecord.operation == operation)
            )
            == 0
        )


@pytest.mark.asyncio
async def test_all_undelivered_shipment_stays_closed_and_cannot_complete_or_reopen() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context = await create_pickup_fixture(client)
        await confirm_pickup(context, client)
        manager_auth = await close_container_undelivered(context, client, context["containers"][0])
        lab_auth = await authorize_lab_operator(context["shipment_id"], client)
        complete = await complete_receiving(
            client,
            context,
            lab_auth,
            f"receiving-complete-undelivered-{context['suffix']}",
        )
        reopen = await reopen_receiving(
            client,
            context,
            manager_auth,
            f"receiving-reopen-undelivered-{context['suffix']}",
        )

    assert complete.status_code == reopen.status_code == 409
    async with get_session_factory()() as session:
        shipment = await session.get(Shipment, context["shipment_id"])
        assert shipment is not None and shipment.status == "CLOSED_UNDELIVERED"
        assert (
            await session.scalar(
                select(func.count())
                .select_from(ReceivingSessionModel)
                .where(ReceivingSessionModel.shipment_id == context["shipment_id"])
            )
            == 0
        )


@pytest.mark.asyncio
async def test_manager_reopen_preserves_history_and_custody_then_recalculates_completion() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context = await create_pickup_fixture(client)
        lab_auth = await receive_first_container(context, client)
        item_id = await record_result(
            client,
            context,
            lab_auth,
            context["containers"][0],
            {
                "result": "MISSING",
                "reason_code": "SAMPLE_NOT_FOUND",
                "comment": "Sample was not present",
            },
            f"reopen-original-result-{context['suffix']}",
        )
        initial_completion = await complete_receiving(
            client,
            context,
            lab_auth,
            f"receiving-initial-complete-{context['suffix']}",
        )
        assert initial_completion.status_code == 200, initial_completion.text
        assert initial_completion.json()["status"] == "COMPLETED_WITH_ISSUES"
        manager_auth = await authorize_lab_manager(context["shipment_id"], client)
        reopened = await reopen_receiving(
            client,
            context,
            manager_auth,
            f"receiving-reopen-valid-{context['suffix']}",
            reason="  Recheck approved sample details  ",
        )
        assert reopened.status_code == 200, reopened.text
        assert reopened.json()["status"] == "RECEIVING"

        async with get_session_factory()() as session:
            item = await session.get(ContainerItem, item_id)
            receiving_sessions = list(
                await session.scalars(
                    select(ReceivingSessionModel)
                    .where(ReceivingSessionModel.shipment_id == context["shipment_id"])
                    .order_by(ReceivingSessionModel.sequence)
                )
            )
            revisions = list(
                await session.scalars(
                    select(ReceiptRevision).where(ReceiptRevision.container_item_id == item_id)
                )
            )
            container = await session.get(TransportContainer, context["container_id"])
            assert item is not None and item.receipt_result == "MISSING"
            assert len(receiving_sessions) == 2
            assert [row.sequence for row in receiving_sessions] == [1, 2]
            assert [row.status for row in receiving_sessions] == ["CLOSED", "OPEN"]
            assert receiving_sessions[1].reopen_reason == "Recheck approved sample details"
            assert len(revisions) == 1
            assert revisions[0].receiving_session_id == receiving_sessions[0].id
            assert container is not None and container.delivery_status == "RECEIVED"
            container_state_version = container.state_version
            received_at = container.received_at
            received_by = container.received_by
            receipt_evidence = container.receipt_evidence

        correction = await patch_receipt(
            client,
            context,
            item_id,
            lab_auth,
            {"result": "ACCEPTED"},
            f"reopen-correct-result-{context['suffix']}",
        )
        assert correction.status_code == 200, correction.text
        final_completion = await complete_receiving(
            client,
            context,
            lab_auth,
            f"receiving-recomplete-{context['suffix']}",
        )
        assert final_completion.status_code == 200, final_completion.text
        assert final_completion.json()["status"] == "COMPLETED"

    async with get_session_factory()() as session:
        item = await session.get(ContainerItem, item_id)
        receiving_sessions = list(
            await session.scalars(
                select(ReceivingSessionModel)
                .where(ReceivingSessionModel.shipment_id == context["shipment_id"])
                .order_by(ReceivingSessionModel.sequence)
            )
        )
        revisions = list(
            await session.scalars(
                select(ReceiptRevision).where(ReceiptRevision.container_item_id == item_id)
            )
        )
        container = await session.get(TransportContainer, context["container_id"])
        assert item is not None and item.receipt_result == "ACCEPTED"
        assert len(receiving_sessions) == 2
        assert [row.sequence for row in receiving_sessions] == [1, 2]
        assert [row.status for row in receiving_sessions] == ["CLOSED", "CLOSED"]
        assert len(revisions) == 2
        assert revisions[1].receiving_session_id == receiving_sessions[1].id
        assert container is not None and container.delivery_status == "RECEIVED"
        assert container.state_version == container_state_version
        assert container.received_at == received_at
        assert container.received_by == received_by
        assert container.receipt_evidence == receipt_evidence

    async with get_session_factory()() as session:
        receiving_sessions = list(
            await session.scalars(
                select(ReceivingSessionModel)
                .where(ReceivingSessionModel.shipment_id == context["shipment_id"])
                .order_by(ReceivingSessionModel.sequence)
            )
        )
        revisions = list(
            await session.scalars(
                select(ReceiptRevision).where(ReceiptRevision.container_item_id == item_id)
            )
        )
        container = await session.get(TransportContainer, context["container_id"])
        assert [row.status for row in receiving_sessions] == ["CLOSED", "CLOSED"]
        assert len(revisions) == 2
        assert revisions[1].receiving_session_id == receiving_sessions[1].id
        assert container is not None and container.delivery_status == "RECEIVED"
        assert container.state_version == container_state_version
        assert container.received_at == received_at
        assert container.received_by == received_by
        assert container.receipt_evidence == receipt_evidence


@pytest.mark.asyncio
async def test_reopen_requires_active_destination_lab_manager_and_reason() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context = await create_pickup_fixture(client)
        lab_auth = await receive_first_container(context, client)
        await record_result(
            client,
            context,
            lab_auth,
            context["containers"][0],
            {"result": "ACCEPTED"},
            f"reopen-auth-result-{context['suffix']}",
        )
        completed = await complete_receiving(
            client,
            context,
            lab_auth,
            f"receiving-reopen-auth-complete-{context['suffix']}",
        )
        assert completed.status_code == 200, completed.text
        manager_auth = await authorize_lab_manager(context["shipment_id"], client)
        unauthorized = await reopen_receiving(
            client,
            context,
            context["sender_auth"],
            f"receiving-reopen-unauthorized-{context['suffix']}",
        )
        wrong_scope_auth = await create_wrong_scope_lab_manager(context["shipment_id"], client)
        wrong_scope = await reopen_receiving(
            client,
            context,
            wrong_scope_auth,
            f"receiving-reopen-wrong-scope-{context['suffix']}",
        )
        missing_reason = await client.post(
            reopen_receiving_url(context),
            headers={
                **manager_auth,
                "Idempotency-Key": f"receiving-reopen-missing-reason-{context['suffix']}",
            },
            json={},
        )
        async with get_session_factory()() as session:
            shipment = await session.get(Shipment, context["shipment_id"])
            assert shipment is not None
            membership = await session.scalar(
                select(Membership).where(
                    Membership.organization_id == shipment.destination_organization_id,
                    Membership.role == "LAB_MANAGER",
                )
            )
            assert membership is not None
            membership.is_active = False
            await session.commit()
        inactive_membership = await reopen_receiving(
            client,
            context,
            manager_auth,
            f"receiving-reopen-inactive-{context['suffix']}",
        )

    assert unauthorized.status_code == wrong_scope.status_code == 404
    assert missing_reason.status_code == 422
    assert inactive_membership.status_code == 404
    async with get_session_factory()() as session:
        shipment = await session.get(Shipment, context["shipment_id"])
        assert shipment is not None and shipment.status == "COMPLETED"
        assert (
            await session.scalar(
                select(func.count())
                .select_from(ReceivingSessionModel)
                .where(ReceivingSessionModel.shipment_id == context["shipment_id"])
            )
            == 1
        )


@pytest.mark.asyncio
async def test_concurrent_completion_serializes_on_shipment_and_only_one_transition_wins() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context = await create_pickup_fixture(client)
        lab_auth = await receive_first_container(context, client)
        await record_result(
            client,
            context,
            lab_auth,
            context["containers"][0],
            {"result": "ACCEPTED"},
            f"complete-concurrent-result-{context['suffix']}",
        )
        first, second = await asyncio.gather(
            complete_receiving(
                client,
                context,
                lab_auth,
                f"receiving-complete-concurrent-a-{context['suffix']}",
            ),
            complete_receiving(
                client,
                context,
                lab_auth,
                f"receiving-complete-concurrent-b-{context['suffix']}",
            ),
        )

    assert sorted([first.status_code, second.status_code]) == [200, 409]
    operation = f"shipments.{context['shipment_id']}.receiving.complete"
    async with get_session_factory()() as session:
        shipment = await session.get(Shipment, context["shipment_id"])
        assert shipment is not None and shipment.status == "COMPLETED"
        assert (
            await session.scalar(
                select(func.count())
                .select_from(ReceivingSessionModel)
                .where(
                    ReceivingSessionModel.shipment_id == context["shipment_id"],
                    ReceivingSessionModel.status == "CLOSED",
                )
            )
            == 1
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.operation == operation)
            )
            == 1
        )


@pytest.mark.asyncio
async def test_completion_refreshes_shipment_version_after_waiting_for_row_lock() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context = await create_pickup_fixture(client)
        lab_auth = await receive_first_container(context, client)
        await record_result(
            client,
            context,
            lab_auth,
            context["containers"][0],
            {"result": "ACCEPTED"},
            f"complete-stale-version-result-{context['suffix']}",
        )

        async with get_session_factory()() as session:
            version_before = await session.scalar(
                select(Shipment.version).where(Shipment.id == context["shipment_id"])
            )
        assert version_before is not None

        owner_engine = create_async_engine(get_settings().migration_database_url)
        completion_task: asyncio.Task | None = None
        try:
            async with owner_engine.connect() as connection:
                transaction = await connection.begin()
                await connection.execute(
                    text("SELECT id FROM shipments WHERE id=:shipment_id FOR UPDATE"),
                    {"shipment_id": context["shipment_id"]},
                )
                completion_task = asyncio.create_task(
                    complete_receiving(
                        client,
                        context,
                        lab_auth,
                        f"complete-stale-version-finish-{context['suffix']}",
                    )
                )

                pg_activity = []
                for _ in range(250):
                    waiting_for_shipment = await connection.scalar(
                        text("""
                        SELECT EXISTS (
                          SELECT 1 FROM pg_stat_activity
                          WHERE datname=current_database()
                            AND wait_event_type='Lock'
                            AND query NOT ILIKE '%pg_stat_activity%'
                        )
                        """)
                    )
                    if waiting_for_shipment:
                        break
                    pg_activity = list(
                        await connection.execute(
                            text("""
                            SELECT pid, wait_event_type, wait_event, state, query
                            FROM pg_stat_activity
                            WHERE datname=current_database() AND pid <> pg_backend_pid()
                            """)
                        )
                    )
                    await asyncio.sleep(0.02)
                assert waiting_for_shipment, (
                    f"Completion did not wait on the Shipment row lock: {pg_activity}"
                )

                await connection.execute(
                    text("UPDATE shipments SET version=version+1 WHERE id=:shipment_id"),
                    {"shipment_id": context["shipment_id"]},
                )
                await transaction.commit()

            response = await asyncio.wait_for(completion_task, timeout=10)
        finally:
            if completion_task is not None and not completion_task.done():
                completion_task.cancel()
                await asyncio.gather(completion_task, return_exceptions=True)
            await owner_engine.dispose()

    assert response.status_code == 200, response.text
    assert response.json()["version"] == version_before + 2
    async with get_session_factory()() as session:
        stored_version = await session.scalar(
            select(Shipment.version).where(Shipment.id == context["shipment_id"])
        )
    assert stored_version == version_before + 2


@pytest.mark.asyncio
async def test_concurrent_reopen_creates_only_one_next_session() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context = await create_pickup_fixture(client)
        lab_auth = await receive_first_container(context, client)
        await record_result(
            client,
            context,
            lab_auth,
            context["containers"][0],
            {"result": "ACCEPTED"},
            f"reopen-concurrent-result-{context['suffix']}",
        )
        completed = await complete_receiving(
            client,
            context,
            lab_auth,
            f"receiving-reopen-concurrent-complete-{context['suffix']}",
        )
        assert completed.status_code == 200, completed.text
        manager_auth = await authorize_lab_manager(context["shipment_id"], client)
        first, second = await asyncio.gather(
            reopen_receiving(
                client,
                context,
                manager_auth,
                f"receiving-reopen-concurrent-a-{context['suffix']}",
                reason="First concurrent reopen",
            ),
            reopen_receiving(
                client,
                context,
                manager_auth,
                f"receiving-reopen-concurrent-b-{context['suffix']}",
                reason="Second concurrent reopen",
            ),
        )

    assert sorted([first.status_code, second.status_code]) == [200, 409]
    operation = f"shipments.{context['shipment_id']}.receiving.reopen"
    async with get_session_factory()() as session:
        shipment = await session.get(Shipment, context["shipment_id"])
        assert shipment is not None and shipment.status == "RECEIVING"
        receiving_sessions = list(
            await session.scalars(
                select(ReceivingSessionModel)
                .where(ReceivingSessionModel.shipment_id == context["shipment_id"])
                .order_by(ReceivingSessionModel.sequence)
            )
        )
        assert [row.sequence for row in receiving_sessions] == [1, 2]
        assert [row.status for row in receiving_sessions] == ["CLOSED", "OPEN"]
        assert (
            await session.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.operation == operation)
            )
            == 1
        )


@pytest.mark.asyncio
async def test_completion_audit_failure_rolls_back_session_shipment_and_idempotency(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context = await create_pickup_fixture(client)
        lab_auth = await receive_first_container(context, client)
        await record_result(
            client,
            context,
            lab_auth,
            context["containers"][0],
            {"result": "ACCEPTED"},
            f"complete-audit-result-{context['suffix']}",
        )

    operation = f"shipments.{context['shipment_id']}.receiving.complete"
    key = f"receiving-complete-audit-failure-{context['suffix']}"
    owner_engine = create_async_engine(get_settings().migration_database_url)
    try:
        async with owner_engine.begin() as connection:
            audit_trigger_sql = f"""
                CREATE FUNCTION reject_receiving_complete_audit() RETURNS trigger AS $$
                BEGIN
                  IF NEW.operation = '{operation}' THEN
                    RAISE EXCEPTION 'Injected receiving completion audit failure';
                  END IF;
                  RETURN NEW;
                END; $$ LANGUAGE plpgsql
                """
            await connection.execute(text(audit_trigger_sql))
            await connection.execute(
                text("""
                CREATE TRIGGER trg_test_reject_receiving_complete_audit
                BEFORE INSERT ON audit_events FOR EACH ROW
                EXECUTE FUNCTION reject_receiving_complete_audit()
                """)
            )

        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as client:
            response = await complete_receiving(client, context, lab_auth, key)
        assert response.status_code == 500
        assert response.json()["error"]["code"] == "INTERNAL_ERROR"
        error_record = next(
            record
            for record in caplog.records
            if record.name == "app.main" and record.msg == "unhandled_api_exception"
        )
        error_payload = json.loads(JsonFormatter().format(error_record))
        assert error_payload["message"] == "unhandled_api_exception"
        assert error_payload["error_type"] == "DBAPIError"
        assert "Injected receiving completion audit failure" not in caplog.text
        assert "Injected receiving completion audit failure" not in json.dumps(error_payload)
    finally:
        async with owner_engine.begin() as connection:
            await connection.execute(
                text(
                    "DROP TRIGGER IF EXISTS trg_test_reject_receiving_complete_audit "
                    "ON audit_events"
                )
            )
            await connection.execute(
                text("DROP FUNCTION IF EXISTS reject_receiving_complete_audit()")
            )
        await owner_engine.dispose()

    async with get_session_factory()() as session:
        shipment = await session.get(Shipment, context["shipment_id"])
        assert shipment is not None and shipment.status == "RECEIVING"
        receiving_session = await session.scalar(
            select(ReceivingSessionModel).where(
                ReceivingSessionModel.shipment_id == context["shipment_id"]
            )
        )
        assert receiving_session is not None and receiving_session.status == "OPEN"
        assert (
            await session.scalar(
                select(func.count())
                .select_from(IdempotencyRecord)
                .where(
                    IdempotencyRecord.operation == operation,
                    IdempotencyRecord.key == key,
                )
            )
            == 0
        )


@pytest.mark.asyncio
async def test_database_allows_valid_completion_and_reopen_transitions() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context = await create_pickup_fixture(client)
        lab_auth = await receive_first_container(context, client)
        await record_result(
            client,
            context,
            lab_auth,
            context["containers"][0],
            {"result": "ACCEPTED"},
            f"direct-lifecycle-accepted-{context['suffix']}",
        )
        manager_auth = await authorize_lab_manager(context["shipment_id"], client)

    async with get_session_factory()() as session:
        manager_id = await session.scalar(
            select(Membership.user_id)
            .join(Organization, Organization.id == Membership.organization_id)
            .join(Shipment, Shipment.destination_organization_id == Organization.id)
            .where(
                Shipment.id == context["shipment_id"],
                Membership.role == "LAB_MANAGER",
                Membership.is_active.is_(True),
            )
        )
        assert manager_id is not None
        await session.execute(
            text(
                "UPDATE receiving_sessions SET status='CLOSED', closed_by=:actor, "
                "closed_at=clock_timestamp() WHERE shipment_id=:shipment_id AND status='OPEN'"
            ),
            {"actor": context["sender_id"], "shipment_id": context["shipment_id"]},
        )
        await session.execute(
            text(
                "UPDATE shipments SET status='COMPLETED', version=version+1 WHERE id=:shipment_id"
            ),
            {"shipment_id": context["shipment_id"]},
        )
        await session.commit()

        await session.execute(
            text(
                "INSERT INTO receiving_sessions "
                "(id, shipment_id, sequence, status, opened_by, opened_at, reopen_reason) "
                "VALUES (:id, :shipment_id, 2, 'OPEN', :actor, clock_timestamp(), :reason)"
            ),
            {
                "id": uuid4(),
                "shipment_id": context["shipment_id"],
                "actor": manager_id,
                "reason": "Direct SQL reopen acceptance",
            },
        )
        await session.execute(
            text(
                "UPDATE shipments SET status='RECEIVING', version=version+1 WHERE id=:shipment_id"
            ),
            {"shipment_id": context["shipment_id"]},
        )
        await session.commit()

        shipment = await session.get(Shipment, context["shipment_id"])
        receiving_sessions = list(
            await session.scalars(
                select(ReceivingSessionModel)
                .where(ReceivingSessionModel.shipment_id == context["shipment_id"])
                .order_by(ReceivingSessionModel.sequence)
            )
        )

    assert shipment is not None and shipment.status == "RECEIVING"
    assert [row.sequence for row in receiving_sessions] == [1, 2]
    assert [row.status for row in receiving_sessions] == ["CLOSED", "OPEN"]
    assert receiving_sessions[1].reopen_reason == "Direct SQL reopen acceptance"
    assert manager_auth


@pytest.mark.asyncio
async def test_database_rejects_completion_while_receiving_session_is_open() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context = await create_pickup_fixture(client)
        lab_auth = await receive_first_container(context, client)
        await record_result(
            client,
            context,
            lab_auth,
            context["containers"][0],
            {"result": "ACCEPTED"},
            f"direct-open-session-result-{context['suffix']}",
        )

    async with get_session_factory()() as session:
        with pytest.raises(DBAPIError, match="Completion requires no OPEN receiving session"):
            await session.execute(
                text("UPDATE shipments SET status='COMPLETED' WHERE id=:shipment_id"),
                {"shipment_id": context["shipment_id"]},
            )
        await session.rollback()


@pytest.mark.asyncio
async def test_database_rejects_completion_with_missing_received_item_result() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context = await create_pickup_fixture(client)
        await receive_first_container(context, client)

    async with get_session_factory()() as session:
        await session.execute(
            text(
                "UPDATE receiving_sessions SET status='CLOSED', closed_by=:actor, "
                "closed_at=clock_timestamp() WHERE shipment_id=:shipment_id AND status='OPEN'"
            ),
            {"actor": context["sender_id"], "shipment_id": context["shipment_id"]},
        )
        with pytest.raises(DBAPIError, match="Every received item must have a result"):
            await session.execute(
                text("UPDATE shipments SET status='COMPLETED' WHERE id=:shipment_id"),
                {"shipment_id": context["shipment_id"]},
            )
        await session.rollback()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("receipt_payload", "target_status", "expected_error"),
    [
        (
            {"result": "MISSING", "reason_code": "SAMPLE_NOT_FOUND"},
            "COMPLETED",
            "COMPLETED requires all received item results to be ACCEPTED",
        ),
        (
            {"result": "ACCEPTED"},
            "COMPLETED_WITH_ISSUES",
            "COMPLETED_WITH_ISSUES requires at least one receiving issue",
        ),
    ],
)
async def test_database_rejects_completion_status_that_disagrees_with_results(
    receipt_payload: dict, target_status: str, expected_error: str
) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context = await create_pickup_fixture(client)
        lab_auth = await receive_first_container(context, client)
        await record_result(
            client,
            context,
            lab_auth,
            context["containers"][0],
            receipt_payload,
            f"direct-derived-status-{context['suffix']}",
        )

    async with get_session_factory()() as session:
        await session.execute(
            text(
                "UPDATE receiving_sessions SET status='CLOSED', closed_by=:actor, "
                "closed_at=clock_timestamp() WHERE shipment_id=:shipment_id AND status='OPEN'"
            ),
            {"actor": context["sender_id"], "shipment_id": context["shipment_id"]},
        )
        with pytest.raises(DBAPIError, match=expected_error):
            await session.execute(
                text("UPDATE shipments SET status=:status WHERE id=:shipment_id"),
                {"status": target_status, "shipment_id": context["shipment_id"]},
            )
        await session.rollback()


@pytest.mark.asyncio
async def test_database_rejects_reopen_without_exactly_one_open_session_with_reason() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context = await create_pickup_fixture(client)
        lab_auth = await receive_first_container(context, client)
        await record_result(
            client,
            context,
            lab_auth,
            context["containers"][0],
            {"result": "ACCEPTED"},
            f"direct-reopen-result-{context['suffix']}",
        )
        completed = await complete_receiving(
            client,
            context,
            lab_auth,
            f"direct-reopen-complete-{context['suffix']}",
        )
        assert completed.status_code == 200, completed.text
        manager_auth = await authorize_lab_manager(context["shipment_id"], client)

    async with get_session_factory()() as session:
        manager_id = await session.scalar(
            select(Membership.user_id)
            .join(Organization, Organization.id == Membership.organization_id)
            .join(Shipment, Shipment.destination_organization_id == Organization.id)
            .where(
                Shipment.id == context["shipment_id"],
                Membership.role == "LAB_MANAGER",
                Membership.is_active.is_(True),
            )
        )
        assert manager_id is not None
        with pytest.raises(
            DBAPIError,
            match="Reopen requires exactly one OPEN receiving session with a reason",
        ):
            await session.execute(
                text("UPDATE shipments SET status='RECEIVING' WHERE id=:shipment_id"),
                {"shipment_id": context["shipment_id"]},
            )
        await session.rollback()

        with pytest.raises(
            DBAPIError,
            match="Reopened receiving session requires a nonblank reason",
        ):
            await session.execute(
                text(
                    "INSERT INTO receiving_sessions "
                    "(id, shipment_id, sequence, status, opened_by, opened_at, reopen_reason) "
                    "VALUES (:id, :shipment_id, 2, 'OPEN', :actor, clock_timestamp(), '  ')"
                ),
                {
                    "id": uuid4(),
                    "shipment_id": context["shipment_id"],
                    "actor": manager_id,
                },
            )
        await session.rollback()
    assert manager_auth


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "actor_kind",
    [
        "sender",
        "wrong_scope_manager",
        "inactive_membership",
        "inactive_user",
        "inactive_organization",
        "inactive_region",
    ],
)
async def test_database_rejects_reopen_session_without_active_destination_manager(
    actor_kind: str,
) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context = await create_pickup_fixture(client)
        await complete_accepted_shipment(client, context, f"direct-invalid-opener-{actor_kind}")

        if actor_kind == "wrong_scope_manager":
            await create_wrong_scope_lab_manager(context["shipment_id"], client)
        else:
            await authorize_lab_manager(context["shipment_id"], client)

    async with get_session_factory()() as session:
        destination_id = await session.scalar(
            select(Shipment.destination_organization_id).where(
                Shipment.id == context["shipment_id"]
            )
        )
        manager_query = select(Membership.user_id).where(Membership.role == "LAB_MANAGER")
        if actor_kind == "wrong_scope_manager":
            manager_query = manager_query.where(Membership.organization_id != destination_id)
        else:
            manager_query = manager_query.where(Membership.organization_id == destination_id)
        manager_id = await session.scalar(manager_query)
        assert destination_id is not None
        assert manager_id is not None or actor_kind == "sender"
        if actor_kind == "sender":
            actor_id = context["sender_id"]
        else:
            actor_id = manager_id
            if actor_kind.startswith("inactive_"):
                if actor_kind == "inactive_membership":
                    await session.execute(
                        text("UPDATE memberships SET is_active=false WHERE user_id=:user_id"),
                        {"user_id": actor_id},
                    )
                elif actor_kind == "inactive_user":
                    await session.execute(
                        text("UPDATE users SET is_active=false WHERE id=:user_id"),
                        {"user_id": actor_id},
                    )
                elif actor_kind == "inactive_organization":
                    await session.execute(
                        text("UPDATE organizations SET is_active=false WHERE id=:organization_id"),
                        {"organization_id": destination_id},
                    )
                else:
                    region_id = await session.scalar(
                        select(Shipment.region_id).where(Shipment.id == context["shipment_id"])
                    )
                    await session.execute(
                        text("UPDATE regions SET is_active=false WHERE id=:region_id"),
                        {"region_id": region_id},
                    )
        with pytest.raises(
            DBAPIError,
            match="Reopened receiving session opener must be an active destination LAB_MANAGER",
        ):
            await session.execute(
                text(
                    "INSERT INTO receiving_sessions "
                    "(id, shipment_id, sequence, status, opened_by, opened_at, reopen_reason) "
                    "VALUES (:id, :shipment_id, 2, 'OPEN', :actor, "
                    "clock_timestamp(), 'Direct SQL test')"
                ),
                {
                    "id": uuid4(),
                    "shipment_id": context["shipment_id"],
                    "actor": actor_id,
                },
            )
        await session.rollback()


@pytest.mark.asyncio
async def test_database_rejects_regional_session_without_exceptional_evidence() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context = await create_pickup_fixture(client)
        await confirm_pickup(context, client)
        async with get_session_factory()() as session:
            region_id = await session.scalar(
                select(Shipment.region_id).where(Shipment.id == context["shipment_id"])
            )
        assert region_id is not None
        await authorize_regional_operator(str(region_id), client)

    async with get_session_factory()() as session:
        resolver_id = await session.scalar(
            select(Membership.user_id).where(
                Membership.region_id == region_id,
                Membership.role == "REGIONAL_OPERATIONS",
                Membership.is_active.is_(True),
            )
        )
        assert resolver_id is not None
        with pytest.raises(DBAPIError):
            await session.execute(
                text(
                    "INSERT INTO receiving_sessions "
                    "(id, shipment_id, sequence, status, opened_by, opened_at) "
                    "VALUES (:id, :shipment_id, 1, 'OPEN', :actor, clock_timestamp())"
                ),
                {
                    "id": uuid4(),
                    "shipment_id": context["shipment_id"],
                    "actor": resolver_id,
                },
            )
        await session.rollback()


@pytest.mark.asyncio
async def test_database_rechecks_reopen_manager_membership_at_shipment_transition() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context = await create_pickup_fixture(client)
        await complete_accepted_shipment(client, context, "direct-recheck-reopen-opener")
        await authorize_lab_manager(context["shipment_id"], client)

    async with get_session_factory()() as session:
        destination_id = await session.scalar(
            select(Shipment.destination_organization_id).where(
                Shipment.id == context["shipment_id"]
            )
        )
        manager_id = await session.scalar(
            select(Membership.user_id).where(
                Membership.organization_id == destination_id,
                Membership.role == "LAB_MANAGER",
                Membership.is_active.is_(True),
            )
        )
        assert manager_id is not None
        await session.execute(
            text(
                "INSERT INTO receiving_sessions "
                "(id, shipment_id, sequence, status, opened_by, opened_at, reopen_reason) "
                "VALUES (:id, :shipment_id, 2, 'OPEN', :actor, "
                "clock_timestamp(), 'Active at insert')"
            ),
            {
                "id": uuid4(),
                "shipment_id": context["shipment_id"],
                "actor": manager_id,
            },
        )
        await session.execute(
            text("UPDATE memberships SET is_active=false WHERE user_id=:user_id"),
            {"user_id": manager_id},
        )
        with pytest.raises(
            DBAPIError,
            match="Reopen requires an active destination LAB_MANAGER opener",
        ):
            await session.execute(
                text("UPDATE shipments SET status='RECEIVING' WHERE id=:shipment_id"),
                {"shipment_id": context["shipment_id"]},
            )
        await session.rollback()


@pytest.mark.asyncio
async def test_database_rejects_reopening_mutating_or_deleting_closed_receiving_session() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context = await create_pickup_fixture(client)
        await complete_accepted_shipment(client, context, "direct-closed-session")

    async with get_session_factory()() as session:
        session_id = await session.scalar(
            select(ReceivingSessionModel.id).where(
                ReceivingSessionModel.shipment_id == context["shipment_id"]
            )
        )
        assert session_id is not None
        with pytest.raises(DBAPIError, match="Closed receiving sessions are immutable"):
            await session.execute(
                text(
                    "UPDATE receiving_sessions SET status='OPEN', closed_by=NULL, closed_at=NULL "
                    "WHERE id=:session_id"
                ),
                {"session_id": session_id},
            )
        await session.rollback()

        with pytest.raises(DBAPIError, match="Receiving sessions cannot be deleted"):
            await session.execute(
                text("DELETE FROM receiving_sessions WHERE id=:session_id"),
                {"session_id": session_id},
            )
        await session.rollback()
