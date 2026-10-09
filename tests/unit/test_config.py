import pytest
from pydantic import ValidationError

from app.core.config import DEVELOPMENT_DATABASE_URL, Settings


def test_defaults_are_usable_for_local_development() -> None:
    settings = Settings(_env_file=None)

    assert settings.environment == "development"
    assert settings.processing_batch_size == 1000


def test_production_refuses_the_development_database() -> None:
    with pytest.raises(ValidationError, match="DATABASE_URL"):
        Settings(_env_file=None, environment="production", database_url=DEVELOPMENT_DATABASE_URL)


def test_production_accepts_an_explicit_database() -> None:
    settings = Settings(
        _env_file=None,
        environment="production",
        database_url="postgresql+psycopg://app:secret@db.internal:5432/geo",
        api_key="test-api-key-for-unit-tests-123456",
    )

    assert settings.environment == "production"


def test_environment_variables_override_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROCESSING_BATCH_SIZE", "250")
    monkeypatch.setenv("LOG_FORMAT", "console")

    settings = Settings(_env_file=None)

    assert settings.processing_batch_size == 250
    assert settings.log_format == "console"


@pytest.mark.parametrize("name", ["MAX_UPLOAD_BYTES", "PROCESSING_BATCH_SIZE", "MAX_ZIP_MEMBERS"])
def test_non_positive_limits_are_rejected(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    monkeypatch.setenv(name, "0")

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_production_requires_an_api_key() -> None:
    with pytest.raises(ValidationError, match="API_KEY"):
        Settings(
            _env_file=None,
            environment="production",
            database_url="postgresql+psycopg://app:secret@db.internal:5432/geo",
        )


def test_production_rejects_non_postgresql_database_urls() -> None:
    with pytest.raises(ValidationError, match=r"postgresql\+psycopg"):
        Settings(
            _env_file=None,
            environment="production",
            database_url="sqlite:///application.db",
            api_key="test-api-key-for-unit-tests-123456",
        )
