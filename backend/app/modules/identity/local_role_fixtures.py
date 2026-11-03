"""Guarded, repeatable provisioning for explicitly local Stage I role fixtures."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password, verify_password
from app.modules.identity.models import Membership, User
from app.modules.identity.provisioning import (
    ProvisioningError,
    _resolve_membership_scope,
    normalize_email,
)


@dataclass(frozen=True)
class LocalRoleSpec:
    """One local account and its single intended membership."""

    key: str
    email: str
    password: str = field(repr=False)
    role: str
    display_name: str
    organization_code: str | None = None
    region_code: str | None = None


@dataclass(frozen=True)
class LocalRoleProvisioningSummary:
    created: int = 0
    updated: int = 0
    unchanged: int = 0

    @property
    def total(self) -> int:
        return self.created + self.updated + self.unchanged


def validate_local_role_target(settings: object, environ: Mapping[str, str]) -> None:
    """Require an explicit opt-in and local-only application/database target."""

    if environ.get("LABTRACK_LOCAL_ROLE_FIXTURES") != "1":
        raise ProvisioningError("Set LABTRACK_LOCAL_ROLE_FIXTURES=1 to opt in")

    app_env = str(getattr(settings, "app_env", "")).strip().casefold()
    if app_env not in {"development", "dev", "test"}:
        raise ProvisioningError("Local role fixtures require a development or test environment")

    database = str(getattr(settings, "postgres_db", "")).strip().casefold()
    database_parts = database.replace("-", "_").split("_")
    if {"demo", "prod", "production", "live"}.intersection(database_parts):
        raise ProvisioningError("Local role fixtures require a local or test database name")
    if not (database.startswith("labtrack_local_") or "test" in database):
        raise ProvisioningError("Local role fixtures require a local or test database name")

    host = str(getattr(settings, "postgres_host", "")).strip().casefold()
    if host not in {"localhost", "127.0.0.1", "::1"} and not Path(host).is_absolute():
        raise ProvisioningError("Local role fixtures require a loopback or Unix socket host")


def _required(environ: Mapping[str, str], key: str) -> str:
    value = environ.get(key)
    if value is None or not value.strip():
        raise ProvisioningError(f"Required environment variable is missing: {key}")
    return value if key.endswith("_PASSWORD") else value.strip()


def load_local_role_specs(environ: Mapping[str, str]) -> tuple[LocalRoleSpec, ...]:
    """Read the five local credentials and scope codes from environment only."""

    region_code = _required(environ, "LABTRACK_LOCAL_REGION_CODE")
    sender_code = _required(environ, "LABTRACK_LOCAL_SENDER_ORG_CODE")
    lab_code = _required(environ, "LABTRACK_LOCAL_LAB_ORG_CODE")
    definitions = (
        ("SENDER", "Stage I Sender", "SENDER_OPERATOR", sender_code, None),
        ("COURIER", "Stage I Courier", "COURIER", None, region_code),
        ("LAB_OPERATOR", "Stage I Lab Operator", "LAB_OPERATOR", lab_code, None),
        ("LAB_MANAGER", "Stage I Lab Manager", "LAB_MANAGER", lab_code, None),
        ("OPERATIONS", "Private Local Operations", "REGIONAL_OPERATIONS", None, region_code),
    )

    specs: list[LocalRoleSpec] = []
    for key, display_name, role, organization_code, scoped_region in definitions:
        email = _required(environ, f"LABTRACK_LOCAL_{key}_EMAIL")
        password = _required(environ, f"LABTRACK_LOCAL_{key}_PASSWORD")
        normalized_email = normalize_email(email)
        if not 12 <= len(password) <= 256:
            raise ProvisioningError(
                f"Password for local role {key} must contain 12 to 256 characters"
            )
        specs.append(
            LocalRoleSpec(
                key=key,
                email=normalized_email,
                password=password,
                role=role,
                display_name=display_name,
                organization_code=organization_code,
                region_code=scoped_region,
            )
        )
    return tuple(specs)


async def ensure_local_role_fixtures(
    session: AsyncSession,
    specs: tuple[LocalRoleSpec, ...],
) -> LocalRoleProvisioningSummary:
    """Create or refresh the five exact local users/grants, refusing conflicts."""

    created = 0
    updated = 0
    unchanged = 0
    for spec in specs:
        organization, region = await _resolve_membership_scope(
            session,
            role=spec.role,
            organization_code=spec.organization_code,
            region_code=spec.region_code,
        )
        user = await session.scalar(select(User).where(User.email == spec.email).with_for_update())
        if user is None:
            user = User(
                email=spec.email,
                display_name=spec.display_name,
                password_hash=hash_password(spec.password),
            )
            session.add(user)
            await session.flush()
            session.add(
                Membership(
                    user_id=user.id,
                    organization_id=organization.id if organization else None,
                    region_id=region.id if region else None,
                    role=spec.role,
                )
            )
            await session.flush()
            created += 1
            continue

        if not user.is_active:
            raise ProvisioningError("A local role account is inactive")

        memberships = list(
            await session.scalars(
                select(Membership).where(Membership.user_id == user.id).with_for_update()
            )
        )
        if len(memberships) != 1:
            raise ProvisioningError("An existing local role account has conflicting memberships")
        membership = memberships[0]
        matching_scope = membership.organization_id == (
            organization.id if organization else None
        ) and membership.region_id == (region.id if region else None)
        if membership.role != spec.role or not matching_scope or not membership.is_active:
            raise ProvisioningError(
                "An existing local role account has a conflicting role or scope"
            )

        changed = False
        if user.display_name != spec.display_name:
            user.display_name = spec.display_name
            changed = True
        if not verify_password(spec.password, user.password_hash):
            user.password_hash = hash_password(spec.password)
            changed = True
        if changed:
            updated += 1
        else:
            unchanged += 1

    return LocalRoleProvisioningSummary(created, updated, unchanged)
