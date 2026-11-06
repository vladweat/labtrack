"""Runtime OpenAPI and structured request-log contract checks for Stage H."""

import json
import logging
import re
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.logging import JsonFormatter, request_id_var
from app.main import app


def _normalized_schema(schema: dict, components: dict) -> dict:
    """Resolve refs and normalize equivalent nullable forms across OpenAPI versions."""

    if "$ref" in schema:
        name = schema["$ref"].rsplit("/", 1)[1]
        return _normalized_schema(components[name], components)

    any_of = schema.get("anyOf")
    type_value = schema.get("type")
    if any_of is not None:
        nullable = any(option.get("type") == "null" for option in any_of)
        non_null = [option for option in any_of if option.get("type") != "null"]
        if nullable and len(non_null) == 1:
            normalized = _normalized_schema(non_null[0], components)
            normalized["nullable"] = True
            return normalized
    elif isinstance(type_value, list) and "null" in type_value and len(type_value) == 2:
        normalized = {
            key: value
            for key, value in schema.items()
            if key not in {"type", "description", "title"}
        }
        normalized["type"] = next(value for value in type_value if value != "null")
        normalized["nullable"] = True
        return _normalized_schema(normalized, components)

    normalized = {}
    for key, value in schema.items():
        if key in {"description", "title", "examples"}:
            continue
        if key == "required":
            normalized[key] = sorted(value)
        elif key == "properties":
            normalized[key] = {
                property_name: _normalized_schema(property_schema, components)
                for property_name, property_schema in value.items()
            }
        elif key in {"items", "additionalProperties"} and isinstance(value, dict):
            normalized[key] = _normalized_schema(value, components)
        elif key == "anyOf":
            normalized[key] = sorted(
                (_normalized_schema(option, components) for option in value),
                key=lambda item: json.dumps(item, sort_keys=True),
            )
        else:
            normalized[key] = value
    return normalized


def _schemas_match(expected: dict, actual: dict) -> bool:
    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        return expected == actual
    if type(expected) is not type(actual):
        return False
    if isinstance(expected, dict):
        return expected.keys() == actual.keys() and all(
            _schemas_match(expected[key], actual[key]) for key in expected
        )
    if isinstance(expected, list):
        return len(expected) == len(actual) and all(
            _schemas_match(expected_item, actual_item)
            for expected_item, actual_item in zip(expected, actual, strict=True)
        )
    return expected == actual


