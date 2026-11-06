"""Public demo seed roles stay active and scoped to their intended organizations."""

from __future__ import annotations

from urllib.request import Request
from uuid import uuid4

import pytest
from sqlalchemy import select

from app import demo_seed
from app.core.config import Settings, get_settings
from app.core.security import verify_password
from app.modules.identity.models import Membership, Organization, Region, User


def configure_seed_environment(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    values = {
        "DEMO_SENDER_EMAIL": "sender@demo.example.test",
        "DEMO_SENDER_PASSWORD": f"sender-test-{uuid4().hex}",
        "DEMO_COURIER_EMAIL": "courier@demo.example.test",
        "DEMO_COURIER_PASSWORD": f"courier-test-{uuid4().hex}",
        "DEMO_LAB_OPERATOR_EMAIL": "lab-operator@demo.example.test",
        "DEMO_LAB_OPERATOR_PASSWORD": f"operator-test-{uuid4().hex}",
        "DEMO_LAB_MANAGER_EMAIL": "lab-manager@demo.example.test",
        "DEMO_LAB_MANAGER_PASSWORD": f"manager-test-{uuid4().hex}",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    seed_settings = get_settings().model_copy(
        update={"app_env": "demo", "demo_seed_enabled": True, "postgres_db": "labtrack_demo_test"}
    )
    monkeypatch.setattr(demo_seed, "get_settings", lambda: seed_settings)
    return values


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "settings",
    (
        Settings(_env_file=None),
        Settings(
            _env_file=None,
            app_env="production",
            demo_seed_enabled=True,
            postgres_db="labtrack_demo",
            jwt_secret_key="unit-test-jwt-secret-that-is-long-enough-000000",
        ),
    ),
)
async def test_seed_rejects_default_and_production_before_database_access(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    monkeypatch.setattr(demo_seed, "get_settings", lambda: settings)
    monkeypatch.setattr(
        demo_seed,
        "get_session_factory",
        lambda: pytest.fail("database access must not happen when the guard rejects"),
    )

    with pytest.raises(demo_seed.DemoSeedNotAllowed):
        await demo_seed.ensure_demo_identities()


def test_demo_api_request_cannot_reach_an_external_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested: list[Request] = []
    monkeypatch.setenv("DEMO_API_URL", "https://payments.example.invalid")
    monkeypatch.setattr(demo_seed, "urlopen", lambda request, timeout: requested.append(request))

    with pytest.raises(demo_seed.DemoSeedNotAllowed):
        demo_seed.api_request("GET", "/health")

    assert requested == []


def test_demo_api_request_uses_a_mocked_local_labtrack_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested: list[Request] = []

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def read(self) -> bytes:
            return b'{"status":"ok"}'

    def fake_urlopen(request: Request, timeout: int) -> FakeResponse:
        assert timeout == 15
        requested.append(request)
        return FakeResponse()

    monkeypatch.setenv("DEMO_API_URL", "http://api:8000")
    monkeypatch.setattr(demo_seed, "urlopen", fake_urlopen)

    assert demo_seed.api_request("GET", "/health") == {"status": "ok"}
    assert len(requested) == 1
    assert requested[0].full_url == "http://api:8000/health"


@pytest.mark.asyncio
async def test_seed_provisions_four_public_roles_in_scope_and_is_idempotent(
    monkeypatch: pytest.MonkeyPatch, database_sessions
) -> None:
    values = configure_seed_environment(monkeypatch)
    monkeypatch.setattr(demo_seed, "get_session_factory", lambda: database_sessions)

    first_result = await demo_seed.ensure_demo_identities()
    second_result = await demo_seed.ensure_demo_identities()

    assert first_result == second_result
    async with database_sessions() as session:
        users = {
            user.email: user for user in await session.scalars(select(User).order_by(User.email))
        }
        assert set(users) == {
            values["DEMO_SENDER_EMAIL"],
            values["DEMO_COURIER_EMAIL"],
            values["DEMO_LAB_OPERATOR_EMAIL"],
            values["DEMO_LAB_MANAGER_EMAIL"],
        }
        region = await session.scalar(select(Region).where(Region.code == "PILOT"))
        sender_org = await session.scalar(
            select(Organization).where(Organization.code == "DEMO_SENDER")
        )
        laboratory = await session.scalar(
            select(Organization).where(Organization.code == "CENTRAL_LAB")
        )
        assert region is not None and sender_org is not None and laboratory is not None

        expected_scopes = {
            values["DEMO_SENDER_EMAIL"]: ("SENDER_OPERATOR", sender_org.id, None),
            values["DEMO_COURIER_EMAIL"]: ("COURIER", None, region.id),
            values["DEMO_LAB_OPERATOR_EMAIL"]: ("LAB_OPERATOR", laboratory.id, None),
            values["DEMO_LAB_MANAGER_EMAIL"]: ("LAB_MANAGER", laboratory.id, None),
        }
        for email, (role, organization_id, region_id) in expected_scopes.items():
            memberships = list(
                await session.scalars(
                    select(Membership).where(Membership.user_id == users[email].id)
                )
            )
            assert len(memberships) == 1
            membership = memberships[0]
            assert (membership.role, membership.organization_id, membership.region_id) == (
                role,
                organization_id,
                region_id,
            )
            assert membership.is_active

        all_memberships = list(await session.scalars(select(Membership)))
        assert len(users) == 4
        assert len(all_memberships) == 4
        assert all(
            membership.role not in {"REGIONAL_OPERATIONS", "REGIONAL_ANALYST"}
            for membership in all_memberships
        )
        assert verify_password(
            values["DEMO_LAB_OPERATOR_PASSWORD"],
            users[values["DEMO_LAB_OPERATOR_EMAIL"]].password_hash,
        )
        assert verify_password(
            values["DEMO_LAB_MANAGER_PASSWORD"],
            users[values["DEMO_LAB_MANAGER_EMAIL"]].password_hash,
        )
        password_by_email = {
            values["DEMO_SENDER_EMAIL"]: values["DEMO_SENDER_PASSWORD"],
            values["DEMO_COURIER_EMAIL"]: values["DEMO_COURIER_PASSWORD"],
            values["DEMO_LAB_OPERATOR_EMAIL"]: values["DEMO_LAB_OPERATOR_PASSWORD"],
            values["DEMO_LAB_MANAGER_EMAIL"]: values["DEMO_LAB_MANAGER_PASSWORD"],
        }
        for account_email, user in users.items():
            for candidate_email, password in password_by_email.items():
                assert verify_password(password, user.password_hash) is (
                    candidate_email == account_email
                )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "missing_name",
    (
        "DEMO_LAB_OPERATOR_EMAIL",
        "DEMO_LAB_OPERATOR_PASSWORD",
        "DEMO_LAB_MANAGER_EMAIL",
        "DEMO_LAB_MANAGER_PASSWORD",
    ),
)
async def test_seed_requires_each_lab_identity_environment_value(
    monkeypatch: pytest.MonkeyPatch, database_sessions, missing_name: str
) -> None:
    configure_seed_environment(monkeypatch)
    monkeypatch.delenv(missing_name)
    monkeypatch.setattr(demo_seed, "get_session_factory", lambda: database_sessions)

    with pytest.raises(RuntimeError, match=missing_name):
        await demo_seed.ensure_demo_identities()


@pytest.mark.asyncio
async def test_seed_refuses_an_inactive_existing_lab_user(
    monkeypatch: pytest.MonkeyPatch, database_sessions
) -> None:
    values = configure_seed_environment(monkeypatch)
    monkeypatch.setattr(demo_seed, "get_session_factory", lambda: database_sessions)
    async with database_sessions() as session:
        async with session.begin():
            await demo_seed.provision_user(
                session,
                email=values["DEMO_LAB_OPERATOR_EMAIL"],
                display_name="Synthetic Demo Lab Operator",
                password=values["DEMO_LAB_OPERATOR_PASSWORD"],
                role="LAB_OPERATOR",
                organization_code="CENTRAL_LAB",
            )
            user = await session.scalar(
                select(User).where(User.email == values["DEMO_LAB_OPERATOR_EMAIL"])
            )
            assert user is not None
            user.is_active = False

    with pytest.raises(demo_seed.ProvisioningError, match="is inactive"):
        await demo_seed.ensure_demo_identities()

    async with database_sessions() as session:
        user = await session.scalar(
            select(User).where(User.email == values["DEMO_LAB_OPERATOR_EMAIL"])
        )
        memberships = list(
            await session.scalars(select(Membership).where(Membership.user_id == user.id))
        )
        assert user is not None and not user.is_active
        assert [(membership.role, membership.is_active) for membership in memberships] == [
            ("LAB_OPERATOR", True)
        ]


@pytest.mark.asyncio
async def test_seed_refuses_a_conflicting_existing_lab_membership_without_changing_it(
    monkeypatch: pytest.MonkeyPatch, database_sessions
) -> None:
    values = configure_seed_environment(monkeypatch)
    monkeypatch.setattr(demo_seed, "get_session_factory", lambda: database_sessions)
    async with database_sessions() as session:
        async with session.begin():
            await demo_seed.provision_user(
                session,
                email=values["DEMO_LAB_OPERATOR_EMAIL"],
                display_name="Synthetic Demo Lab Operator",
                password=values["DEMO_LAB_OPERATOR_PASSWORD"],
                role="LAB_MANAGER",
                organization_code="CENTRAL_LAB",
            )

    with pytest.raises(demo_seed.ProvisioningError, match="required demo role"):
        await demo_seed.ensure_demo_identities()

    async with database_sessions() as session:
        user = await session.scalar(
            select(User).where(User.email == values["DEMO_LAB_OPERATOR_EMAIL"])
        )
        memberships = list(
            await session.scalars(select(Membership).where(Membership.user_id == user.id))
        )
        assert user is not None and user.is_active
        assert [(membership.role, membership.is_active) for membership in memberships] == [
            ("LAB_MANAGER", True)
        ]


@pytest.mark.asyncio
async def test_seed_refuses_expected_role_with_an_additional_active_role(
    monkeypatch: pytest.MonkeyPatch, database_sessions
) -> None:
    values = configure_seed_environment(monkeypatch)
    monkeypatch.setattr(demo_seed, "get_session_factory", lambda: database_sessions)
    async with database_sessions() as session:
        async with session.begin():
            user = await demo_seed.provision_user(
                session,
                email=values["DEMO_LAB_OPERATOR_EMAIL"],
                display_name="Synthetic Demo Lab Operator",
                password=values["DEMO_LAB_OPERATOR_PASSWORD"],
                role="LAB_OPERATOR",
                organization_code="CENTRAL_LAB",
            )
            laboratory = await session.scalar(
                select(Organization).where(Organization.code == "CENTRAL_LAB")
            )
            assert laboratory is not None
            session.add(
                Membership(
                    user_id=user.id,
                    organization_id=laboratory.id,
                    role="LAB_MANAGER",
                    is_active=True,
                )
            )
            await session.flush()

    with pytest.raises(demo_seed.ProvisioningError, match="conflicting active membership"):
        await demo_seed.ensure_demo_identities()

    async with database_sessions() as session:
        user = await session.scalar(
            select(User).where(User.email == values["DEMO_LAB_OPERATOR_EMAIL"])
        )
        laboratory = await session.scalar(
            select(Organization).where(Organization.code == "CENTRAL_LAB")
        )
        assert user is not None and user.is_active
        assert laboratory is not None
        memberships = list(
            await session.scalars(
                select(Membership).where(Membership.user_id == user.id).order_by(Membership.role)
            )
        )
        assert [
            (
                membership.role,
                membership.organization_id,
                membership.region_id,
                membership.is_active,
            )
            for membership in memberships
        ] == [
            ("LAB_MANAGER", laboratory.id, None, True),
            ("LAB_OPERATOR", laboratory.id, None, True),
        ]
        users = list(await session.scalars(select(User)))
        assert [stored_user.email for stored_user in users] == [values["DEMO_LAB_OPERATOR_EMAIL"]]
