"""Typed application settings loaded from the repository's local environment file."""

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
DEVELOPMENT_JWT_SECRET = "local-development-secret-change-before-deploying-000000000000"


class Settings(BaseSettings):
    """Runtime configuration; secrets are never included in repr output."""

    model_config = SettingsConfigDict(
        env_file=REPOSITORY_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
    )

    app_name: str = "LabTrack API"
    app_env: str = "development"
    postgres_db: str = "labtrack"
    postgres_user: str = "labtrack"
    postgres_password: SecretStr = SecretStr("labtrack-local-only-change-me")
    app_postgres_user: str = "labtrack_app"
    app_postgres_password: SecretStr = SecretStr("labtrack-local-only-change-me")
    postgres_host: str = "127.0.0.1"
    postgres_port: int = 5432
    celery_broker_url: SecretStr = SecretStr("redis://127.0.0.1:6379/0")
    jwt_secret_key: SecretStr = SecretStr(DEVELOPMENT_JWT_SECRET)
    jwt_issuer: str = "labtrack-api"
    jwt_audience: str = "labtrack-client"
    jwt_access_token_minutes: int = 15
    destination_organization_code: str = "CENTRAL_LAB"
    log_level: str = "INFO"
    stalled_pickup_seconds: int = Field(default=4 * 60 * 60, gt=0)
    stalled_delivery_seconds: int = Field(default=4 * 60 * 60, gt=0)
    stalled_proposal_seconds: int = Field(default=24 * 60 * 60, gt=0)
    stalled_notification_scan_seconds: int = Field(default=300, gt=0)

    @model_validator(mode="after")
    def require_secure_jwt_secret(self) -> "Settings":
        secret = self.jwt_secret_key.get_secret_value()
        if self.app_env.lower() not in {"development", "dev", "test"}:
            if secret == DEVELOPMENT_JWT_SECRET or len(secret) < 32:
                raise ValueError(
                    "JWT_SECRET_KEY must be at least 32 characters outside development"
                )
        if self.jwt_access_token_minutes < 1 or self.jwt_access_token_minutes > 60:
            raise ValueError("JWT_ACCESS_TOKEN_MINUTES must be between 1 and 60")
        return self

    @property
    def database_url(self) -> URL:
        return URL.create(
            "postgresql+asyncpg",
            username=self.app_postgres_user,
            password=self.app_postgres_password.get_secret_value(),
            host=self.postgres_host,
            port=self.postgres_port,
            database=self.postgres_db,
        )

    @property
    def migration_database_url(self) -> URL:
        """Database owner connection used only for migrations and test cleanup."""
        return URL.create(
            "postgresql+asyncpg",
            username=self.postgres_user,
            password=self.postgres_password.get_secret_value(),
            host=self.postgres_host,
            port=self.postgres_port,
            database=self.postgres_db,
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide validated settings object."""

    return Settings()
