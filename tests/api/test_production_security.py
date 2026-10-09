from collections.abc import Iterator
from typing import cast

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api import middleware
from app.api.routes import jobs
from app.core.config import Settings
from app.core.rate_limit import RateLimiterUnavailable
from app.db.database import get_session
from app.main import create_app


class PermissiveRateLimiter:
    def allow(self, client_key: str, limit: int, window_seconds: int) -> tuple[bool, int]:
        return True, window_seconds


def production_settings() -> Settings:
    return Settings(
        _env_file=None,
        environment="production",
        database_url="postgresql+psycopg://app:secret@localhost:5432/geospatial",
        redis_url="redis://localhost:6379/15",
        api_key="test-api-key-for-unit-tests-123456",
    )


def test_production_api_rejects_missing_or_invalid_keys(monkeypatch) -> None:
    monkeypatch.setattr(middleware, "get_rate_limiter", lambda _url: PermissiveRateLimiter())
    app = create_app(production_settings())
    client = TestClient(app, raise_server_exceptions=False)

    assert client.get("/api/jobs/missing/").status_code == 401
    response = client.get("/api/jobs/missing/", headers={"X-API-Key": "wrong"})
    assert response.status_code == 401
    assert response.json() == {"detail": "A valid API key is required."}


def test_production_api_accepts_a_valid_key_before_lookup(monkeypatch) -> None:
    monkeypatch.setattr(middleware, "get_rate_limiter", lambda _url: PermissiveRateLimiter())
    app = create_app(production_settings())

    def empty_session() -> Iterator[Session]:
        yield cast(Session, None)  # The lookup is replaced below; no database is needed.

    app.dependency_overrides[get_session] = empty_session
    monkeypatch.setattr(jobs.file_service, "get_job", lambda _session, _job_id: None)
    client = TestClient(app, raise_server_exceptions=False)
    response = client.get(
        "/api/jobs/missing/", headers={"X-API-Key": "test-api-key-for-unit-tests-123456"}
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "Job not found."}


def test_production_api_returns_429_when_rate_limit_is_exceeded(monkeypatch) -> None:
    class BlockingRateLimiter:
        def allow(self, client_key: str, limit: int, window_seconds: int) -> tuple[bool, int]:
            return False, 17

    monkeypatch.setattr(middleware, "get_rate_limiter", lambda _url: BlockingRateLimiter())
    app = create_app(production_settings())
    client = TestClient(app, raise_server_exceptions=False)

    response = client.get("/api/jobs/missing/")

    assert response.status_code == 429
    assert response.headers["Retry-After"] == "17"


def test_production_api_fails_closed_when_rate_limiter_is_unavailable(monkeypatch) -> None:
    class UnavailableRateLimiter:
        def allow(self, client_key: str, limit: int, window_seconds: int) -> tuple[bool, int]:
            raise RateLimiterUnavailable("Redis is unavailable.")

    monkeypatch.setattr(middleware, "get_rate_limiter", lambda _url: UnavailableRateLimiter())
    app = create_app(production_settings())
    client = TestClient(app, raise_server_exceptions=False)

    response = client.get("/api/jobs/missing/")

    assert response.status_code == 503
    assert response.json() == {"detail": "Request protection is temporarily unavailable."}
