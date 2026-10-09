"""The real Redis queue and worker path, configured through environment variables."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from redis import Redis
from redis.exceptions import RedisError
from rq import Queue, SimpleWorker
from sqlalchemy import Engine

from app.core.config import Settings, get_settings
from app.db.database import get_engine
from app.main import create_app
from app.workers.queue import QUEUE_NAME, QueueUnavailableError, RqJobQueue, get_job_queue
from tests.conftest import TEST_DATABASE_URL, TEST_REDIS_URL
from tests.geodata import FIXTURES


@pytest.fixture
def redis_connection() -> Iterator[Redis]:
    connection = Redis.from_url(TEST_REDIS_URL)
    try:
        connection.ping()
    except RedisError as error:
        pytest.skip(f"Redis unavailable ({TEST_REDIS_URL}): {error}")
    connection.flushdb()
    yield connection
    connection.flushdb()
    connection.close()


@pytest.fixture
def worker_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, database: Engine
) -> Iterator[Settings]:
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("REDIS_URL", TEST_REDIS_URL)
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
    get_settings.cache_clear()
    get_engine.cache_clear()
    yield get_settings()
    get_engine.cache_clear()
    get_settings.cache_clear()


def test_a_queued_upload_is_processed_by_the_worker(
    redis_connection: Redis, worker_environment: Settings
) -> None:
    app = create_app()
    app.dependency_overrides[get_job_queue] = lambda: RqJobQueue(TEST_REDIS_URL, 60)
    client = TestClient(app)

    created = client.post(
        "/api/files/", files={"file": ("survey.kml", (FIXTURES / "survey.kml").read_bytes())}
    ).json()
    assert client.get(f"/api/files/{created['id']}/").json()["status"] == "PENDING"
    assert Queue(QUEUE_NAME, connection=redis_connection).count == 1

    SimpleWorker([QUEUE_NAME], connection=redis_connection).work(burst=True)

    info = client.get(f"/api/files/{created['id']}/").json()
    assert info["status"] == "COMPLETED"
    assert info["feature_count"] == 4
    assert client.get(f"/api/files/{created['id']}/measurements/").json()["total"] == 4


def test_ping_reports_a_reachable_redis(redis_connection: Redis) -> None:
    assert RqJobQueue(TEST_REDIS_URL, 60).ping() is True


def test_an_unreachable_redis_is_reported_not_raised() -> None:
    queue = RqJobQueue("redis://127.0.0.1:1/0", 60)

    assert queue.ping() is False
    with pytest.raises(QueueUnavailableError):
        queue.enqueue("job-id")
