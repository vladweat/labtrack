"""Documented error response models for business and authorization failures."""

from typing import Any

from pydantic import BaseModel, ConfigDict


class APIError(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    details: dict[str, Any]
    request_id: str


class APIErrorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    error: APIError


API_ERROR_RESPONSES = {
    status: {"model": APIErrorResponse, "description": "Documented API error"}
    for status in (401, 403, 404, 409, 422, 500, 503)
}
