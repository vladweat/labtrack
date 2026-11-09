"""Seed the synthetic LabTrack Sender → Courier → Lab demonstration."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.demo_seed import (
    DemoSeedNotAllowed,
    require_demo_api_target,
    require_demo_seed_allowed,
)
from app.db.session import dispose_engine, get_session_factory
from app.modules.identity.models import Membership, Organization, Region, User
from app.modules.identity.provisioning import ProvisioningError, provision_user


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


def urlopen(request: Request, timeout: int):
    """Open a local demo API request without proxies or redirect following."""

    opener = build_opener(ProxyHandler({}), _RejectRedirects)
    return opener.open(request, timeout=timeout)


def required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} must be set")
    return value


class DemoApiRequestError(RuntimeError):
    def __init__(self, method: str, path: str, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"{method} {path} failed with HTTP {status_code}: {detail}")


async def ensure_demo_user(
    session: AsyncSession,
    *,
    email: str,
    display_name: str,
    password: str,
    role: str,
    organization_id: UUID | None = None,
    region_id: UUID | None = None,
    organization_code: str | None = None,
    region_code: str | None = None,
) -> User:
    normalized_email = email.strip().casefold()
    user = await session.scalar(select(User).where(User.email == normalized_email))
    if user is None:
        return await provision_user(
            session,
            email=normalized_email,
            display_name=display_name,
            password=password,
            role=role,
            organization_code=organization_code,
            region_code=region_code,
        )
    if not user.is_active:
        raise ProvisioningError(f"Demo account {normalized_email} is inactive")

    active_memberships = list(
        await session.scalars(
            select(Membership).where(
                Membership.user_id == user.id,
                Membership.is_active.is_(True),
            )
        )
    )
    expected_membership = any(
        membership.role == role
        and membership.organization_id == organization_id
        and membership.region_id == region_id
        for membership in active_memberships
    )
    if len(active_memberships) > 1:
        raise ProvisioningError(
            f"Existing account {normalized_email} has conflicting active memberships"
        )
    if not expected_membership:
        raise ProvisioningError(
            f"Existing account {normalized_email} does not have the required demo role"
        )
    return user


async def ensure_demo_identities() -> tuple[UUID, UUID, UUID, UUID]:
    require_demo_seed_allowed(get_settings())
    courier_email = required_env("DEMO_COURIER_EMAIL")
    courier_password = required_env("DEMO_COURIER_PASSWORD")
    sender_email = required_env("DEMO_SENDER_EMAIL")
    sender_password = required_env("DEMO_SENDER_PASSWORD")
    lab_operator_email = required_env("DEMO_LAB_OPERATOR_EMAIL")
    lab_operator_password = required_env("DEMO_LAB_OPERATOR_PASSWORD")
    lab_manager_email = required_env("DEMO_LAB_MANAGER_EMAIL")
    lab_manager_password = required_env("DEMO_LAB_MANAGER_PASSWORD")
    passwords = (
        courier_password,
        sender_password,
        lab_operator_password,
        lab_manager_password,
    )
    if any(not 12 <= len(password) <= 256 for password in passwords):
        raise RuntimeError("Demo passwords must contain 12 to 256 characters")

    async with get_session_factory()() as session:
        async with session.begin():
            region = await session.scalar(select(Region).where(Region.code == "PILOT"))
            laboratory = await session.scalar(
                select(Organization).where(Organization.code == "CENTRAL_LAB")
            )
            if region is None or not region.is_active:
                raise RuntimeError("The active PILOT region is required")
            if (
                laboratory is None
                or not laboratory.is_active
                or laboratory.organization_type != "LABORATORY"
                or laboratory.region_id != region.id
            ):
                raise RuntimeError("The active CENTRAL_LAB organization is required in PILOT")

            sender_org = await session.scalar(
                select(Organization).where(Organization.code == "DEMO_SENDER")
            )
            if sender_org is None:
                sender_org = Organization(
                    region_id=region.id,
                    code="DEMO_SENDER",
                    name="Synthetic Demo Sender",
                    organization_type="HEALTHCARE_FACILITY",
                )
                session.add(sender_org)
                await session.flush()
            elif (
                not sender_org.is_active
                or sender_org.organization_type != "HEALTHCARE_FACILITY"
                or sender_org.region_id != region.id
            ):
                raise RuntimeError("DEMO_SENDER exists with an unexpected scope or type")

            sender = await ensure_demo_user(
                session,
                email=sender_email,
                display_name="Synthetic Demo Sender",
                password=sender_password,
                role="SENDER_OPERATOR",
                organization_id=sender_org.id,
                organization_code=sender_org.code,
            )
            courier = await ensure_demo_user(
                session,
                email=courier_email,
                display_name="Synthetic Demo Courier",
                password=courier_password,
                role="COURIER",
                region_id=region.id,
                region_code=region.code,
            )
            await ensure_demo_user(
                session,
                email=lab_operator_email,
                display_name="Synthetic Demo Lab Operator",
                password=lab_operator_password,
                role="LAB_OPERATOR",
                organization_id=laboratory.id,
                organization_code=laboratory.code,
            )
            await ensure_demo_user(
                session,
                email=lab_manager_email,
                display_name="Synthetic Demo Lab Manager",
                password=lab_manager_password,
                role="LAB_MANAGER",
                organization_id=laboratory.id,
                organization_code=laboratory.code,
            )
            return sender.id, sender_org.id, courier.id, laboratory.id


def api_request(
    method: str,
    path: str,
    *,
    body: dict | None = None,
    token: str | None = None,
    idempotency_key: str | None = None,
) -> dict:
    base_url = os.environ.get("DEMO_API_URL", "http://api:8000").rstrip("/")
    require_demo_api_target(base_url)
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key

    request = Request(f"{base_url}{path}", data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=15) as response:
            return json.loads(response.read())
    except HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise DemoApiRequestError(method, path, error.code, detail) from error
    except URLError as error:
        raise RuntimeError(
            f"Could not reach the LabTrack API for {method} {path}: {error}"
        ) from error


def login(email: str, password: str) -> str:
    token_response = api_request(
        "POST", "/api/v1/auth/token", body={"email": email, "password": password}
    )
    return token_response["access_token"]


def create_demo_shipment(sender_org_id: UUID, courier_id: UUID, lab_id: UUID) -> str:
    sender_email = required_env("DEMO_SENDER_EMAIL")
    courier_email = required_env("DEMO_COURIER_EMAIL")
    sender_token = login(sender_email, required_env("DEMO_SENDER_PASSWORD"))
    courier_token = login(courier_email, required_env("DEMO_COURIER_PASSWORD"))

    sample = api_request(
        "POST",
        "/api/v1/samples",
        token=sender_token,
        idempotency_key="labtrack-public-demo-v1-sample",
        body={
            "origin_organization_id": str(sender_org_id),
            "material_type": "BLOOD",
            "collected_at": None,
        },
    )
    shipment = api_request(
        "POST",
        "/api/v1/shipments",
        token=sender_token,
        idempotency_key="labtrack-public-demo-v1-shipment",
        body={
            "sender_org_id": str(sender_org_id),
            "destination_org_id": str(lab_id),
            "expected_delivery_at": None,
        },
    )
    shipment_id = shipment["id"]
    current_shipment = api_request("GET", f"/api/v1/shipments/{shipment_id}", token=sender_token)
    if current_shipment["expected_delivery_at"] is None:
        expected_delivery_at = (datetime.now(UTC) + timedelta(days=1)).replace(
            hour=12, minute=0, second=0, microsecond=0
        )
        try:
            api_request(
                "PATCH",
                f"/api/v1/shipments/{shipment_id}",
                token=sender_token,
                idempotency_key="labtrack-public-demo-v1-delivery-date",
                body={"expected_delivery_at": expected_delivery_at.isoformat()},
            )
        except DemoApiRequestError as error:
            idempotency_conflict = (
                error.status_code == 409
                and "Idempotency-Key was already used with another request" in error.detail
            )
            if not idempotency_conflict:
                raise
            current_shipment = api_request(
                "GET", f"/api/v1/shipments/{shipment_id}", token=sender_token
            )
            if current_shipment["expected_delivery_at"] is None:
                raise
    api_request(
        "PATCH",
        f"/api/v1/shipments/{shipment_id}/courier",
        token=sender_token,
        idempotency_key="labtrack-public-demo-v1-assign-courier",
        body={"courier_user_id": str(courier_id)},
    )
    container = api_request(
        "POST",
        f"/api/v1/shipments/{shipment_id}/containers",
        token=sender_token,
        idempotency_key="labtrack-public-demo-v1-container",
    )
    api_request(
        "POST",
        f"/api/v1/shipments/{shipment_id}/containers/{container['id']}/items",
        token=sender_token,
        idempotency_key="labtrack-public-demo-v1-container-item",
        body={"sample_id": sample["id"]},
    )
    api_request(
        "POST",
        f"/api/v1/shipments/{shipment_id}/seal",
        token=sender_token,
        idempotency_key="labtrack-public-demo-v1-seal",
    )
    manifest = api_request(
        "GET",
        f"/api/v1/courier/shipments/{shipment_id}/offline-manifest",
        token=courier_token,
    )
    if manifest.get("status") != "READY_FOR_PICKUP":
        raise RuntimeError("Seeded Shipment is not in READY_FOR_PICKUP state")
    return shipment_id


async def main() -> int:
    require_demo_api_target(os.environ.get("DEMO_API_URL", "http://api:8000").rstrip("/"))
    try:
        _, sender_org_id, courier_id, lab_id = await ensure_demo_identities()
    finally:
        await dispose_engine()
    shipment_id = create_demo_shipment(sender_org_id, courier_id, lab_id)
    print("Synthetic Courier demo is ready.")
    print(f"Courier email: {required_env('DEMO_COURIER_EMAIL')}")
    print(f"Shipment ID: {shipment_id}")
    print("Share the Courier password separately; it is not included in this output.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except (DemoSeedNotAllowed, ProvisioningError, RuntimeError, KeyError) as error:
        print(f"Demo seed failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
