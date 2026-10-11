"""Authentication endpoints."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.security import create_access_token, verify_password
from app.db.session import get_db_session
from app.modules.identity.access import Principal, get_current_principal
from app.modules.identity.models import Membership, Organization, Region, User
from app.modules.identity.schemas import (
    AccessTokenResponse,
    CurrentUserResponse,
    LoginRequest,
    MembershipResponse,
)

router = APIRouter(prefix="/auth", tags=["authentication"])


@router.post("/token", response_model=AccessTokenResponse)
async def login(
    request: LoginRequest,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> AccessTokenResponse:
    """Exchange credentials for a short-lived access token."""

    normalized_email = request.email.strip().casefold()
    user = await session.scalar(select(User).where(User.email == normalized_email))
    valid_password = False
    if user is not None and user.is_active:
        valid_password = await run_in_threadpool(
            verify_password,
            request.password,
            user.password_hash,
        )
    if not valid_password:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token, expires_in = create_access_token(user.id, settings)
    return AccessTokenResponse(access_token=token, expires_in=expires_in)


@router.get("/me", response_model=CurrentUserResponse)
async def current_user(
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> CurrentUserResponse:
    """Return the authenticated account and currently active grants."""

    memberships = await session.execute(
        select(Membership, Organization, Region)
        .outerjoin(Organization, Membership.organization_id == Organization.id)
        .outerjoin(Region, Membership.region_id == Region.id)
        .where(Membership.user_id == principal.user_id, Membership.is_active.is_(True))
    )
    response_memberships = []
    for membership, organization, direct_region in memberships:
        if membership.organization_id is not None:
            if organization is None or not organization.is_active:
                continue
            region = await session.scalar(
                select(Region).where(
                    Region.id == organization.region_id,
                    Region.is_active.is_(True),
                )
            )
            region_id = region.id if region else None
        else:
            region_id = direct_region.id if direct_region and direct_region.is_active else None
        if region_id is not None:
            response_memberships.append(
                MembershipResponse(
                    role=membership.role,
                    organization_id=membership.organization_id,
                    region_id=region_id,
                )
            )
    return CurrentUserResponse(
        id=principal.user_id,
        email=principal.email,
        display_name=principal.display_name,
        memberships=response_memberships,
    )