def test_stage_h_runtime_openapi_conforms_to_historical_design_excerpt() -> None:
    contract_path = Path(__file__).resolve().parents[3] / "openapi" / "labtrack-v1.json"
    contract = json.loads(contract_path.read_text())
    runtime = app.openapi()
    operations = (
        ("/operations/stalled-shipments", "get", "listStalledShipments"),
        ("/analytics/shipments/summary", "get", "getShipmentSummary"),
        ("/notifications", "get", "listInboxNotifications"),
        ("/notifications/unread-count", "get", "getInboxUnreadCount"),
        ("/notifications/{notification_id}/read", "post", "markInboxNotificationRead"),
    )
    response_header_mismatches = []
    component_schema_mismatches = []

    for path, method, operation_id in operations:
        expected = contract["paths"][path][method]
        actual = runtime["paths"][f"/api/v1{path}"][method]
        assert actual["operationId"] == expected["operationId"] == operation_id
        assert actual["summary"] == expected["summary"]
        assert set(actual["responses"]) == set(expected["responses"])
        if actual["responses"]["200"].get("headers", {}) != expected["responses"]["200"].get(
            "headers", {}
        ):
            response_header_mismatches.append(path)

        expected_parameters = {
            (parameter["name"], parameter["in"]): parameter
            for parameter in expected.get("parameters", [])
        }
        actual_parameters = {
            (parameter["name"], parameter["in"]): parameter
            for parameter in actual.get("parameters", [])
        }
        assert actual_parameters.keys() == expected_parameters.keys()
        for key, expected_parameter in expected_parameters.items():
            actual_parameter = actual_parameters[key]
            assert actual_parameter.get("required", False) == expected_parameter.get(
                "required", False
            )
            expected_schema = expected_parameter["schema"]
            actual_schema = actual_parameter["schema"]
            if "anyOf" in actual_schema:
                actual_schema = next(
                    option for option in actual_schema["anyOf"] if option.get("type") != "null"
                )
            for field in ("type", "format", "enum", "minimum", "maximum", "minLength", "maxLength"):
                if field in expected_schema:
                    assert actual_schema.get(field) == expected_schema[field]
            if "default" in expected_schema:
                assert actual_schema.get("default") == expected_schema["default"]

        expected_response_ref = expected["responses"]["200"]["content"]["application/json"][
            "schema"
        ]["$ref"]
        actual_response_ref = actual["responses"]["200"]["content"]["application/json"]["schema"][
            "$ref"
        ]
        expected_name = expected_response_ref.rsplit("/", 1)[1]
        actual_name = actual_response_ref.rsplit("/", 1)[1]
        assert actual_name == expected_name
        expected_schema = contract["components"]["schemas"][expected_name]
        actual_schema = runtime["components"]["schemas"][actual_name]
        assert set(actual_schema["properties"]) == set(expected_schema["properties"])
        assert set(actual_schema["required"]) == set(expected_schema["required"])
        assert actual_schema.get("additionalProperties") is False

    stage_h_schema_names = (
        "StalledShipment",
        "PagedStalledShipment",
        "ShipmentSummary",
        "AnalyticsPeriodBucket",
        "AnalyticsFacility",
        "PagedNotification",
        "Notification",
        "NotificationUnreadCount",
    )
    static_components = contract["components"]["schemas"]
    runtime_components = runtime["components"]["schemas"]
    for name in stage_h_schema_names:
        static_schema = _normalized_schema(static_components[name], static_components)
        runtime_schema = _normalized_schema(runtime_components[name], runtime_components)
        if not _schemas_match(static_schema, runtime_schema):
            component_schema_mismatches.append(name)

    assert not response_header_mismatches and not component_schema_mismatches, (
        "Runtime and checked-in OpenAPI mismatches: missing/different X-Request-ID headers on "
        + ", ".join(response_header_mismatches)
        + "; differing Stage H component schemas: "
        + ", ".join(component_schema_mismatches)
    )

    operations_description = runtime["paths"]["/api/v1/operations/stalled-shipments"]["get"][
        "description"
    ]
    assert (
        operations_description
        == contract["paths"]["/operations/stalled-shipments"]["get"]["description"]
    )
    analytics_description = runtime["paths"]["/api/v1/analytics/shipments/summary"]["get"][
        "description"
    ]
    assert (
        analytics_description
        == contract["paths"]["/analytics/shipments/summary"]["get"]["description"]
    )


@pytest.mark.asyncio
async def test_request_id_is_regenerated_and_completion_log_is_structured_and_redacted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caller_value = f"private-request-id-{uuid4()}"
    private_query = f"token-{uuid4()}"
    with caplog.at_level(logging.INFO):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://stageh-test"
        ) as client:
            response = await client.get(
                "/health/live",
                headers={"X-Request-ID": caller_value},
                params={"access_token": private_query},
            )

    assert response.status_code == 200
    request_id = response.headers["X-Request-ID"]
    assert request_id != caller_value
    UUID(request_id)
    record = next(
        item
        for item in caplog.records
        if item.msg == "request_completed" and getattr(item, "request_id", None) == request_id
    )
    payload = json.loads(JsonFormatter().format(record))
    assert payload["message"] == "request_completed"
    assert payload["request_id"] == request_id
    assert payload["http_method"] == "GET"
    assert payload["route_template"] == "/health/live"
    assert payload["status"] == 200
    assert isinstance(payload["duration_ms"], (int, float))
    assert payload["duration_ms"] >= 0
    serialized = json.dumps(payload)
    assert caller_value not in serialized
    assert private_query not in serialized
    assert "access_token" not in serialized
    assert re.fullmatch(r"[0-9a-f-]{36}", payload["request_id"])


def test_json_formatter_drops_unapproved_ids_routes_and_extra_fields() -> None:
    record = logging.LogRecord(
        name="stageh.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="request_completed",
        args=(),
        exc_info=None,
    )
    record.request_id = f"untrusted {uuid4()}"
    record.http_method = "GET"
    record.route_template = f"/api/v1/notifications/{uuid4()}/read?access_token=secret"
    record.status = 200
    record.duration_ms = 12.5
    record.password = "private-password"
    context_token = request_id_var.set(f"request-{uuid4()}")
    try:
        payload = json.loads(JsonFormatter().format(record))
    finally:
        request_id_var.reset(context_token)

    assert payload["request_id"] == "-"
    assert payload["route_template"] == "unmatched"
    assert payload["message"] == "request_completed"
    assert payload["http_method"] == "GET"
    assert payload["status"] == 200
    assert payload["duration_ms"] == 12.5
    assert "password" not in payload
    assert "private-password" not in json.dumps(payload)
    assert str(record.request_id) not in json.dumps(payload)
