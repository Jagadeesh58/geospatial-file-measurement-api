import os
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.core.config import MEBIBYTE, Settings, get_settings
from app.db import models  # noqa: F401  (registers the tables on Base.metadata)
from app.db.database import Base, get_session
from app.main import create_app
from app.workers.queue import get_job_queue
from tests.queues import InlineJobQueue

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+psycopg://geo:geo@localhost:5432/geo_test"
)
TEST_REDIS_URL = os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/15")


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        database_url=TEST_DATABASE_URL,
        redis_url=TEST_REDIS_URL,
        upload_dir=tmp_path / "uploads",
        max_upload_bytes=MEBIBYTE,
        max_zip_uncompressed_bytes=MEBIBYTE,
        max_zip_members=10,
        # Small batches make every test exercise the multi-batch path.
        processing_batch_size=2,
    )


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    engine = create_engine(TEST_DATABASE_URL)
    try:
        with engine.begin() as connection:
            connection.execute(text("CREATE EXTENSION IF NOT EXISTS postgis"))
    except OperationalError as error:
        if os.environ.get("CI"):
            raise
        pytest.skip(f"PostgreSQL/PostGIS test database unavailable ({TEST_DATABASE_URL}): {error}")
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def database(engine: Engine) -> Iterator[Engine]:
    yield engine
    with engine.begin() as connection:
        connection.execute(
            text("TRUNCATE features, processing_jobs, uploaded_files RESTART IDENTITY CASCADE")
        )


@pytest.fixture
def queue(database: Engine, settings: Settings) -> InlineJobQueue:
    return InlineJobQueue(database, settings)


@pytest.fixture
def client(database: Engine, settings: Settings, queue: InlineJobQueue) -> TestClient:
    app = create_app()
    app.state.settings = settings

    def session_override() -> Iterator[Session]:
        with Session(database, expire_on_commit=False) as session:
            yield session

    app.dependency_overrides[get_session] = session_override
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_job_queue] = lambda: queue
    # Unhandled errors should come back as 500 responses, as they would in production.
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def upload(client: TestClient) -> Callable[[str, bytes], Response]:
    def post_file(filename: str, content: bytes) -> Response:
        return client.post("/api/files/", files={"file": (filename, content)})

    return post_file
