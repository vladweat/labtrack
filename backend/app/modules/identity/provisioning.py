"""Operator-controlled user and membership provisioning."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password
from app.modules.identity.models import Membership, Organization, Region, User

ORGANIZATION_ROLES = frozenset({"SENDER_OPERATOR", "LAB_OPERATOR", "LAB_MANAGER"})
REGION_ROLES = frozenset({"COURIER", "REGIONAL_OPERATIONS", "REGIONAL_ANALYST"})
ALL_ROLES = ORGANIZATION_ROLES | REGION_ROLES
ORGANIZATION_ROLE_TYPES = {
    "SENDER_OPERATOR": "HEALTHCARE_FACILITY",
    "LAB_OPERATOR": "LABORATORY",
    "LAB_MANAGER": "LABORATORY",
}


class ProvisioningError(ValueError):
    """Raised when an operator request violates provisioning rules."""


def normalize_email(email: str) -> str:
    normalized = email.strip().casefold()
    local, separator, domain = normalized.partition("@")
    if (
        not separator
        or not local
        or not domain
        or "@" in domain
        or any(character.isspace() for character in normalized)
        or "." not in domain
    ):
        raise ProvisioningError("A valid email address is required")
    return normalized


async def _resolve_membership_scope(
    session: AsyncSession,
    *,
    role: str,
    organization_code: str | None,
    region_code: str | None,
) -> tuple[Organization | None, Region | None]:
    if role not in ALL_ROLES:
        raise ProvisioningError(f"Unknown role: {role}")
    if bool(organization_code) == bool(region_code):
        raise ProvisioningError("Provide exactly one organization or region scope")
    if role in ORGANIZATION_ROLES:
        if organization_code is None:
            raise ProvisioningError(f"Role {role} requires an organization scope")
        organization = await session.scalar(
            select(Organization)
            .join(Region, Region.id == Organization.region_id)
            .where(
                Organization.code == organization_code.strip(),
                Organization.is_active.is_(True),
                Region.is_active.is_(True),
            )
        )
        if organization is None:
            raise ProvisioningError("An active organization in an active region is required")
        expected_type = ORGANIZATION_ROLE_TYPES[role]
        if organization.organization_type != expected_type:
            raise ProvisioningError(f"Role {role} requires a {expected_type} organization")
        return organization, None
    if region_code is None:
        raise ProvisioningError(f"Role {role} requires a region scope")
    region = await session.scalar(
        select(Region).where(Region.code == region_code.strip(), Region.is_active.is_(True))
    )
    if region is None:
        raise ProvisioningError("An active region is required")
    return None, region


async def provision_user(
    session: AsyncSession,
    *,
    email: str,
    display_name: str,
    password: str,
    role: str,
    organization_code: str | None = None,
    region_code: str | None = None,
) -> User:
    """Create a user and its initial scoped membership in the caller's transaction."""

    normalized_email = normalize_email(email)
    normalized_name = display_name.strip()
    if not normalized_name or len(normalized_name) > 160:
        raise ProvisioningError("Display name must contain 1 to 160 characters")
    if not 12 <= len(password) <= 256:
        raise ProvisioningError("Password must contain 12 to 256 characters")
    organization, region = await _resolve_membership_scope(
        session,
        role=role,
        organization_code=organization_code,
        region_code=region_code,
    )
    existing = await session.scalar(select(User.id).where(User.email == normalized_email))
    if existing is not None:
        raise ProvisioningError("A user with this email already exists")

    user = User(
        email=normalized_email,
        display_name=normalized_name,
        password_hash=hash_password(password),
    )
    session.add(user)
    await session.flush()
    session.add(
        Membership(
            user_id=user.id,
            organization_id=organization.id if organization else None,
            region_id=region.id if region else None,
            role=role,
        )
    )
    await session.flush()
    return user


async def grant_membership(
    session: AsyncSession,
    *,
    email: str,
    role: str,
    organization_code: str | None = None,
    region_code: str | None = None,
) -> Membership:
    """Add a scoped role to an existing active user in the caller's transaction."""

    normalized_email = normalize_email(email)
    user = await session.scalar(
        select(User).where(User.email == normalized_email, User.is_active.is_(True))
    )
    if user is None:
        raise ProvisioningError("An active user with this email is required")
    organization, region = await _resolve_membership_scope(
        session,
        role=role,
        organization_code=organization_code,
        region_code=region_code,
    )
    predicates = [Membership.user_id == user.id, Membership.role == role]
    predicates.append(
        Membership.organization_id == organization.id
        if organization
        else Membership.region_id == region.id
    )
    existing = await session.scalar(select(Membership.id).where(*predicates))
    if existing is not None:
        raise ProvisioningError("This membership already exists")

    membership = Membership(
        user_id=user.id,
        organization_id=organization.id if organization else None,
        region_id=region.id if region else None,
        role=role,
    )
    session.add(membership)
    await session.flush()
    return membership
