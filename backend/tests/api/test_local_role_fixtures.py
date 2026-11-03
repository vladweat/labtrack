"""Repeatable local role setup uses only explicit environment credentials."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select

from app import cli
from app.core.security import verify_password
from app.db.session import get_session_factory
from app.modules.identity.local_role_fixtures import (
    LocalRoleProvisioningSummary,
    ensure_local_role_fixtures,
    load_local_role_specs,
    validate_local_role_target,
)
from app.modules.identity.models import Membership, Organization, Region, User
from app.modules.identity.provisioning import ProvisioningError, provision_user


def set_role_environment(
    monkeypatch, *, sender_code: str, lab_code: str, region_code: str
) -> dict[str, str]:
    values = {
        "LABTRACK_LOCAL_ROLE_FIXTURES": "1",
        "LABTRACK_LOCAL_REGION_CODE": region_code,
        "LABTRACK_LOCAL_SENDER_ORG_CODE": sender_code,
        "LABTRACK_LOCAL_LAB_ORG_CODE": lab_code,
        "LABTRACK_LOCAL_SENDER_EMAIL": "sender@example.test",
        "LABTRACK_LOCAL_SENDER_PASSWORD": "sender-secret-password-123",
        "LABTRACK_LOCAL_COURIER_EMAIL": "courier@example.test",
        "LABTRACK_LOCAL_COURIER_PASSWORD": "courier-secret-password-123",
        "LABTRACK_LOCAL_LAB_OPERATOR_EMAIL": "lab-operator@example.test",
        "LABTRACK_LOCAL_LAB_OPERATOR_PASSWORD": "lab-operator-secret-123",
        "LABTRACK_LOCAL_LAB_MANAGER_EMAIL": "lab-manager@example.test",
        "LABTRACK_LOCAL_LAB_MANAGER_PASSWORD": "lab-manager-secret-123",
        "LABTRACK_LOCAL_OPERATIONS_EMAIL": "private-ops@example.test",
        "LABTRACK_LOCAL_OPERATIONS_PASSWORD": "private-ops-secret-password-123",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    return values


async def create_fixture_scopes() -> tuple[str, str, str]:
    region_code = f"LOCAL-{uuid4().hex[:10]}"
    sender_code = f"LOCAL-SENDER-{uuid4().hex[:10]}"
    lab_code = f"LOCAL-LAB-{uuid4().hex[:10]}"
    async with get_session_factory()() as session:
        async with session.begin():
            region = Region(code=region_code, name="Local acceptance region")
            session.add(region)
            await session.flush()
            session.add_all(
                [
                    Organization(
                        region_id=region.id,
                        code=sender_code,
                        name="Synthetic Sender",
                        organization_type="HEALTHCARE_FACILITY",
                    ),
                    Organization(
                        region_id=region.id,
                        code=lab_code,
                        name="Synthetic Laboratory",
                        organization_type="LABORATORY",
                    ),
                ]
            )
    return region_code, sender_code, lab_code


@pytest.mark.asyncio
async def test_local_role_provisioning_is_repeatable_and_uses_env_passwords(monkeypatch) -> None:
    region_code, sender_code, lab_code = await create_fixture_scopes()
    environment = set_role_environment(
        monkeypatch,
        sender_code=sender_code,
        lab_code=lab_code,
        region_code=region_code,
    )
    specs = load_local_role_specs(environment)

    async with get_session_factory()() as session:
        async with session.begin():
            first = await ensure_local_role_fixtures(session, specs)
    async with get_session_factory()() as session:
        async with session.begin():
            second = await ensure_local_role_fixtures(session, specs)
        users = list(await session.scalars(select(User).order_by(User.email)))
        memberships = list(await session.scalars(select(Membership)))
        organizations = {
            organization.code: organization
            for organization in await session.scalars(
                select(Organization).where(Organization.code.in_([sender_code, lab_code]))
            )
        }
        region = await session.scalar(select(Region).where(Region.code == region_code))

    assert first == LocalRoleProvisioningSummary(created=5, updated=0, unchanged=0)
    assert second == LocalRoleProvisioningSummary(created=0, updated=0, unchanged=5)
    assert len(users) == 5
    assert len(memberships) == 5
    assert region is not None

    users_by_email = {user.email: user for user in users}
    users_by_id = {user.id: user for user in users}
    grants_by_email = {
        users_by_id[membership.user_id].email: membership for membership in memberships
    }
    expected = {
        "sender@example.test": ("SENDER_OPERATOR", organizations[sender_code].id, None),
        "courier@example.test": ("COURIER", None, region.id),
        "lab-operator@example.test": ("LAB_OPERATOR", organizations[lab_code].id, None),
        "lab-manager@example.test": ("LAB_MANAGER", organizations[lab_code].id, None),
        "private-ops@example.test": ("REGIONAL_OPERATIONS", None, region.id),
    }
    for email, (role, organization_id, region_id) in expected.items():
        membership = grants_by_email[email]
        assert membership.role == role
        assert membership.organization_id == organization_id
        assert membership.region_id == region_id
        assert verify_password(
            environment[f"LABTRACK_LOCAL_{role_to_env_key(role)}_PASSWORD"],
            users_by_email[email].password_hash,
        )
        assert (
            environment[f"LABTRACK_LOCAL_{role_to_env_key(role)}_PASSWORD"]
            not in users_by_email[email].password_hash
        )


def role_to_env_key(role: str) -> str:
    return {
        "SENDER_OPERATOR": "SENDER",
        "COURIER": "COURIER",
        "LAB_OPERATOR": "LAB_OPERATOR",
        "LAB_MANAGER": "LAB_MANAGER",
        "REGIONAL_OPERATIONS": "OPERATIONS",
    }[role]


@pytest.mark.asyncio
async def test_local_role_provisioning_refuses_conflicting_existing_memberships(
    monkeypatch,
) -> None:
    region_code, sender_code, lab_code = await create_fixture_scopes()
    environment = set_role_environment(
        monkeypatch,
        sender_code=sender_code,
        lab_code=lab_code,
        region_code=region_code,
    )
    specs = load_local_role_specs(environment)
    other_sender_code = f"LOCAL-OTHER-{uuid4().hex[:10]}"
    async with get_session_factory()() as session:
        region = await session.scalar(select(Region).where(Region.code == region_code))
        assert region is not None
        session.add(
            Organization(
                region_id=region.id,
                code=other_sender_code,
                name="Other local Sender",
                organization_type="HEALTHCARE_FACILITY",
            )
        )
        await session.commit()
        async with session.begin():
            await provision_user(
                session,
                email=environment["LABTRACK_LOCAL_SENDER_EMAIL"],
                display_name="Existing Sender",
                password="existing-local-password-123",
                role="SENDER_OPERATOR",
                organization_code=other_sender_code,
            )

        with pytest.raises(ProvisioningError, match="conflicting role or scope") as error:
            async with session.begin():
                await ensure_local_role_fixtures(session, specs)

        assert environment["LABTRACK_LOCAL_SENDER_EMAIL"] not in str(error.value)
        membership = await session.scalar(
            select(Membership)
            .join(User)
            .where(User.email == environment["LABTRACK_LOCAL_SENDER_EMAIL"])
        )
        other_sender = await session.scalar(
            select(Organization).where(Organization.code == other_sender_code)
        )
        assert membership is not None and other_sender is not None
        assert membership.organization_id == other_sender.id


def test_local_role_guard_requires_explicit_flag_local_db_and_local_host() -> None:
    settings = SimpleNamespace(
        app_env="test", postgres_db="labtrack_stagei_test", postgres_host="127.0.0.1"
    )
    with pytest.raises(ValueError, match="LABTRACK_LOCAL_ROLE_FIXTURES=1"):
        validate_local_role_target(settings, {"LABTRACK_LOCAL_ROLE_FIXTURES": "0"})
    with pytest.raises(ValueError, match="local or test database"):
        validate_local_role_target(
            SimpleNamespace(
                app_env="development", postgres_db="labtrack_demo_test", postgres_host="127.0.0.1"
            ),
            {"LABTRACK_LOCAL_ROLE_FIXTURES": "1"},
        )
    with pytest.raises(ValueError, match="development or test environment"):
        validate_local_role_target(
            SimpleNamespace(
                app_env="production",
                postgres_db="labtrack_local_acceptance",
                postgres_host="127.0.0.1",
            ),
            {"LABTRACK_LOCAL_ROLE_FIXTURES": "1"},
        )
    with pytest.raises(ValueError, match="loopback or Unix socket"):
        validate_local_role_target(
            SimpleNamespace(
                app_env="development",
                postgres_db="labtrack_local_acceptance",
                postgres_host="db.example.test",
            ),
            {"LABTRACK_LOCAL_ROLE_FIXTURES": "1"},
        )
    validate_local_role_target(
        SimpleNamespace(
            app_env="test", postgres_db="labtrack_stagei_test", postgres_host="/tmp/local-pg-socket"
        ),
        {"LABTRACK_LOCAL_ROLE_FIXTURES": "1"},
    )


def test_local_role_cli_output_never_contains_env_passwords(monkeypatch, capsys) -> None:
    values = set_role_environment(
        monkeypatch,
        sender_code="LOCAL-SENDER",
        lab_code="LOCAL-LAB",
        region_code="LOCAL-REGION",
    )
    monkeypatch.setattr(
        cli,
        "get_settings",
        lambda: SimpleNamespace(
            app_env="test", postgres_db="labtrack_stagei_test", postgres_host="localhost"
        ),
    )

    async def ensure_without_database(_specs):
        return LocalRoleProvisioningSummary(created=5, updated=0, unchanged=0)

    monkeypatch.setattr(cli, "_execute_local_role_fixtures", ensure_without_database)
    assert cli.main(["provision-local-roles"]) == 0

    output = capsys.readouterr().out
    assert "5" in output
    assert all(
        password not in output for key, password in values.items() if key.endswith("_PASSWORD")
    )
    assert values["LABTRACK_LOCAL_OPERATIONS_EMAIL"] not in output
