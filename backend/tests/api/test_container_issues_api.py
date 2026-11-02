"""Container issue and two-stage resolution acceptance tests."""

from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from test_delivery_api import authorize_lab_operator, confirm_pickup
from test_pickup_api import PASSWORD, create_pickup_fixture

from app.core.security import hash_password
from app.db.session import get_session_factory
from app.main import app
from app.modules.identity.models import Membership, User
from app.modules.shipments.models import Shipment


def test_stage_e_migration_is_additive_after_delivery() -> None:
    backend = Path(__file__).resolve().parents[2]
    config = Config(str(backend / "alembic.ini"))
    config.set_main_option("script_location", str(backend / "alembic"))
    script = ScriptDirectory.from_config(config)
    head_revision = script.get_revision(script.get_current_head())
    stage_e_revision = script.get_revision("0005_stage_e")

    assert head_revision.revision == "0011_http_request_scope"
    assert head_revision.down_revision == "0010_notifications"
    assert stage_e_revision.down_revision == "0004_delivery"


@pytest.mark.asyncio
async def test_issue_and_proposal_constraints_match_contract() -> None:
    async with get_session_factory()() as session:
        tables = await session.execute(
            text(
                "SELECT tablename FROM pg_tables WHERE schemaname='public' "
                "AND tablename IN ('container_issues', 'container_issue_resolution_proposals')"
            )
        )
        assert {row[0] for row in tables} == {
            "container_issues",
            "container_issue_resolution_proposals",
        }
        indexes = await session.execute(
            text("SELECT indexdef FROM pg_indexes WHERE indexname='uq_issue_pending_proposal'")
        )
        index_definitions = [row[0] for row in indexes]
        assert len(index_definitions) == 1
        assert "UNIQUE" in index_definitions[0]
        assert "WHERE" in index_definitions[0]
        assert "PENDING" in index_definitions[0]


@pytest.mark.asyncio
async def test_database_rejects_invalid_issue_and_proposal_transitions() -> None:
    async with get_session_factory()() as session:
        guards = await session.execute(
            text(
                "SELECT tgname FROM pg_trigger WHERE NOT tgisinternal "
                "AND tgname IN ('trg_guard_container_issue', 'trg_guard_issue_proposal')"
            )
        )
        assert {row[0] for row in guards} == {
            "trg_guard_container_issue",
            "trg_guard_issue_proposal",
        }


@pytest.mark.asyncio
async def test_courier_reports_missing_container() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context = await create_pickup_fixture(client)
        await confirm_pickup(context, client)
        response = await client.post(
            f"/api/v1/shipments/{context['shipment_id']}/container-issues",
            headers={
                **context["courier_auth"],
                "Idempotency-Key": f"issue-missing-{context['suffix']}",
            },
            json={
                "container_id": context["container_id"],
                "kind": "MISSING",
                "reason_code": "CONTAINER_NOT_PRESENT",
                "comment": "Container was not presented",
            },
        )

    assert response.status_code == 201, response.text
    assert response.json()["issue"]["kind"] == "MISSING"
    assert response.json()["issue"]["status"] == "OPEN"
    assert response.json()["shipment_status"] == "DELIVERY_BLOCKED"


@pytest.mark.asyncio
async def test_lab_operator_reports_missing_or_rejected_container() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context = await create_pickup_fixture(client)
        await confirm_pickup(context, client)
        lab_auth = await authorize_lab_operator(context["shipment_id"], client)
        response = await client.post(
            f"/api/v1/shipments/{context['shipment_id']}/container-issues",
            headers={**lab_auth, "Idempotency-Key": f"issue-rejected-{context['suffix']}"},
            json={
                "container_id": context["container_id"],
                "kind": "REJECTED",
                "reason_code": "CONTAINER_SEAL_BROKEN",
                "comment": "Seal was damaged on arrival",
            },
        )

    assert response.status_code == 201, response.text
    assert response.json()["issue"]["kind"] == "REJECTED"
    assert response.json()["issue"]["reported_by"]


