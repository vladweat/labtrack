"""Export or verify the current FastAPI runtime OpenAPI document.

This imports the ASGI application without starting its lifespan or connecting to
PostgreSQL, Redis, or external services.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from app.main import app  # noqa: E402


def _schema_ref(operation: dict[str, Any], status: str) -> str:
    return operation["responses"][status]["content"]["application/json"]["schema"]["$ref"]


def _request_ref(operation: dict[str, Any]) -> str:
    return operation["requestBody"]["content"]["application/json"]["schema"]["$ref"]


def _assert_operation(
    schema: dict[str, Any],
    *,
    path: str,
    method: str,
    operation_id: str,
    request_model: str | None,
    response_model: str,
    error_statuses: tuple[str, ...] = (),
) -> None:
    operation = schema["paths"][path][method]
    assert operation["operationId"] == operation_id, f"Unexpected operationId: {path} {method}"
    if request_model is not None:
        assert _request_ref(operation) == f"#/components/schemas/{request_model}"
    assert _schema_ref(operation, "200") == f"#/components/schemas/{response_model}"
    for status in error_statuses:
        assert _schema_ref(operation, status) == "#/components/schemas/APIErrorResponse"


def validate_runtime_openapi(schema: dict[str, Any]) -> None:
    """Assert key runtime routes, prefixes, names, and declared envelopes."""

    assert schema["openapi"] == "3.1.0"
    assert schema["info"]["title"] == "LabTrack API"
    paths = schema["paths"]
    assert "/api/v1/auth/token" in paths
    assert "/api/v1/auth/login" not in paths

    _assert_operation(
        schema,
        path="/api/v1/auth/token",
        method="post",
        operation_id="login_api_v1_auth_token_post",
        request_model="LoginRequest",
        response_model="AccessTokenResponse",
    )
    _assert_operation(
        schema,
        path="/api/v1/courier/sync",
        method="post",
        operation_id="syncCourierOfflineCommands",
        request_model="CourierSyncRequest",
        response_model="CourierSyncResponse",
        error_statuses=("401", "403", "404", "409", "422", "503"),
    )

    shipment_operations = (
        (
            "/api/v1/shipments/{shipment_id}/handoffs/pickup/confirm-release",
            "confirm_pickup_release_api_v1_shipments__shipment_id__handoffs_pickup_confirm_release_post",
            "ContainerCodesRequest",
            ("401", "403", "404", "409", "422", "500", "503"),
        ),
        (
            "/api/v1/shipments/{shipment_id}/handoffs/pickup/confirm-pickup",
            "confirm_pickup_api_v1_shipments__shipment_id__handoffs_pickup_confirm_pickup_post",
            "ContainerCodesRequest",
            ("401", "403", "404", "409", "422", "500", "503"),
        ),
        (
            "/api/v1/shipments/{shipment_id}/containers/{container_id}/handoffs/delivery/confirm-delivery",
            "courierInitiateContainerDelivery",
            "ContainerCodeRequest",
            ("401", "403", "404", "409", "422", "503"),
        ),
        (
            "/api/v1/shipments/{shipment_id}/containers/{container_id}/handoffs/delivery/confirm-receipt",
            "labConfirmContainerReceipt",
            "ContainerCodeRequest",
            ("401", "403", "404", "409", "422", "503"),
        ),
    )
    for path, operation_id, request_model, error_statuses in shipment_operations:
        _assert_operation(
            schema,
            path=path,
            method="post",
            operation_id=operation_id,
            request_model=request_model,
            response_model="HandoffActionResult",
            error_statuses=error_statuses,
        )

    components = schema["components"]["schemas"]
    error_response = components["APIErrorResponse"]
    assert error_response["required"] == ["error"]
    assert error_response["properties"]["error"]["$ref"] == "#/components/schemas/APIError"
    api_error = components["APIError"]
    assert set(api_error["required"]) == {"code", "message", "details", "request_id"}


def render_runtime_openapi(schema: dict[str, Any]) -> str:
    """Serialize an OpenAPI document in stable, UTF-8-friendly JSON form."""

    return json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    checks = parser.add_mutually_exclusive_group()
    checks.add_argument(
        "--check",
        action="store_true",
        help="validate representative runtime operations and deterministic serialization",
    )
    checks.add_argument(
        "--check-file",
        type=Path,
        metavar="PATH",
        help="fail if PATH differs from the current canonical runtime document",
    )
    args = parser.parse_args(argv)

    schema = app.openapi()
    validate_runtime_openapi(schema)
    rendered = render_runtime_openapi(schema)
    if rendered != render_runtime_openapi(app.openapi()):
        raise RuntimeError("Runtime OpenAPI JSON serialization is not deterministic")

    if args.check:
        print("Runtime OpenAPI routes, response schemas, and deterministic output are valid.")
        return 0
    if args.check_file is not None:
        try:
            current = args.check_file.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"Cannot read runtime OpenAPI artifact {args.check_file}: {exc}", file=sys.stderr)
            return 2
        if current != rendered:
            print(
                f"Runtime OpenAPI drift detected in {args.check_file}; regenerate and review it.",
                file=sys.stderr,
            )
            return 1
        print(f"Runtime OpenAPI artifact is current: {args.check_file}")
        return 0

    sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
