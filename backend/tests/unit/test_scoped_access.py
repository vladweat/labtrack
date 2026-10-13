from typing import Annotated
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import Depends, FastAPI

from app.modules.identity.access import (
    MembershipGrant,
    Principal,
    get_current_principal,
    require_scoped_role,
)


@pytest.mark.asyncio
async def test_scoped_role_masks_another_organization_as_not_found() -> None:
    allowed_organization = uuid4()
    another_organization = uuid4()
    principal = Principal(
        user_id=uuid4(),
        email="member@example.test",
        display_name="Member",
        memberships=(
            MembershipGrant(
                role="LAB_MANAGER",
                organization_id=allowed_organization,
                region_id=uuid4(),
            ),
        ),
    )
    app = FastAPI()
    app.dependency_overrides[get_current_principal] = lambda: principal

    @app.get("/organizations/{organization_id}/private")
    async def read_private_resource(
        organization_id: UUID,
        _: Annotated[
            Principal,
            Depends(
                require_scoped_role(
                    "LAB_MANAGER",
                    scope_parameter="organization_id",
                )
            ),
        ],
    ) -> dict[str, str]:
        return {"organization_id": str(organization_id)}

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://labtrack.test") as client:
        allowed = await client.get(f"/organizations/{allowed_organization}/private")
        denied = await client.get(f"/organizations/{another_organization}/private")

    assert allowed.status_code == 200
    assert denied.status_code == 404


@pytest.mark.asyncio
async def test_region_role_does_not_grant_organization_scoped_access() -> None:
    organization_id = uuid4()
    principal = Principal(
        user_id=uuid4(),
        email="analyst@example.test",
        display_name="Analyst",
        memberships=(MembershipGrant("REGIONAL_ANALYST", None, uuid4()),),
    )
    app = FastAPI()
    app.dependency_overrides[get_current_principal] = lambda: principal

    @app.get("/organizations/{organization_id}/private")
    async def read_private_resource(
        organization_id: UUID,
        _: Annotated[
            Principal,
            Depends(
                require_scoped_role(
                    "LAB_MANAGER",
                    scope_parameter="organization_id",
                )
            ),
        ],
    ) -> dict[str, str]:
        return {"organization_id": str(organization_id)}

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://labtrack.test") as client:
        response = await client.get(f"/organizations/{organization_id}/private")

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_regional_role_is_limited_to_its_region() -> None:
    allowed_region = uuid4()
    another_region = uuid4()
    principal = Principal(
        user_id=uuid4(),
        email="ops@example.test",
        display_name="Regional operations",
        memberships=(MembershipGrant("REGIONAL_OPERATIONS", None, allowed_region),),
    )
    app = FastAPI()
    app.dependency_overrides[get_current_principal] = lambda: principal

    @app.get("/regions/{region_id}/private")
    async def read_private_resource(
        region_id: UUID,
        _: Annotated[
            Principal,
            Depends(
                require_scoped_role(
                    "REGIONAL_OPERATIONS",
                    scope_parameter="region_id",
                )
            ),
        ],
    ) -> dict[str, str]:
        return {"region_id": str(region_id)}

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://labtrack.test") as client:
        allowed = await client.get(f"/regions/{allowed_region}/private")
        denied = await client.get(f"/regions/{another_region}/private")

    assert allowed.status_code == 200
    assert denied.status_code == 404
