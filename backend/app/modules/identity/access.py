"""Authenticated principal and fail-closed role dependencies."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.security import decode_access_token
from app.db.session import get_db_session
from app.modules.identity.models import Membership, Organization, Region, User

_bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class MembershipGrant:
    role: str
    organization_id: UUID | None
    region_id: UUID


@dataclass(frozen=True)
class Principal:
    user_id: UUID
    email: str
    display_name: str
    memberships: tuple[MembershipGrant, ...]

    def has_role(
        self,
        role: str,
        *,
        organization_id: UUID | None = None,
        region_id: UUID | None = None,
    ) -> bool:
        return any(
            grant.role == role
            and (organization_id is None or grant.organization_id == organization_id)
            and (region_id is None or grant.region_id == region_id)
            for grant in self.memberships
        )


async def get_current_principal(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Principal:
    """Validate the token and reload account and active scopes on every request."""

    unauthorized = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired access token",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if credentials is None:
        raise unauthorized
    try:
        user_id = decode_access_token(credentials.credentials, settings)
    except (jwt.InvalidTokenError, ValueError):
        raise unauthorized from None

    user = await session.scalar(select(User).where(User.id == user_id, User.is_active.is_(True)))
    if user is None:
        raise unauthorized

    rows = await session.execute(
        select(Membership, Organization, Region)
        .outerjoin(Organization, Membership.organization_id == Organization.id)
        .outerjoin(Region, Membership.region_id == Region.id)
        .where(Membership.user_id == user.id, Membership.is_active.is_(True))
    )
    grants: list[MembershipGrant] = []
    for membership, organization, direct_region in rows:
        if membership.organization_id is not None:
            if organization is None or not organization.is_active:
                continue
            region_id = organization.region_id
            active_region = await session.scalar(
                select(Region.id).where(Region.id == region_id, Region.is_active.is_(True))
            )
        else:
            if direct_region is None or not direct_region.is_active:
                continue
            region_id = direct_region.id
            active_region = region_id
        if active_region is not None:
            grants.append(MembershipGrant(membership.role, membership.organization_id, region_id))

    return Principal(user.id, user.email, user.display_name, tuple(grants))


def require_roles(*roles: str) -> Callable[..., Principal]:
    """Require one current role; resource ownership and exact scope remain use-case checks."""

    async def dependency(
        principal: Annotated[Principal, Depends(get_current_principal)],
    ) -> Principal:
        if not any(principal.has_role(role) for role in roles):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient role")
        return principal

    return dependency
