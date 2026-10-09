from functools import lru_cache
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

MEBIBYTE = 1024 * 1024
DEFAULT_PAGE_SIZE = 100
MAX_PAGE_SIZE = 1000
DEVELOPMENT_DATABASE_URL = "postgresql+psycopg://geo:geo@localhost:5432/geo"


class Settings(BaseSettings):
    """Runtime configuration, read from environment variables and an optional .env file."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    environment: Literal["development", "test", "production"] = "development"
    database_url: str = DEVELOPMENT_DATABASE_URL
    redis_url: str = "redis://localhost:6379/0"
    upload_dir: Path = Path("data/uploads")
    api_key: SecretStr | None = None

    max_upload_bytes: int = Field(100 * MEBIBYTE, gt=0)
    max_zip_uncompressed_bytes: int = Field(500 * MEBIBYTE, gt=0)
    max_zip_members: int = Field(50, gt=0)

    processing_batch_size: int = Field(1000, gt=0)
    job_timeout_seconds: int = Field(1800, gt=0)
    max_processing_attempts: int = Field(3, ge=1, le=10)
    reconciliation_interval_seconds: int = Field(15, ge=1, le=300)
    queue_retry_interval_seconds: int = Field(60, ge=1, le=3600)
    job_recovery_grace_seconds: int = Field(60, ge=1, le=3600)
    reconciliation_batch_size: int = Field(100, ge=1, le=1000)

    rate_limit_requests: int = Field(120, gt=0)
    rate_limit_window_seconds: int = Field(60, gt=0)

    log_level: str = "INFO"
    log_format: Literal["json", "console"] = "json"

    @model_validator(mode="after")
    def validate_production_configuration(self) -> Self:
        if self.environment == "production":
            if self.database_url == DEVELOPMENT_DATABASE_URL:
                raise ValueError("DATABASE_URL must be set explicitly when ENVIRONMENT=production.")
            try:
                database_url = make_url(self.database_url)
            except ArgumentError as error:
                raise ValueError(
                    "DATABASE_URL must be a valid PostgreSQL connection URL."
                ) from error
            if database_url.drivername != "postgresql+psycopg" or not database_url.database:
                raise ValueError(
                    "DATABASE_URL must target a database using the postgresql+psycopg driver."
                )
            api_key_value = self.api_key.get_secret_value() if self.api_key is not None else ""
            if len(api_key_value) < 32:
                raise ValueError(
                    "API_KEY must be a random value with at least 32 characters "
                    "when ENVIRONMENT=production."
                )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
