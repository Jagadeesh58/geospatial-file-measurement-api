from collections.abc import Iterator
from functools import lru_cache
from pathlib import Path

from sqlalchemy import Engine, create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, Session

from app.core.config import get_settings


class Base(DeclarativeBase):
    pass


def create_database_engine(database_url: str) -> Engine:
    url = make_url(database_url)
    if url.get_backend_name() != "sqlite":
        return create_engine(url)

    if url.database and url.database != ":memory:":
        Path(url.database).parent.mkdir(parents=True, exist_ok=True)
    # Requests run in FastAPI's thread pool, so connections cross threads.
    return create_engine(url, connect_args={"check_same_thread": False})


@lru_cache
def get_engine() -> Engine:
    return create_database_engine(get_settings().database_url)


def get_session() -> Iterator[Session]:
    # Objects stay readable after commit so routes can serialise them without a reload.
    with Session(get_engine(), expire_on_commit=False) as session:
        yield session


def init_db(engine: Engine) -> None:
    Base.metadata.create_all(engine)
