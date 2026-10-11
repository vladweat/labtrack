"""Small JSON logging setup with request correlation and no request-body capture."""

import json
import logging
import re
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")
_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{8,64}$")


class JsonFormatter(logging.Formatter):
    """Emit a stable, compact JSON object for each log record."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": request_id_var.get(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def configure_logging(level: str) -> None:
    """Configure application logs as JSON without logging sensitive request data."""

    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())


def normalized_request_id(value: str | None) -> str:
    """Accept only short, printable correlation IDs; otherwise use a UUID."""

    if value and _REQUEST_ID_PATTERN.fullmatch(value):
        return value
    from uuid import uuid4

    return str(uuid4())
