"""Sample API contracts."""

from datetime import datetime
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class CreateSampleRequest(BaseModel):
    origin_organization_id: UUID
    material_type: str = Field(pattern="^(BLOOD|URINE|OTHER)$")
    collected_at: AwareDatetime | None = None


class SampleResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    code: str
    origin_organization_id: UUID
    material_type: str
    collected_at: datetime | None
    created_at: datetime


class PagedSampleResponse(BaseModel):
    items: list[SampleResponse]
    total: int
    limit: int
    offset: int


class CreateExternalReferenceRequest(BaseModel):
    system_code: str = Field(min_length=1, max_length=48)
    reference_kind: str = Field(min_length=1, max_length=32)
    external_id: str = Field(min_length=1, max_length=128)


class ExternalReferenceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    sample_id: UUID
    system_code: str
    reference_kind: str
    external_id: str
    created_at: datetime


class PagedExternalReferenceResponse(BaseModel):
    items: list[ExternalReferenceResponse]
    total: int
    limit: int
    offset: int