@pytest.mark.asyncio
async def test_issue_blocks_only_target_container_and_withdraws_pending_delivery() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context = await create_pickup_fixture(client, container_count=2)
        await confirm_pickup(context, client)
        lab_auth = await authorize_lab_operator(context["shipment_id"], client)
        target = context["containers"][0]
        other = context["containers"][1]
        pending = await client.post(
            f"/api/v1/shipments/{context['shipment_id']}/containers/{target['id']}"
            "/handoffs/delivery/confirm-delivery",
            headers={
                **context["courier_auth"],
                "Idempotency-Key": f"delivery-pending-{context['suffix']}",
            },
            json={"container_code": target["code"]},
        )
        assert pending.status_code == 200, pending.text
        issue = await client.post(
            f"/api/v1/shipments/{context['shipment_id']}/container-issues",
            headers={**lab_auth, "Idempotency-Key": f"issue-pending-{context['suffix']}"},
            json={
                "container_id": target["id"],
                "kind": "MISSING",
                "reason_code": "CONTAINER_NOT_PRESENT",
            },
        )
        assert issue.status_code == 201, issue.text
        manifest = await client.get(
            f"/api/v1/shipments/{context['shipment_id']}/manifest",
            headers=lab_auth,
        )

    states = {
        container["id"]: container["delivery_status"] for container in manifest.json()["containers"]
    }
    assert states[target["id"]] == "BLOCKED"
    assert states[other["id"]] == "IN_TRANSIT"
    assert issue.json()["shipment_status"] == "DELIVERY_BLOCKED"


@pytest.mark.asyncio
async def test_issue_report_rejects_wrong_actor_state_scope_or_reason() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context = await create_pickup_fixture(client)
        await confirm_pickup(context, client)
        response = await client.post(
            f"/api/v1/shipments/{context['shipment_id']}/container-issues",
            headers={**context["sender_auth"], "Idempotency-Key": "bad-issue-sender-001"},
            json={
                "container_id": context["container_id"],
                "kind": "MISSING",
                "reason_code": "SAMPLE_NOT_FOUND",
            },
        )
        assert response.status_code in (403, 404)
        invalid_reason = await client.post(
            f"/api/v1/shipments/{context['shipment_id']}/container-issues",
            headers={**context["courier_auth"], "Idempotency-Key": "bad-issue-reason-001"},
            json={
                "container_id": context["container_id"],
                "kind": "MISSING",
                "reason_code": "SAMPLE_NOT_FOUND",
            },
        )

    assert invalid_reason.status_code == 409


async def authorize_regional_operator(region_id: str, client: AsyncClient) -> dict[str, str]:
    from uuid import uuid4

    suffix = uuid4().hex[:10]
    async with get_session_factory()() as session:
        operator = User(
            email=f"regional-{suffix}@example.test",
            display_name="Regional Operator",
            password_hash=hash_password(PASSWORD),
        )
        session.add(operator)
        await session.flush()
        session.add(
            Membership(
                user_id=operator.id,
                region_id=region_id,
                role="REGIONAL_OPERATIONS",
            )
        )
        await session.commit()
        email = operator.email
    login = await client.post("/api/v1/auth/token", json={"email": email, "password": PASSWORD})
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


async def authorize_lab_manager(shipment_id: str, client: AsyncClient) -> dict[str, str]:
    from uuid import uuid4

    suffix = uuid4().hex[:10]
    async with get_session_factory()() as session:
        shipment = await session.get(Shipment, shipment_id)
        assert shipment is not None
        manager = User(
            email=f"lab-manager-{suffix}@example.test",
            display_name="Resolution Lab Manager",
            password_hash=hash_password(PASSWORD),
        )
        session.add(manager)
        await session.flush()
        session.add(
            Membership(
                user_id=manager.id,
                organization_id=shipment.destination_organization_id,
                role="LAB_MANAGER",
            )
        )
        await session.commit()
        email = manager.email
    login = await client.post("/api/v1/auth/token", json={"email": email, "password": PASSWORD})
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


