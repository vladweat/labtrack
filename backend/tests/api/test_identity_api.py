from uuid import UUID

import pytest
from fastapi import APIRouter, Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import update

from app.core.security import hash_password
from app.db.session import get_session_factory
from app.main import app
from app.modules.identity.access import require_scoped_role
from app.modules.identity.models import Membership, Organization, Region, User

PASSWORD_HASH = hash_password("Foundation Test Password 2026!")


@pytest.fixture
def scoped_app() -> FastAPI:
    router = APIRouter()

    @router.get(
        "/organizations/{organization_id}/restricted",
        dependencies=[
            Depends(
                require_scoped_role(
                    "LAB_MANAGER",
                    scope_parameter="organization_id",
                )
            )
        ],
    )
    async def restricted_organization(organization_id: UUID) -> dict[str, str]:
        return {"organization_id": str(organization_id)}

    application = FastAPI()
    application.include_router(router)
    return application


async def create_member(
    email: str,
    *,
    role: str = "LAB_MANAGER",
    membership_active: bool = True,
    organization_active: bool = True,
    region_active: bool = True,
) -> tuple[UUID, UUID]:
    async with get_session_factory()() as session:
        region = Region(code=f"R-{email[:8]}", name="Test region", is_active=region_active)
        organization = Organization(
            region=region,
            code=f"O-{email[:8]}",
            name="Test laboratory",
            organization_type="LABORATORY",
            is_active=organization_active,
        )
        user = User(
            email=email,
            display_name="Test User",
            password_hash=PASSWORD_HASH,
            is_active=True,
        )
        session.add_all([region, organization, user])
        await session.flush()
        session.add(
            Membership(
                user_id=user.id,
                organization_id=organization.id,
                role=role,
                is_active=membership_active,
            )
        )
        await session.commit()
        return user.id, organization.id


async def login(client: AsyncClient, email: str) -> str:
    response = await client.post(
        "/api/v1/auth/token",
        json={"email": email, "password": "Foundation Test Password 2026!"},
    )
    assert response.status_code == 200
    return response.json()["access_token"]


@pytest.mark.asyncio
async def test_login_and_me_return_only_the_authenticated_users_active_memberships() -> None:
    email = "first@example.test"
    _, organization_id = await create_member(email)
    await create_member("second@example.test")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = await login(client, email)
        response = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    data = response.json()
    assert data["email"] == email
    assert [grant["organization_id"] for grant in data["memberships"]] == [str(organization_id)]


@pytest.mark.asyncio
async def test_wrong_password_and_missing_bearer_are_rejected() -> None:
    email = "member@example.test"
    await create_member(email)

    from app.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        bad_login = await client.post(
            "/api/v1/auth/token",
            json={"email": email, "password": "wrong"},
        )
        missing_bearer = await client.get("/api/v1/auth/me")

    assert bad_login.status_code == 401
    assert bad_login.json()["error"]["message"] == "Invalid email or password"
    assert missing_bearer.status_code == 401


@pytest.mark.asyncio
async def test_inactive_membership_is_not_returned_or_authorized(scoped_app: FastAPI) -> None:
    email = "inactive-grant@example.test"
    _, organization_id = await create_member(email, membership_active=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as auth_client:
        token = await login(auth_client, email)
        current = await auth_client.get(
            "/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"}
        )
    async with AsyncClient(
        transport=ASGITransport(app=scoped_app), base_url="http://test"
    ) as scoped_client:
        denied = await scoped_client.get(
            f"/organizations/{organization_id}/restricted",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert current.status_code == 200
    assert current.json()["memberships"] == []
    assert denied.status_code == 404


@pytest.mark.asyncio
async def test_inactive_account_invalidates_an_already_issued_token() -> None:
    email = "deactivated@example.test"
    user_id, _ = await create_member(email)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = await login(client, email)
        async with get_session_factory()() as session:
            await session.execute(update(User).where(User.id == user_id).values(is_active=False))
            await session.commit()
        response = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_user_cannot_use_role_from_another_organization(scoped_app: FastAPI) -> None:
    email = "manager@example.test"
    _, organization_id = await create_member(email)
    _, foreign_organization_id = await create_member("foreign@example.test")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as auth_client:
        token = await login(auth_client, email)
    async with AsyncClient(
        transport=ASGITransport(app=scoped_app), base_url="http://test"
    ) as client:
        allowed = await client.get(
            f"/organizations/{organization_id}/restricted",
            headers={"Authorization": f"Bearer {token}"},
        )
        denied = await client.get(
            f"/organizations/{foreign_organization_id}/restricted",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert allowed.status_code == 200
    assert denied.status_code == 404


@pytest.mark.asyncio
async def test_live_and_ready_health_routes_report_process_and_postgresql() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        live = await client.get("/health/live")
        ready = await client.get("/health/ready")

    assert live.status_code == 200
    assert live.json() == {"status": "ok"}
    assert ready.status_code == 200
    assert ready.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_request_id_is_returned_and_invalid_request_id_is_replaced() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        accepted = await client.get("/health/live", headers={"X-Request-ID": "trace-1234"})
        replaced = await client.get("/health/live", headers={"X-Request-ID": "bad token"})

    accepted_request_id = accepted.headers["X-Request-ID"]
    replaced_request_id = replaced.headers["X-Request-ID"]
    UUID(accepted_request_id)
    UUID(replaced_request_id)
    assert accepted_request_id != "trace-1234"
    assert replaced_request_id != "bad token"
    assert accepted_request_id != replaced_request_id
