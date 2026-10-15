from uuid import uuid4

import pytest
from sqlalchemy import select

from app.core.security import verify_password
from app.db.session import get_session_factory
from app.modules.identity.models import Membership, Organization, Region
from app.modules.identity.provisioning import (
    ProvisioningError,
    grant_membership,
    provision_user,
)


async def create_organization(
    *, active: bool = True, organization_type: str = "HEALTHCARE_FACILITY"
) -> Organization:
    async with get_session_factory()() as session:
        region = Region(code=f"PROV-{uuid4().hex[:8]}", name="Provisioning region")
        organization = Organization(
            region=region,
            code=f"PROV-ORG-{uuid4().hex[:8]}",
            name="Provisioning organization",
            organization_type=organization_type,
            is_active=active,
        )
        session.add(organization)
        await session.commit()
        return organization


@pytest.mark.asyncio
async def test_provision_user_normalizes_email_hashes_password_and_assigns_scope() -> None:
    organization = await create_organization()
    async with get_session_factory()() as session:
        async with session.begin():
            user = await provision_user(
                session,
                email="  SENDER@Example.Test ",
                display_name=" Sender Operator ",
                password="a sufficiently long test password",
                role="SENDER_OPERATOR",
                organization_code=organization.code,
            )
            memberships = await session.scalars(
                select(Membership).where(Membership.user_id == user.id)
            )
            membership = memberships.one()

    assert user.email == "sender@example.test"
    assert user.display_name == "Sender Operator"
    assert verify_password("a sufficiently long test password", user.password_hash)
    assert membership.organization_id == organization.id
    assert membership.region_id is None
    assert membership.role == "SENDER_OPERATOR"


@pytest.mark.asyncio
async def test_provision_user_rejects_role_with_wrong_scope() -> None:
    organization = await create_organization()
    async with get_session_factory()() as session:
        with pytest.raises(ProvisioningError, match="requires a region scope"):
            await provision_user(
                session,
                email="courier@example.test",
                display_name="Courier",
                password="a sufficiently long test password",
                role="COURIER",
                organization_code=organization.code,
            )


@pytest.mark.asyncio
async def test_provision_user_rejects_inactive_organization() -> None:
    organization = await create_organization(active=False)
    async with get_session_factory()() as session:
        with pytest.raises(ProvisioningError, match="active organization"):
            await provision_user(
                session,
                email="sender@example.test",
                display_name="Sender",
                password="a sufficiently long test password",
                role="SENDER_OPERATOR",
                organization_code=organization.code,
            )


@pytest.mark.asyncio
async def test_provision_user_rejects_role_for_wrong_organization_type() -> None:
    organization = await create_organization()
    async with get_session_factory()() as session:
        with pytest.raises(ProvisioningError, match="requires a LABORATORY organization"):
            await provision_user(
                session,
                email="manager@example.test",
                display_name="Manager",
                password="a sufficiently long test password",
                role="LAB_MANAGER",
                organization_code=organization.code,
            )


@pytest.mark.asyncio
async def test_provision_user_rejects_duplicate_email_case_insensitively() -> None:
    organization = await create_organization()
    async with get_session_factory()() as session:
        async with session.begin():
            await provision_user(
                session,
                email="sender@example.test",
                display_name="Sender",
                password="a sufficiently long test password",
                role="SENDER_OPERATOR",
                organization_code=organization.code,
            )
        with pytest.raises(ProvisioningError, match="already exists"):
            await provision_user(
                session,
                email=" SENDER@EXAMPLE.TEST ",
                display_name="Duplicate",
                password="a sufficiently long test password",
                role="SENDER_OPERATOR",
                organization_code=organization.code,
            )


@pytest.mark.asyncio
async def test_grant_membership_adds_distinct_region_scope_to_existing_user() -> None:
    organization = await create_organization(organization_type="LABORATORY")
    async with get_session_factory()() as session:
        async with session.begin():
            user = await provision_user(
                session,
                email="courier@example.test",
                display_name="Courier",
                password="a sufficiently long test password",
                role="LAB_OPERATOR",
                organization_code=organization.code,
            )
            region = await session.scalar(select(Region).where(Region.id == organization.region_id))
            assert region is not None
            membership = await grant_membership(
                session,
                email=user.email,
                role="COURIER",
                region_code=region.code,
            )

    assert membership.user_id == user.id
    assert membership.region_id == organization.region_id
    assert membership.organization_id is None


@pytest.mark.asyncio
async def test_grant_membership_rejects_duplicate_grant() -> None:
    organization = await create_organization(organization_type="LABORATORY")
    async with get_session_factory()() as session:
        async with session.begin():
            await provision_user(
                session,
                email="manager@example.test",
                display_name="Manager",
                password="a sufficiently long test password",
                role="LAB_MANAGER",
                organization_code=organization.code,
            )
        with pytest.raises(ProvisioningError, match="membership already exists"):
            await grant_membership(
                session,
                email="manager@example.test",
                role="LAB_MANAGER",
                organization_code=organization.code,
            )
