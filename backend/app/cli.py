"""Operator commands for identity provisioning."""

import argparse
import asyncio
import getpass
import os
import sys
from uuid import UUID

from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.core.config import get_settings
from app.core.demo_seed import DemoSeedNotAllowed
from app.db.session import dispose_engine, get_session_factory
from app.modules.idempotency.service import compact_offline_replays
from app.modules.identity.local_role_fixtures import (
    LocalRoleProvisioningSummary,
    LocalRoleSpec,
    ensure_local_role_fixtures,
    load_local_role_specs,
    validate_local_role_target,
)
from app.modules.identity.provisioning import (
    ALL_ROLES,
    ProvisioningError,
    grant_membership,
    provision_user,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser("provision-user", help="create a user and initial membership")
    create.add_argument("--email", required=True)
    create.add_argument("--display-name", required=True)
    create.add_argument("--role", required=True, choices=sorted(ALL_ROLES))
    _add_scope_arguments(create)

    grant = commands.add_parser("grant-membership", help="add a scoped role to an existing user")
    grant.add_argument("--email", required=True)
    grant.add_argument("--role", required=True, choices=sorted(ALL_ROLES))
    _add_scope_arguments(grant)
    commands.add_parser(
        "provision-local-roles",
        help="ensure local Stage I role fixtures from explicitly enabled environment variables",
    )
    commands.add_parser(
        "compact-offline-replays", help="compact Courier replay detail older than 90 days"
    )
    commands.add_parser(
        "seed-demo",
        help="seed synthetic Sender, Courier, Lab, and Shipment data in explicit demo mode",
    )
    return parser


async def _execute_local_role_fixtures(
    specs: tuple[LocalRoleSpec, ...],
) -> LocalRoleProvisioningSummary:
    try:
        async with get_session_factory()() as session:
            async with session.begin():
                return await ensure_local_role_fixtures(session, specs)
    finally:
        await dispose_engine()


def _add_scope_arguments(parser: argparse.ArgumentParser) -> None:
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--organization-code")
    scope.add_argument("--region-code")


def _prompt_password() -> str:
    if not sys.stdin.isatty():
        raise ProvisioningError("Password provisioning requires an interactive terminal")
    password = getpass.getpass("Initial password (input hidden): ")
    confirmation = getpass.getpass("Confirm password: ")
    if password != confirmation:
        raise ProvisioningError("Passwords do not match")
    if not 12 <= len(password) <= 256:
        raise ProvisioningError("Password must contain 12 to 256 characters")
    return password


async def _execute(args: argparse.Namespace, password: str | None) -> UUID | int:
    try:
        async with get_session_factory()() as session:
            async with session.begin():
                if args.command == "compact-offline-replays":
                    return await compact_offline_replays(session)
                if args.command == "provision-user":
                    assert password is not None
                    user = await provision_user(
                        session,
                        email=args.email,
                        display_name=args.display_name,
                        password=password,
                        role=args.role,
                        organization_code=args.organization_code,
                        region_code=args.region_code,
                    )
                    return user.id
                membership = await grant_membership(
                    session,
                    email=args.email,
                    role=args.role,
                    organization_code=args.organization_code,
                    region_code=args.region_code,
                )
                return membership.id
    finally:
        await dispose_engine()


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "seed-demo":
        from app.demo_seed import main as run_demo_seed

        try:
            return asyncio.run(run_demo_seed())
        except (DemoSeedNotAllowed, ProvisioningError, RuntimeError, KeyError) as error:
            print(f"Demo seed failed: {error}", file=sys.stderr)
            return 2
        except SQLAlchemyError:
            print("Demo seed failed: database operation failed", file=sys.stderr)
            return 2
    try:
        if args.command == "provision-local-roles":
            validate_local_role_target(get_settings(), os.environ)
            specs = load_local_role_specs(os.environ)
            summary = asyncio.run(_execute_local_role_fixtures(specs))
        else:
            password = _prompt_password() if args.command == "provision-user" else None
            resource_id = asyncio.run(_execute(args, password))
    except ProvisioningError as error:
        print(f"Provisioning failed: {error}", file=sys.stderr)
        return 2
    except IntegrityError:
        print("Provisioning failed: a database constraint rejected the request", file=sys.stderr)
        return 2
    except SQLAlchemyError:
        print("Provisioning failed: database operation failed", file=sys.stderr)
        return 2
    if args.command == "provision-user":
        print(f"User provisioned: {args.email.strip().casefold()} (id={resource_id})")
    elif args.command == "provision-local-roles":
        print(
            "Local role fixtures ensured: "
            f"{summary.total} accounts (created={summary.created}, "
            f"updated={summary.updated}, unchanged={summary.unchanged})."
        )
    elif args.command == "compact-offline-replays":
        print(f"Offline replay details compacted: {resource_id}")
    else:
        print(f"Membership granted (id={resource_id})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
