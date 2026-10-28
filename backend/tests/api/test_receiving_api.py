"""Receipt revision persistence and PostgreSQL receiving guards."""

import asyncio
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
from app.core.security import hash_password
from app.db.base import Base
from app.db.session import get_session_factory
from app.main import app
from app.modules.audit.models import AuditEvent
from app.modules.idempotency.models import IdempotencyRecord
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


def test_stage_f_migration_is_additive_after_stage_e() -> None:
    backend = Path(__file__).resolve().parents[2]
    config = Config(str(backend / "alembic.ini"))
    config.set_main_option("script_location", str(backend / "alembic"))
    script = ScriptDirectory.from_config(config)
    head = script.get_current_head()
    revision = script.get_revision(head)

    assert revision.revision == "0007_stage_f_lifecycle"
    assert revision.down_revision == "0006_stage_f"


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
        assert "Injected receiving completion audit failure" in caplog.text
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

    async with get_session_factory()() as session:
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
                "actor": context["sender_id"],
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

    async with get_session_factory()() as session:
        with pytest.raises(
            DBAPIError,
            match="Reopen requires exactly one OPEN receiving session with a reason",
        ):
            await session.execute(
                text("UPDATE shipments SET status='RECEIVING' WHERE id=:shipment_id"),
                {"shipment_id": context["shipment_id"]},
            )
        await session.rollback()

        await session.execute(
            text(
                "INSERT INTO receiving_sessions "
                "(id, shipment_id, sequence, status, opened_by, opened_at, reopen_reason) "
                "VALUES (:id, :shipment_id, 2, 'OPEN', :actor, clock_timestamp(), '  ')"
            ),
            {
                "id": uuid4(),
                "shipment_id": context["shipment_id"],
                "actor": context["sender_id"],
            },
        )
        with pytest.raises(
            DBAPIError,
            match="Reopen requires exactly one OPEN receiving session with a reason",
        ):
            await session.execute(
                text("UPDATE shipments SET status='RECEIVING' WHERE id=:shipment_id"),
                {"shipment_id": context["shipment_id"]},
            )
        await session.rollback()
