import json
import logging
from logging import LogRecord
from uuid import uuid4

import pytest
from pydantic import ValidationError

import app.core.logging as logging_setup
from app.core.config import Settings
from app.core.logging import JsonFormatter, emit_telemetry_event, request_id_var


def test_production_settings_require_a_non_default_jwt_secret() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, app_env="production")

    settings = Settings(
        _env_file=None,
        app_env="production",
        jwt_secret_key="production-key-that-has-more-than-32-characters",
    )
    assert settings.app_env == "production"


def test_loopback_database_urls_keep_configured_host_and_port() -> None:
    settings = Settings(
        _env_file=None,
        postgres_host="127.0.0.1",
        postgres_port=5544,
    )

    assert settings.database_url.host == "127.0.0.1"
    assert settings.database_url.port == 5544
    assert settings.migration_database_url.host == "127.0.0.1"
    assert settings.migration_database_url.port == 5544


def test_unix_socket_database_urls_use_query_host_and_default_port() -> None:
    settings = Settings(
        _env_file=None,
        postgres_host="/tmp/labtrack-postgres-socket",
        postgres_port=32791,
    )

    assert settings.database_url.host is None
    assert settings.database_url.port == 5432
    assert settings.database_url.query["host"] == "/tmp/labtrack-postgres-socket"
    assert settings.migration_database_url.host is None
    assert settings.migration_database_url.port == 5432
    assert settings.migration_database_url.query["host"] == "/tmp/labtrack-postgres-socket"


def test_json_logs_include_request_correlation_without_credentials() -> None:
    request_id = f"req-{uuid4()}"
    context_token = request_id_var.set(request_id)
    record = LogRecord(
        name="labtrack.api",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="unhandled_api_exception",
        args=(),
        exc_info=None,
    )
    record.error_type = "RuntimeError"
    record.detail = "Injected private exception detail"
    record.password = "private-password"
    record.access_token = "private-access-token"
    try:
        payload = json.loads(JsonFormatter().format(record))
    finally:
        request_id_var.reset(context_token)

    assert payload["request_id"] == request_id
    assert payload["level"] == "INFO"
    assert payload["message"] == "unhandled_api_exception"
    assert payload["error_type"] == "RuntimeError"
    serialized = json.dumps(payload)
    assert "Injected private exception detail" not in serialized
    assert "private-password" not in serialized
    assert "private-access-token" not in serialized
    assert "password" not in payload
    assert "access_token" not in payload


def test_telemetry_event_uses_bounded_fields_and_omits_unapproved_values() -> None:
    record = LogRecord(
        name="app.telemetry",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="offline_sync_command",
        args=(),
        exc_info=None,
    )
    record.command_type = "pickup"
    record.outcome = "applied"
    record.reason_category = "none"
    record.count = 1
    record.shipment_id = str(uuid4())
    record.email = "private@example.test"
    context_token = request_id_var.set(f"request-{uuid4()}")
    try:
        payload = json.loads(JsonFormatter().format(record))
    finally:
        request_id_var.reset(context_token)

    assert payload["message"] == "offline_sync_command"
    assert payload["command_type"] == "pickup"
    assert payload["outcome"] == "applied"
    assert payload["reason_category"] == "none"
    assert payload["count"] == 1
    serialized = json.dumps(payload)
    assert record.shipment_id not in serialized
    assert record.email not in serialized
    assert "shipment_id" not in payload
    assert "email" not in payload


def test_telemetry_logging_failure_is_suppressed(monkeypatch: pytest.MonkeyPatch) -> None:
    class FailingLogger:
        def info(self, *_args: object, **_kwargs: object) -> None:
            raise RuntimeError("telemetry unavailable")

    monkeypatch.setattr(
        logging_setup.logging.getLogger("app.telemetry"), "info", FailingLogger().info
    )

    emit_telemetry_event(
        "offline_sync_command",
        command_type="pickup",
        outcome="applied",
        reason_category="none",
        count=1,
    )
