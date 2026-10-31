from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.security import hash_password
from app.main import app
from app.modules.identity.models import Membership, Organization, Region, User


async def create_active_member(
    sessions: async_sessionmaker,
    *,
    email: str = "manager@example.test",
    role: str = "LAB_MANAGER",
) -> tuple[User, Organization]:
    async with sessions() as session:
        region = Region(code=f"R-{uuid4().hex[:8]}", name="Test region")
        organization = Organization(
            region=region,
            code=f"O-{uuid4().hex[:8]}",
            name="Test laboratory",
            organization_type="LABORATORY",
        )
        user = User(
            email=email,
            display_name="Test user",
            password_hash=hash_password("correct horse battery staple"),
        )
        session.add_all(
            [
                region,
                organization,
                user,
                Membership(user=user, organization=organization, role=role),
            ]
        )
        await session.commit()
        await session.refresh(user)
        await session.refresh(organization)
        return user, organization


@pytest.mark.asyncio
async def test_login_and_current_user_return_active_membership(database_sessions) -> None:
    user, organization = await create_active_member(database_sessions)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://labtrack.test") as client:
        login = await client.post(
            "/api/v1/auth/token",
            json={"email": user.email.upper(), "password": "correct horse battery staple"},
        )
        token = login.json()["access_token"]
        response = await client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert login.status_code == 200
    assert login.json()["token_type"] == "bearer"
    assert login.json()["expires_in"] == 900
    assert response.status_code == 200
    assert response.json() == {
        "id": str(user.id),
        "email": user.email,
        "display_name": "Test user",
        "memberships": [
            {
                "role": "LAB_MANAGER",
                "organization_id": str(organization.id),
                "region_id": str(organization.region_id),
            }
        ],
    }


@pytest.mark.asyncio
async def test_invalid_password_and_unknown_email_share_generic_unauthorized(
    database_sessions,
) -> None:
    await create_active_member(database_sessions)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://labtrack.test") as client:
        wrong_password = await client.post(
            "/api/v1/auth/token",
            json={"email": "manager@example.test", "password": "wrong"},
        )
        unknown_user = await client.post(
            "/api/v1/auth/token",
            json={"email": "missing@example.test", "password": "wrong"},
        )

    assert wrong_password.status_code == unknown_user.status_code == 401
    wrong_error = wrong_password.json()["error"]
    unknown_error = unknown_user.json()["error"]
    assert {key: value for key, value in wrong_error.items() if key != "request_id"} == {
        key: value for key, value in unknown_error.items() if key != "request_id"
    }


@pytest.mark.asyncio
async def test_deactivated_user_cannot_reuse_an_issued_token(database_sessions) -> None:
    user, _ = await create_active_member(database_sessions)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://labtrack.test") as client:
        login = await client.post(
            "/api/v1/auth/token",
            json={"email": user.email, "password": "correct horse battery staple"},
        )
        token = login.json()["access_token"]
        async with database_sessions() as session:
            stored_user = await session.scalar(select(User).where(User.id == user.id))
            stored_user.is_active = False
            await session.commit()
        response = await client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_revoked_membership_disappears_from_existing_token(database_sessions) -> None:
    user, _ = await create_active_member(database_sessions)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://labtrack.test") as client:
        login = await client.post(
            "/api/v1/auth/token",
            json={"email": user.email, "password": "correct horse battery staple"},
        )
        token = login.json()["access_token"]
        async with database_sessions() as session:
            membership = await session.scalar(
                select(Membership).where(Membership.user_id == user.id)
            )
            membership.is_active = False
            await session.commit()
        response = await client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    assert response.json()["memberships"] == []


@pytest.mark.asyncio
async def test_database_migration_revision_is_applied(database_sessions) -> None:
    async with database_sessions() as session:
        revision = await session.scalar(text("SELECT version_num FROM alembic_version"))

    assert revision == "0009_stage_g_offline"
