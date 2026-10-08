from collections.abc import Callable, Iterator

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.config import MEBIBYTE, Settings, get_settings
from app.db.database import get_session, init_db
from app.main import create_app


@pytest.fixture
def settings() -> Settings:
    return Settings(
        database_url="sqlite://",
        max_upload_bytes=MEBIBYTE,
        max_zip_uncompressed_bytes=MEBIBYTE,
        max_zip_members=10,
    )


@pytest.fixture
def engine() -> Engine:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    init_db(engine)
    return engine


@pytest.fixture
def client(engine: Engine, settings: Settings) -> Iterator[TestClient]:
    app = create_app()

    def session_override() -> Iterator[Session]:
        with Session(engine, expire_on_commit=False) as session:
            yield session

    app.dependency_overrides[get_session] = session_override
    app.dependency_overrides[get_settings] = lambda: settings
    # Unhandled errors should come back as 500 responses, as they would in production.
    yield TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def upload(client: TestClient) -> Callable[[str, bytes], Response]:
    def post_file(filename: str, content: bytes) -> Response:
        return client.post("/api/files/", files={"file": (filename, content)})

    return post_file
