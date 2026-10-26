"""Receipt revision persistence and PostgreSQL receiving guards."""

from pathlib import Path
from uuid import uuid4

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from httpx import ASGITransport, AsyncClient
from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError
from test_container_issues_api import (
    authorize_lab_manager,
    authorize_regional_operator,
    create_issue,
)
from test_delivery_api import authorize_lab_operator, confirm_pickup
from test_pickup_api import create_pickup_fixture

from app.db.base import Base
from app.db.session import get_session_factory
from app.main import app

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


async def receive_first_container(context: dict, client: AsyncClient) -> None:
    container = context["containers"][0]
    await confirm_pickup(context, client)
    lab_auth = await authorize_lab_operator(context["shipment_id"], client)
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
