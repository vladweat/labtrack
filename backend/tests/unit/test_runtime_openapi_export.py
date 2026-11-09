"""Runtime OpenAPI export checks independent of database services."""

import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app

BACKEND_ROOT = Path(__file__).resolve().parents[2]
EXPORTER = BACKEND_ROOT / "scripts" / "export_runtime_openapi.py"


def _run_exporter(*arguments: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(
        {
            "APP_ENV": "test",
            "POSTGRES_DB": "labtrack_test",
            "POSTGRES_HOST": "127.0.0.1",
            "POSTGRES_PORT": "1",
        }
    )
    return subprocess.run(
        [sys.executable, str(EXPORTER), *arguments],
        cwd=BACKEND_ROOT,
        env=env,
        capture_output=True,
        check=False,
        text=True,
    )


def _schema_ref(operation: dict, status: str) -> str:
    return operation["responses"][status]["content"]["application/json"]["schema"]["$ref"]


def test_exported_runtime_openapi_has_expected_routes_and_is_reproducible(
    tmp_path: Path,
) -> None:
    assert EXPORTER.is_file(), "runtime OpenAPI exporter is missing"

    first = _run_exporter()
    second = _run_exporter()
    assert first.returncode == second.returncode == 0, first.stderr + second.stderr
    assert first.stdout == second.stdout

    schema = json.loads(first.stdout)
    paths = schema["paths"]
    auth = paths["/api/v1/auth/token"]["post"]
    assert auth["operationId"] == "login_api_v1_auth_token_post"
    assert auth["requestBody"]["content"]["application/json"]["schema"]["$ref"] == (
        "#/components/schemas/LoginRequest"
    )
    assert _schema_ref(auth, "200") == "#/components/schemas/AccessTokenResponse"
    assert _schema_ref(auth, "401") == "#/components/schemas/APIErrorResponse"
    assert _schema_ref(auth, "429") == "#/components/schemas/APIErrorResponse"
    assert _schema_ref(auth, "422") == "#/components/schemas/APIErrorResponse"
    assert auth["responses"]["401"]["headers"]["WWW-Authenticate"]["schema"]["type"] == "string"
    assert auth["responses"]["429"]["headers"]["Retry-After"]["schema"]["type"] == ("string")

    sync = paths["/api/v1/courier/sync"]["post"]
    assert sync["operationId"] == "syncCourierOfflineCommands"
    assert sync["requestBody"]["content"]["application/json"]["schema"]["$ref"] == (
        "#/components/schemas/CourierSyncRequest"
    )
    assert _schema_ref(sync, "200") == "#/components/schemas/CourierSyncResponse"
    assert _schema_ref(sync, "409") == "#/components/schemas/APIErrorResponse"
    assert _schema_ref(sync, "422") == "#/components/schemas/APIErrorResponse"

    workflow_operations = {
        "/api/v1/shipments/{shipment_id}/handoffs/pickup/confirm-release": (
            "post",
            "ContainerCodesRequest",
        ),
        "/api/v1/shipments/{shipment_id}/handoffs/pickup/confirm-pickup": (
            "post",
            "ContainerCodesRequest",
        ),
        "/api/v1/shipments/{shipment_id}/containers/{container_id}/"
        "handoffs/delivery/confirm-delivery": (
            "post",
            "ContainerCodeRequest",
        ),
        "/api/v1/shipments/{shipment_id}/containers/{container_id}/"
        "handoffs/delivery/confirm-receipt": (
            "post",
            "ContainerCodeRequest",
        ),
    }
    for path, (method, request_model) in workflow_operations.items():
        operation = paths[path][method]
        assert operation["requestBody"]["content"]["application/json"]["schema"]["$ref"] == (
            f"#/components/schemas/{request_model}"
        )
        assert _schema_ref(operation, "200") == "#/components/schemas/HandoffActionResult"
        assert _schema_ref(operation, "409") == "#/components/schemas/APIErrorResponse"

    api_error_response = schema["components"]["schemas"]["APIErrorResponse"]
    assert api_error_response["required"] == ["error"]
    assert api_error_response["properties"]["error"]["$ref"] == ("#/components/schemas/APIError")
    api_error = schema["components"]["schemas"]["APIError"]
    assert set(api_error["required"]) == {"code", "message", "details", "request_id"}

    artifact = tmp_path / "runtime-openapi.json"
    artifact.write_text(first.stdout, encoding="utf-8")
    matching = _run_exporter("--check-file", str(artifact))
    assert matching.returncode == 0, matching.stderr
    artifact.write_text("{}\n", encoding="utf-8")
    drifted = _run_exporter("--check-file", str(artifact))
    assert drifted.returncode != 0
    assert "drift" in drifted.stderr.casefold()


@pytest.mark.asyncio
async def test_auth_validation_runtime_response_uses_the_shared_error_envelope() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/v1/auth/token", json={})

    assert response.status_code == 422
    body = response.json()
    assert set(body) == {"error"}
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert body["error"]["message"] == "Request validation failed"
    assert body["error"]["details"] == {}
    assert UUID(body["error"]["request_id"])
