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
        "stalled_pickup_count",
        "stalled_delivery_count",
        "duration_ms",
    ),
    "stalled_notification_scan_failed": ("outcome", "error_type"),
    "stalled_work_count_failed": ("error_type",),
    "offline_sync_command": ("command_type", "outcome", "reason_category", "count"),
    "custody_command_failure": ("command_type", "reason_category", "count"),
    "unhandled_api_exception": ("error_type",),
    "database_readiness_probe_failed": ("error_type",),
    "offline_command_rollback": ("error_type",),
}
_SAFE_TELEMETRY_EVENTS = {
    "offline_sync_command",
    "custody_command_failure",
    "stalled_notification_scan_completed",
    "stalled_notification_scan_failed",
    "stalled_work_count_failed",
}
_SAFE_TELEMETRY_LOGGERS = {"app.telemetry", "app.workers.notifications"}
_CUSTODY_FAILURE_COMMANDS = {
    "confirm_pickup_release": "pickup",
    "confirm_pickup": "pickup",
    "withdraw_pickup": "pickup",
    "courier_initiate_container_delivery": "delivery",
    "lab_confirm_container_receipt": "delivery",
    "courier_withdraw_container_delivery": "delivery",
    "create_custody_incident": "custody_incident",
    "attest_custody_incident": "custody_incident",
    "start_exceptional_container_receipt": "exceptional_receipt",
    "resolve_custody_incident": "custody_incident",
    "record_receipt": "receiving",
    "complete_receiving": "receiving",
    "reopen_receiving": "receiving",
    "report_container_issue": "container_issue",
    "propose_container_issue_resolution": "container_issue",
    "decide_container_issue_resolution": "container_issue",
    "withdraw_container_issue_proposal": "container_issue",
    "cancel_shipment": "shipment_cancellation",
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
            "stalled_pickup_count",
            "stalled_delivery_count",
            "count",
        }:
            return isinstance(value, int) and not isinstance(value, bool) and value >= 0
        if field == "outcome":
            return isinstance(value, str) and value in {
                "success",
                "failure",
                "applied",
                "replayed",
                "rejected",
                "retryable",
                "review_required",
            }
        if field == "command_type":
            return isinstance(value, str) and value in {
                "pickup",
                "delivery",
                "custody_incident",
                "exceptional_receipt",
                "receiving",
                "container_issue",
                "shipment_cancellation",
            }
        if field == "reason_category":
            return isinstance(value, str) and value in {
                "none",
                "authorization",
                "missing_or_hidden",
                "conflict",
                "validation",
                "rate_limited",
                "server_error",
                "request_error",
                "stale_state",
                "dependency",
                "invalid_snapshot",
                "invalid_command",
                "idempotency",
                "expired_snapshot",
                "temporary_failure",
                "unknown",
            }
        if field == "error_type":
            return (
                isinstance(value, str)
                and len(value) <= 64
                and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value) is not None
            )
        return False


def emit_telemetry_event(event: str, *, logger_name: str = "app.telemetry", **fields: Any) -> None:
    """Write one allowlisted workflow event without affecting its caller."""

    allowed_fields = _EVENT_FIELDS.get(event)
    if (
        event not in _SAFE_TELEMETRY_EVENTS
        or allowed_fields is None
        or logger_name not in _SAFE_TELEMETRY_LOGGERS
    ):
        return
    if set(fields) - set(allowed_fields) or any(
        not JsonFormatter._is_safe_value(field, value) for field, value in fields.items()
    ):
        return
    try:
        logging.getLogger(logger_name).info(event, extra=fields)
    except Exception:
        # Telemetry is diagnostic only and must never alter a business outcome.
        return


def record_custody_command_failure(route_name: str | None, status: int) -> None:
    """Emit a bounded failure category for known state-changing custody routes."""

    command_type = _CUSTODY_FAILURE_COMMANDS.get(route_name or "")
    if command_type is None or status < 400:
        return
    if status in {401, 403}:
        reason_category = "authorization"
    elif status == 404:
        reason_category = "missing_or_hidden"
    elif status == 409:
        reason_category = "conflict"
    elif status == 422:
        reason_category = "validation"
    elif status == 429:
        reason_category = "rate_limited"
    elif status >= 500:
        reason_category = "server_error"
    else:
        reason_category = "request_error"
    emit_telemetry_event(
        "custody_command_failure",
        command_type=command_type,
        reason_category=reason_category,
        count=1,
    )


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
