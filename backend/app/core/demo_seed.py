"""Fail-closed checks for the synthetic demo seed command."""

import re
from urllib.parse import urlsplit

from app.core.config import Settings


class DemoSeedNotAllowed(RuntimeError):
    """Raised when demo seeding is not explicitly enabled for an isolated target."""


def require_demo_seed_allowed(settings: Settings) -> None:
    """Allow seeding only with explicit demo mode, opt-in, and a demo/test DB name."""

    if settings.app_env.strip().casefold() != "demo" or not settings.demo_seed_enabled:
        raise DemoSeedNotAllowed("Demo seeding requires APP_ENV=demo and DEMO_SEED_ENABLED=true")

    database_parts = set(re.split(r"[^a-z0-9]+", settings.postgres_db.casefold()))
    has_production_marker = bool(database_parts.intersection({"prod", "production", "live"}))
    has_isolated_database_marker = bool(database_parts.intersection({"demo", "test"}))
    if has_production_marker or not has_isolated_database_marker:
        raise DemoSeedNotAllowed(
            "Demo seeding requires a database name containing demo or test and no production marker"
        )


def require_demo_api_target(url: str) -> None:
    """Restrict seed HTTP calls to a local LabTrack API, never an external provider."""

    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise DemoSeedNotAllowed("Demo seed API URL must target a local LabTrack API") from exc

    if (
        parsed.scheme.casefold() != "http"
        or parsed.hostname is None
        or parsed.hostname.casefold() not in {"api", "localhost", "127.0.0.1", "::1"}
        or port != 8000
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise DemoSeedNotAllowed("Demo seed API URL must target a local LabTrack API")
