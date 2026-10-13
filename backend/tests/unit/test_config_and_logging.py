import json
import logging
from logging import LogRecord
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.core.logging import JsonFormatter, request_id_var


def test_production_settings_require_a_non_default_jwt_secret() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, app_env="production")

    settings = Settings(
        _env_file=None,
        app_env="production",
        jwt_secret_key="production-key-that-has-more-than-32-characters",
    )
    assert settings.app_env == "production"


def test_json_logs_include_request_correlation_without_credentials() -> None:
    request_id = f"req-{uuid4()}"
    context_token = request_id_var.set(request_id)
    record = LogRecord(
        name="labtrack.api",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="request completed",
        args=(),
        exc_info=None,
    )
    try:
        payload = json.loads(JsonFormatter().format(record))
    finally:
        request_id_var.reset(context_token)

    assert payload["request_id"] == request_id
    assert payload["level"] == "INFO"
    assert payload["message"] == "request completed"
    assert "password" not in payload
    assert "access_token" not in payload
