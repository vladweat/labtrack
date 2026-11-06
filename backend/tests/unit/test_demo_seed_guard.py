"""Demo seeding is an explicit, demo-only operation."""

import pytest

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
