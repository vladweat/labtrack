"""Small JSON logging setup with request correlation and no request-body capture."""

import json
import logging
import math
import re
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")
_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{8,64}$")
_UUID_PATTERN = re.compile(
    r"(?i)(?<![0-9a-f])[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(?![0-9a-f])"
)
_EVENT_FIELDS: dict[str, tuple[str, ...]] = {
    "request_completed": (
        "request_id",
        "http_method",
        "route_template",
        "status",
        "duration_ms",
    ),
    "stalled_notification_scan_completed": (
        "outcome",
        "candidate_case_count",
        "notification_insert_count",
        "notification_dedup_count",
        "duration_ms",
    ),
    "stalled_notification_scan_failed": ("outcome", "error_type"),
    "unhandled_api_exception": ("error_type",),
    "database_readiness_probe_failed": ("error_type",),
    "offline_command_rollback": ("error_type",),
}


class JsonFormatter(logging.Formatter):
    """Emit compact JSON with fixed fields and event-specific safe metadata."""

    def format(self, record: logging.LogRecord) -> str:
        # Messages and extras are treated as untrusted unless the event is allow-listed.
        offline_error_type: Any = None
        if (
            isinstance(record.msg, str)
            and record.msg == "Offline command %s rolled back (%s)"
            and isinstance(record.args, tuple)
            and len(record.args) == 2
        ):
            # The first argument is a client-controlled command ID; never emit it.
            event = "offline_command_rollback"
            offline_error_type = record.args[1]
        else:
            event = record.msg if isinstance(record.msg, str) and not record.args else None
        if event not in _EVENT_FIELDS:
            event = "application_log"

        request_id = request_id_var.get()
        if not self._is_safe_value("request_id", request_id):
            request_id = "-"

        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": event,
            "request_id": request_id,
        }

        for field in _EVENT_FIELDS.get(event, ()):
            if field == "request_id":
                value = getattr(record, field, request_id)
            elif field == "error_type" and event == "offline_command_rollback":
                value = offline_error_type
            else:
                value = getattr(record, field, None)
            if self._is_safe_value(field, value):
                payload[field] = value
            elif field == "request_id":
                payload[field] = "-"
            elif field == "route_template":
                payload[field] = "unmatched"
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _is_safe_value(field: str, value: Any) -> bool:
        if field == "request_id":
            return isinstance(value, str) and (
                value == "-" or _REQUEST_ID_PATTERN.fullmatch(value) is not None
            )
        if field == "http_method":
            return isinstance(value, str) and re.fullmatch(r"[A-Z]{1,16}", value) is not None
        if field == "route_template":
            return isinstance(value, str) and (
                value == "unmatched"
                or (
                    len(value) <= 512
                    and value.startswith("/")
                    and "?" not in value
                    and "#" not in value
                    and not any(ord(character) < 32 for character in value)
                    and _UUID_PATTERN.search(value) is None
                )
            )
        if field == "status":
            return isinstance(value, int) and not isinstance(value, bool) and 100 <= value <= 599
        if field == "duration_ms":
            if isinstance(value, int) and not isinstance(value, bool):
                return value >= 0
            return isinstance(value, float) and math.isfinite(value) and value >= 0
        if field in {
            "candidate_case_count",
            "notification_insert_count",
            "notification_dedup_count",
        }:
            return isinstance(value, int) and not isinstance(value, bool) and value >= 0
        if field == "outcome":
            return isinstance(value, str) and value in {"success", "failure"}
        if field == "error_type":
            return (
                isinstance(value, str)
                and len(value) <= 64
                and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value) is not None
            )
        return False


def configure_logging(level: str) -> None:
    """Configure application logs as JSON without logging sensitive request data."""

    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())


def normalized_request_id(value: str | None) -> str:
    """Ignore caller IDs, which may identify resources, and issue a server UUID instead."""

    from uuid import uuid4

    return str(uuid4())
