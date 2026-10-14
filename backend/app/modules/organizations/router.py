"""Read-only organization and reason-code catalogs."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db_session
from app.modules.identity.access import Principal, get_current_principal
from app.modules.identity.models import Organization
from app.modules.organizations.models import IssueReason

router = APIRouter(tags=["organizations"])


class OrganizationResponse(BaseModel):
    id: UUID
    region_id: UUID
    code: str
    name: str
    kind: str
    is_active: bool


class PagedOrganizationResponse(BaseModel):
    items: list[OrganizationResponse]
    total: int
    limit: int
    offset: int


@router.get("/organizations")
async def list_organizations(
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    region_id: UUID | None = None,
    kind: str | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> PagedOrganizationResponse:
    allowed_regions = {grant.region_id for grant in principal.memberships}
    query = select(Organization).where(
        Organization.is_active.is_(True), Organization.region_id.in_(allowed_regions)
    )
    if region_id is not None:
        query = query.where(Organization.region_id == region_id)
    if kind is not None:
        query = query.where(Organization.organization_type == kind)
    total = await session.scalar(select(func.count()).select_from(query.subquery()))
    rows = await session.scalars(query.order_by(Organization.name).limit(limit).offset(offset))
    items = [
        {
            "id": org.id,
            "region_id": org.region_id,
            "code": org.code,
            "name": org.name,
            "kind": org.organization_type,
            "is_active": org.is_active,
        }
        for org in rows
    ]
    return PagedOrganizationResponse(items=items, total=total or 0, limit=limit, offset=offset)


@router.get("/issue-reasons")
async def list_issue_reasons(
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> dict:
    total = await session.scalar(select(func.count()).select_from(IssueReason))
    rows = await session.scalars(
        select(IssueReason)
        .where(IssueReason.is_active.is_(True))
        .order_by(IssueReason.code)
        .limit(limit)
        .offset(offset)
    )
    items = [
        {
            "id": str(row.id),
            "code": row.code,
            "scope": row.scope,
            "kind": row.kind,
            "label": row.label,
            "is_active": row.is_active,
        }
        for row in rows
    ]
    return {"items": items, "total": total or 0, "limit": limit, "offset": offset}
