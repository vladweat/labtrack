"""Demo seeding is an explicit, demo-only operation."""

from datetime import UTC, datetime
from uuid import UUID

import pytest

from app import demo_seed
from app.core.config import Settings
from app.core.demo_seed import (
    DemoSeedNotAllowed,
    require_demo_api_target,
    require_demo_seed_allowed,
)


def make_settings(
    *,
    app_env: str = "development",
    demo_seed_enabled: bool = False,
    postgres_db: str = "labtrack_demo",
) -> Settings:
    return Settings(
        _env_file=None,
        app_env=app_env,
        demo_seed_enabled=demo_seed_enabled,
        postgres_db=postgres_db,
        jwt_secret_key="unit-test-jwt-secret-that-is-long-enough-000000",
    )


def test_demo_seed_opt_in_is_disabled_by_default() -> None:
    assert Settings(_env_file=None).demo_seed_enabled is False


def test_demo_seed_opt_in_reads_explicit_environment_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEMO_SEED_ENABLED", "true")

    assert Settings(_env_file=None).demo_seed_enabled is True


@pytest.mark.parametrize(
    "settings",
    (
        make_settings(),
        make_settings(app_env="development", demo_seed_enabled=True),
        make_settings(app_env="production", demo_seed_enabled=True),
        make_settings(app_env="demo"),
        make_settings(app_env="demo", demo_seed_enabled=True, postgres_db="labtrack"),
        make_settings(
            app_env="demo", demo_seed_enabled=True, postgres_db="labtrack_production_demo"
        ),
    ),
)
def test_demo_seed_rejects_default_production_and_non_demo_targets(settings: Settings) -> None:
    with pytest.raises(DemoSeedNotAllowed):
        require_demo_seed_allowed(settings)


@pytest.mark.parametrize("database_name", ("labtrack_demo", "labtrack_demo_test"))
def test_demo_seed_requires_demo_mode_opt_in_and_isolated_database(database_name: str) -> None:
    settings = make_settings(
        app_env="demo",
        demo_seed_enabled=True,
        postgres_db=database_name,
    )

    require_demo_seed_allowed(settings)


@pytest.mark.parametrize(
    "url",
    (
        "https://api:8000",
        "http://payments.example:8000",
        "http://api:5432",
        "http://api:8000/provider",
    ),
)
def test_demo_seed_rejects_external_or_non_api_network_targets(url: str) -> None:
    with pytest.raises(DemoSeedNotAllowed):
        require_demo_api_target(url)


@pytest.mark.parametrize(
    "url",
    ("http://api:8000", "http://localhost:8000", "http://127.0.0.1:8000", "http://[::1]:8000"),
)
def test_demo_seed_accepts_only_local_api_targets(url: str) -> None:
    require_demo_api_target(url)


def test_repeated_shipment_seed_reuses_existing_delivery_date_across_days(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEMO_SENDER_EMAIL", "sender@demo.example.test")
    monkeypatch.setenv("DEMO_SENDER_PASSWORD", "synthetic-sender-password")
    monkeypatch.setenv("DEMO_COURIER_EMAIL", "courier@demo.example.test")
    monkeypatch.setenv("DEMO_COURIER_PASSWORD", "synthetic-courier-password")

    dates = iter(
        (
            datetime(2026, 11, 6, 10, tzinfo=UTC),
            datetime(2026, 11, 7, 10, tzinfo=UTC),
        )
    )

    class ControlledDateTime:
        @classmethod
        def now(cls, tz: object = None) -> datetime:
            return next(dates)

    monkeypatch.setattr(demo_seed, "datetime", ControlledDateTime)

    shipment_id = "shipment-1"
    sample_id = "sample-1"
    container_id = "container-1"
    current_delivery_date: str | None = None
    date_request: dict | None = None
    date_patch_count = 0

    def fake_api_request(
        method: str,
        path: str,
        *,
        body: dict | None = None,
        token: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict:
        nonlocal current_delivery_date, date_request, date_patch_count
        if path == "/api/v1/auth/token":
            return {"access_token": "synthetic-token"}
        if path == "/api/v1/samples":
            return {"id": sample_id}
        if path == "/api/v1/shipments" and method == "POST":
            return {"id": shipment_id}
        if path == f"/api/v1/shipments/{shipment_id}" and method == "GET":
            return {"expected_delivery_at": current_delivery_date}
        if path == f"/api/v1/shipments/{shipment_id}" and method == "PATCH":
            assert idempotency_key == "labtrack-public-demo-v1-delivery-date"
            if date_request is not None and date_request != body:
                raise RuntimeError("409 Idempotency-Key was already used with another request")
            date_request = body
            current_delivery_date = body["expected_delivery_at"]
            date_patch_count += 1
            return {"id": shipment_id}
        if path == f"/api/v1/shipments/{shipment_id}/containers":
            return {"id": container_id}
        if path == f"/api/v1/courier/shipments/{shipment_id}/offline-manifest":
            return {"status": "READY_FOR_PICKUP"}
        return {}

    monkeypatch.setattr(demo_seed, "api_request", fake_api_request)
    ids = (UUID("00000000-0000-0000-0000-000000000001"),) * 3

    first = demo_seed.create_demo_shipment(*ids)
    second = demo_seed.create_demo_shipment(*ids)

    assert first == second == shipment_id
    assert date_patch_count == 1
    assert date_request == {"expected_delivery_at": "2026-11-07T12:00:00+00:00"}
