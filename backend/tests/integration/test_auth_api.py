import asyncio
from threading import Event
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

    assert revision == "0011_http_request_scope"


@pytest.fixture
def login_limiter(monkeypatch):
    from app.core.login_rate_limit import LoginAttemptLimiter

    limiter = LoginAttemptLimiter(limit=2, window_seconds=900, clock=lambda: 50)
    monkeypatch.setattr(app.state, "login_attempt_limiter", limiter, raising=False)
    return limiter


@pytest.mark.asyncio
async def test_failed_login_threshold_returns_documented_429_and_retry_after(
    database_sessions, login_limiter
) -> None:
    user, _ = await create_active_member(database_sessions)
    transport = httpx.ASGITransport(app=app, client=("192.0.2.1", 1234))
    async with httpx.AsyncClient(transport=transport, base_url="http://labtrack.test") as client:
        wrong_password = await client.post(
            "/api/v1/auth/token", json={"email": user.email, "password": "wrong"}
        )
        unknown_user = await client.post(
            "/api/v1/auth/token", json={"email": "missing@example.test", "password": "wrong"}
        )
        limited = await client.post(
            "/api/v1/auth/token",
            json={"email": user.email, "password": "correct horse battery staple"},
        )

    assert wrong_password.status_code == unknown_user.status_code == 401
    assert wrong_password.headers["WWW-Authenticate"] == "Bearer"
    assert wrong_password.json()["error"]["message"] == "Invalid email or password"
    assert limited.status_code == 429
    assert limited.headers["Retry-After"] == "900"
    assert limited.json()["error"] == {
        "code": "RATE_LIMITED",
        "message": "Too many failed login attempts. Try again later.",
        "details": {},
        "request_id": limited.headers["X-Request-ID"],
    }


@pytest.mark.asyncio
async def test_successful_and_malformed_requests_do_not_count_as_failed_authentication(
    database_sessions, login_limiter
) -> None:
    user, _ = await create_active_member(database_sessions)
    transport = httpx.ASGITransport(app=app, client=("192.0.2.2", 1234))
    async with httpx.AsyncClient(transport=transport, base_url="http://labtrack.test") as client:
        for _ in range(4):
            success = await client.post(
                "/api/v1/auth/token",
                json={"email": user.email, "password": "correct horse battery staple"},
            )
            assert success.status_code == 200
        for _ in range(4):
            malformed = await client.post("/api/v1/auth/token", json={"email": user.email})
            assert malformed.status_code == 422
        first_bad = await client.post(
            "/api/v1/auth/token", json={"email": user.email, "password": "wrong"}
        )
        second_bad = await client.post(
            "/api/v1/auth/token", json={"email": user.email, "password": "wrong"}
        )
        limited = await client.post(
            "/api/v1/auth/token", json={"email": user.email, "password": "wrong"}
        )

    assert first_bad.status_code == second_bad.status_code == 401
    assert limited.status_code == 429


@pytest.mark.asyncio
async def test_successful_login_clears_previous_failures(database_sessions, login_limiter) -> None:
    user, _ = await create_active_member(database_sessions)
    transport = httpx.ASGITransport(app=app, client=("192.0.2.3", 1234))
    async with httpx.AsyncClient(transport=transport, base_url="http://labtrack.test") as client:
        for _ in range(3):
            bad = await client.post(
                "/api/v1/auth/token", json={"email": user.email, "password": "wrong"}
            )
            assert bad.status_code == 401
            success = await client.post(
                "/api/v1/auth/token",
                json={"email": user.email, "password": "correct horse battery staple"},
            )
            assert success.status_code == 200


@pytest.mark.asyncio
async def test_limited_source_does_not_lock_another_source_out_of_shared_account(
    database_sessions, login_limiter
) -> None:
    user, _ = await create_active_member(database_sessions)
    transport = httpx.ASGITransport(app=app, client=("192.0.2.4", 1234))
    credentials = {"email": user.email, "password": "correct horse battery staple"}
    async with httpx.AsyncClient(transport=transport, base_url="http://labtrack.test") as client:
        for _ in range(2):
            bad = await client.post(
                "/api/v1/auth/token",
                headers={"X-Real-IP": "198.51.100.1"},
                json={"email": user.email, "password": "wrong"},
            )
            assert bad.status_code == 401
        limited = await client.post(
            "/api/v1/auth/token", headers={"X-Real-IP": "198.51.100.1"}, json=credentials
        )
        other_source = await client.post(
            "/api/v1/auth/token", headers={"X-Real-IP": "198.51.100.2"}, json=credentials
        )

    assert limited.status_code == 429
    assert other_source.status_code == 200


@pytest.mark.asyncio
async def test_parallel_bad_logins_enforce_source_limit_without_blocking_another_source(
    database_sessions, login_limiter, monkeypatch
) -> None:
    from app.modules.identity import router as identity_router

    user, _ = await create_active_member(database_sessions)
    first_verification_started = Event()
    release_failed_verifications = Event()
    verify_password = identity_router.verify_password

    def gated_password_verification(password: str, password_hash: str) -> bool:
        if password == "wrong":
            first_verification_started.set()
            if not release_failed_verifications.wait(timeout=10):
                raise TimeoutError("failed verification gate was not released")
        return verify_password(password, password_hash)

    monkeypatch.setattr(identity_router, "verify_password", gated_password_verification)
    transport = httpx.ASGITransport(app=app, client=("192.0.2.5", 1234))
    async with httpx.AsyncClient(transport=transport, base_url="http://labtrack.test") as client:
        bad_attempts = [
            asyncio.create_task(
                client.post(
                    "/api/v1/auth/token",
                    headers={"X-Real-IP": "198.51.100.10"},
                    json={"email": user.email, "password": "wrong"},
                )
            )
            for _ in range(6)
        ]
        try:
            assert await asyncio.to_thread(first_verification_started.wait, 5)
            other_source = await asyncio.wait_for(
                client.post(
                    "/api/v1/auth/token",
                    headers={"X-Real-IP": "198.51.100.11"},
                    json={"email": user.email, "password": "correct horse battery staple"},
                ),
                timeout=5,
            )
            assert other_source.status_code == 200
        finally:
            release_failed_verifications.set()
            responses = await asyncio.gather(*bad_attempts)

    assert sorted(response.status_code for response in responses) == [401, 401, 429, 429, 429, 429]
    assert all(
        response.headers["Retry-After"] == "900"
        for response in responses
        if response.status_code == 429
    )
