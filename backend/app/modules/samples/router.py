"""Sample registration and external-reference endpoints."""

from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db_session
from app.modules.idempotency.service import execute_idempotently
from app.modules.identity.access import Principal, get_current_principal
from app.modules.identity.models import Organization
from app.modules.samples.models import Sample, SampleExternalReference
from app.modules.samples.schemas import (
    CreateExternalReferenceRequest,
    CreateSampleRequest,
    ExternalReferenceResponse,
    PagedExternalReferenceResponse,
    PagedSampleResponse,
    SampleResponse,
)

router = APIRouter(prefix="/samples", tags=["samples"])


async def can_access_sample(session: AsyncSession, principal: Principal, sample: Sample) -> bool:
    return principal.has_role(
        "SENDER_OPERATOR", organization_id=sample.origin_organization_id
    ) or principal.has_role("LAB_MANAGER", organization_id=sample.origin_organization_id)


@router.post("", status_code=201, response_model=SampleResponse)
async def create_sample(
    request: CreateSampleRequest,
    response: Response,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> SampleResponse:
    if not principal.has_role("SENDER_OPERATOR", organization_id=request.origin_organization_id):
        raise HTTPException(status_code=404, detail="Resource not found")

    async def action() -> tuple[int, dict]:
        organization = await session.scalar(
            select(Organization).where(
                Organization.id == request.origin_organization_id,
                Organization.is_active.is_(True),
            )
        )
        if organization is None:
            raise HTTPException(404, "Resource not found")
        sample = Sample(
            code=f"SMP-{uuid4().hex[:12].upper()}",
            origin_organization_id=organization.id,
            material_type=request.material_type,
            collected_at=request.collected_at,
            created_by_user_id=principal.user_id,
        )
        session.add(sample)
        await session.flush()
        return 201, SampleResponse.model_validate(sample).model_dump(mode="json")

    status_code, body = await execute_idempotently(
        session,
        actor_id=principal.user_id,
        operation="samples.create",
        key=idempotency_key,
        request_context={},
        request_data=request.model_dump(mode="json"),
        action=action,
    )
    response.status_code = status_code
    return SampleResponse.model_validate(body)


@router.get("", response_model=PagedSampleResponse)
async def list_samples(
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    origin_organization_id: UUID | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> PagedSampleResponse:
    org_ids = {
        grant.organization_id
        for grant in principal.memberships
        if grant.organization_id is not None and grant.role in {"SENDER_OPERATOR", "LAB_MANAGER"}
    }
    query = select(Sample).where(Sample.origin_organization_id.in_(org_ids))
    if origin_organization_id:
        if origin_organization_id not in org_ids:
            raise HTTPException(404, "Resource not found")
        query = query.where(Sample.origin_organization_id == origin_organization_id)
    total = await session.scalar(select(func.count()).select_from(query.subquery()))
    samples = await session.scalars(
        query.order_by(Sample.created_at.desc()).limit(limit).offset(offset)
    )
    return PagedSampleResponse(
        items=[SampleResponse.model_validate(sample) for sample in samples],
        total=total or 0,
        limit=limit,
        offset=offset,
    )


@router.get("/{sample_id}", response_model=SampleResponse)
async def get_sample(
    sample_id: UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> SampleResponse:
    sample = await session.get(Sample, sample_id)
    if sample is None or not await can_access_sample(session, principal, sample):
        raise HTTPException(404, "Resource not found")
    return SampleResponse.model_validate(sample)


@router.post(
    "/{sample_id}/external-references", status_code=201, response_model=ExternalReferenceResponse
)
async def create_external_reference(
    sample_id: UUID,
    request: CreateExternalReferenceRequest,
    response: Response,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> ExternalReferenceResponse:
    sample = await session.get(Sample, sample_id)
    if sample is None or not await can_access_sample(session, principal, sample):
        raise HTTPException(404, "Resource not found")

    async def action() -> tuple[int, dict]:
        reference = SampleExternalReference(sample_id=sample_id, **request.model_dump())
        session.add(reference)
        await session.flush()
        return 201, ExternalReferenceResponse.model_validate(reference).model_dump(mode="json")

    code, body = await execute_idempotently(
        session,
        actor_id=principal.user_id,
        operation=f"samples.{sample_id}.external-references.create",
        key=idempotency_key,
        request_context={"sample_id": str(sample_id)},
        request_data=request.model_dump(mode="json"),
        action=action,
    )
    response.status_code = code
    return ExternalReferenceResponse.model_validate(body)


@router.get("/{sample_id}/external-references", response_model=PagedExternalReferenceResponse)
async def list_external_references(
    sample_id: UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> PagedExternalReferenceResponse:
    sample = await session.get(Sample, sample_id)
    if sample is None or not await can_access_sample(session, principal, sample):
        raise HTTPException(404, "Resource not found")
    query = select(SampleExternalReference).where(SampleExternalReference.sample_id == sample_id)
    total = await session.scalar(select(func.count()).select_from(query.subquery()))
    rows = await session.scalars(query.limit(limit).offset(offset))
    return PagedExternalReferenceResponse(
        items=[ExternalReferenceResponse.model_validate(row) for row in rows],
        total=total or 0,
        limit=limit,
        offset=offset,
    )
