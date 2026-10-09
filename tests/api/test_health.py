import logging

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.db.database import get_session
from tests.queues import InlineJobQueue


def test_health_reports_ok(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ready_when_dependencies_respond(client: TestClient) -> None:
    response = client.get("/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready", "checks": {"database": True, "queue": True}}


def test_not_ready_when_the_queue_is_down(client: TestClient, queue: InlineJobQueue) -> None:
    queue.ping = lambda: False  # type: ignore[method-assign]

    response = client.get("/ready")

    assert response.status_code == 503
    assert response.json()["checks"] == {"database": True, "queue": False}


def test_every_response_carries_a_request_id(client: TestClient) -> None:
    generated = client.get("/health").headers["X-Request-ID"]
    echoed = client.get("/health", headers={"X-Request-ID": "trace-123"}).headers["X-Request-ID"]
    replaced = client.get("/health", headers={"X-Request-ID": "bad id\n"}).headers["X-Request-ID"]

    assert generated
    assert echoed == "trace-123"
    assert replaced not in {"", "bad id\n"}


def test_not_ready_when_the_database_is_down(client: TestClient, database: Engine) -> None:
    class BrokenSession(Session):
        def execute(self, *args: object, **kwargs: object):
            raise OperationalError("SELECT 1", {}, Exception("connection refused"))

    def broken_session():
        with BrokenSession(database) as session:
            yield session

    client.app.dependency_overrides[get_session] = broken_session  # type: ignore[attr-defined]

    response = client.get("/ready")

    assert response.status_code == 503
    assert response.json()["checks"] == {"database": False, "queue": True}


def test_access_log_lines_carry_the_request_id(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO):
        client.get("/health", headers={"X-Request-ID": "trace-77"})

    [record] = [r for r in caplog.records if r.getMessage() == "request_completed"]
    assert record.request_id == "trace-77"  # type: ignore[attr-defined]
    assert record.path == "/health"  # type: ignore[attr-defined]
    assert record.status_code == 200  # type: ignore[attr-defined]