async def authorize_dual_role_manager(shipment_id: str, client: AsyncClient) -> dict[str, str]:
    from uuid import uuid4

    suffix = uuid4().hex[:10]
    async with get_session_factory()() as session:
        shipment = await session.get(Shipment, shipment_id)
        assert shipment is not None
        user = User(
            email=f"dual-role-{suffix}@example.test",
            display_name="Dual Role Reviewer",
            password_hash=hash_password(PASSWORD),
        )
        session.add(user)
        await session.flush()
        session.add_all(
            [
                Membership(
                    user_id=user.id,
                    region_id=shipment.region_id,
                    role="REGIONAL_OPERATIONS",
                ),
                Membership(
                    user_id=user.id,
                    organization_id=shipment.destination_organization_id,
                    role="LAB_MANAGER",
                ),
            ]
        )
        await session.commit()
        email = user.email
    login = await client.post("/api/v1/auth/token", json={"email": email, "password": PASSWORD})
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


async def create_issue(context: dict, client: AsyncClient) -> dict:
    response = await client.post(
        f"/api/v1/shipments/{context['shipment_id']}/container-issues",
        headers={
            **context["courier_auth"],
            "Idempotency-Key": f"proposal-issue-{context['suffix']}",
        },
        json={
            "container_id": context["container_id"],
            "kind": "MISSING",
            "reason_code": "CONTAINER_NOT_PRESENT",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["issue"]


@pytest.mark.asyncio
async def test_resolution_proposal_does_not_change_custody_and_approval_retries_delivery() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context = await create_pickup_fixture(client)
        await confirm_pickup(context, client)
        issue = await create_issue(context, client)
        async with get_session_factory()() as session:
            region_id = await session.scalar(
                text("SELECT region_id FROM shipments WHERE id=:id"),
                {"id": context["shipment_id"]},
            )
        regional_auth = await authorize_regional_operator(str(region_id), client)
        manager_auth = await authorize_lab_manager(context["shipment_id"], client)
        base = (
            f"/api/v1/shipments/{context['shipment_id']}/container-issues/{issue['id']}/proposals"
        )
        proposed = await client.post(
            base,
            headers={**regional_auth, "Idempotency-Key": f"proposal-{context['suffix']}"},
            json={"action": "RETRY_DELIVERY", "reason": "Carrier can return tomorrow"},
        )
        replay = await client.post(
            base,
            headers={**regional_auth, "Idempotency-Key": f"proposal-{context['suffix']}"},
            json={"action": "RETRY_DELIVERY", "reason": "Carrier can return tomorrow"},
        )
        duplicate_pending = await client.post(
            base,
            headers={**regional_auth, "Idempotency-Key": f"duplicate-proposal-{context['suffix']}"},
            json={"action": "CLOSE_UNDELIVERED", "reason": "Second action"},
        )
        listed = await client.get(base, headers=regional_auth)
        manifest = await client.get(
            f"/api/v1/shipments/{context['shipment_id']}/manifest", headers=context["sender_auth"]
        )
        proposal_id = (
            proposed.json()["id"]
            if proposed.status_code == 201
            else "00000000-0000-0000-0000-000000000000"
        )
        decided = await client.post(
            f"{base}/{proposal_id}/decide",
            headers={**manager_auth, "Idempotency-Key": f"approve-{context['suffix']}"},
            json={"decision": "APPROVE"},
        )
        stale_decision = await client.post(
            f"{base}/{proposal_id}/decide",
            headers={**manager_auth, "Idempotency-Key": f"stale-approve-{context['suffix']}"},
            json={"decision": "APPROVE"},
        )

    assert proposed.status_code == 201, proposed.text
    assert replay.status_code == 201
    assert replay.json() == proposed.json()
    assert duplicate_pending.status_code == 409
    assert listed.status_code == 200
    assert listed.json()["total"] == 1
    assert listed.json()["items"][0]["id"] == proposed.json()["id"]
    assert manifest.json()["containers"][0]["delivery_status"] == "BLOCKED"
    assert decided.status_code == 200, decided.text
    assert decided.json()["issue"]["status"] == "RESOLVED"
    assert decided.json()["container"]["delivery_status"] == "IN_TRANSIT"
    assert stale_decision.status_code == 409


@pytest.mark.asyncio
async def test_rejected_proposal_preserves_block_and_allows_retry_withdrawal() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context = await create_pickup_fixture(client)
        await confirm_pickup(context, client)
        issue = await create_issue(context, client)
        async with get_session_factory()() as session:
            region_id = await session.scalar(
                text("SELECT region_id FROM shipments WHERE id=:id"), {"id": context["shipment_id"]}
            )
        regional_auth = await authorize_regional_operator(str(region_id), client)
        manager_auth = await authorize_lab_manager(context["shipment_id"], client)
        base = (
            f"/api/v1/shipments/{context['shipment_id']}/container-issues/{issue['id']}/proposals"
        )
        first = await client.post(
            base,
            headers={**regional_auth, "Idempotency-Key": f"reject-proposal-{context['suffix']}"},
            json={"action": "RETRY_DELIVERY", "reason": "Review return option"},
        )
        first_id = first.json().get("id", "00000000-0000-0000-0000-000000000000")
        rejected = await client.post(
            f"{base}/{first_id}/decide",
            headers={**manager_auth, "Idempotency-Key": f"reject-{context['suffix']}"},
            json={"decision": "REJECT", "reason": "Need more evidence"},
        )
        second = await client.post(
            base,
            headers={**regional_auth, "Idempotency-Key": f"withdraw-proposal-{context['suffix']}"},
            json={"action": "CLOSE_UNDELIVERED", "reason": "No courier available"},
        )
        withdrawal = await client.post(
            f"{base}/{second.json().get('id', '00000000-0000-0000-0000-000000000000')}/withdraw",
            headers={**regional_auth, "Idempotency-Key": f"withdraw-{context['suffix']}"},
            json={"reason": "Reassess after staffing"},
        )

    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["issue"]["status"] == "OPEN"
    assert rejected.json()["container"]["delivery_status"] == "BLOCKED"
    assert second.status_code == 201, second.text
    assert withdrawal.status_code == 200, withdrawal.text
    assert withdrawal.json()["status"] == "WITHDRAWN"


@pytest.mark.asyncio
async def test_approved_undelivered_closure_preserves_active_sibling() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context = await create_pickup_fixture(client, container_count=2)
        await confirm_pickup(context, client)
        issue = await create_issue(context, client)
        async with get_session_factory()() as session:
            region_id = await session.scalar(
                text("SELECT region_id FROM shipments WHERE id=:id"),
                {"id": context["shipment_id"]},
            )
        regional_auth = await authorize_regional_operator(str(region_id), client)
        manager_auth = await authorize_lab_manager(context["shipment_id"], client)
        base = (
            f"/api/v1/shipments/{context['shipment_id']}/container-issues/{issue['id']}/proposals"
        )
        proposed = await client.post(
            base,
            headers={**regional_auth, "Idempotency-Key": f"close-proposal-{context['suffix']}"},
            json={"action": "CLOSE_UNDELIVERED", "reason": "Courier cannot return"},
        )
        decided = await client.post(
            f"{base}/{proposed.json().get('id', '00000000-0000-0000-0000-000000000000')}/decide",
            headers={**manager_auth, "Idempotency-Key": f"close-approve-{context['suffix']}"},
            json={"decision": "APPROVE"},
        )

    assert proposed.status_code == 201, proposed.text
    assert decided.status_code == 200, decided.text
    assert decided.json()["container"]["delivery_status"] == "UNDELIVERED_CLOSED"
    assert decided.json()["shipment"]["status"] == "DELIVERY_BLOCKED"
    assert decided.json()["shipment"]["status"] != "CLOSED_UNDELIVERED"


@pytest.mark.asyncio
async def test_regional_lab_manager_cannot_approve_own_proposal() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context = await create_pickup_fixture(client)
        await confirm_pickup(context, client)
        issue = await create_issue(context, client)
        dual_role_auth = await authorize_dual_role_manager(context["shipment_id"], client)
        base = (
            f"/api/v1/shipments/{context['shipment_id']}/container-issues/{issue['id']}/proposals"
        )
        proposed = await client.post(
            base,
            headers={**dual_role_auth, "Idempotency-Key": f"dual-proposal-{context['suffix']}"},
            json={"action": "RETRY_DELIVERY", "reason": "Try once more"},
        )
        response = await client.post(
            f"{base}/{proposed.json().get('id', '00000000-0000-0000-0000-000000000000')}/decide",
            headers={**dual_role_auth, "Idempotency-Key": f"self-approve-{context['suffix']}"},
            json={"decision": "APPROVE"},
        )

    assert proposed.status_code == 201, proposed.text
    assert response.status_code == 409
