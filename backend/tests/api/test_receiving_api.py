"""Receipt revision persistence and PostgreSQL receiving guards."""

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
from test_pickup_api import create_pickup_fixture

from app.core.config import get_settings
from app.db.base import Base
from app.db.session import get_session_factory
from app.main import app
from app.modules.audit.models import AuditEvent
from app.modules.idempotency.models import IdempotencyRecord
from app.modules.identity.models import Membership
from app.modules.shipments.models import ContainerItem, ReceiptRevision, Shipment

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

    assert revision.revision == "0006_stage_f"
    assert revision.down_revision == "0005_stage_e"


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
            async with owner_engine.begin() as connection:
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
