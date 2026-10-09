from collections.abc import Iterator
from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import DeclarativeBase, Session

from app.core.config import get_settings


class Base(DeclarativeBase):
    pass


@lru_cache
def get_engine() -> Engine:
    # pool_pre_ping replaces connections that the database dropped while the pool was idle.
    return create_engine(get_settings().database_url, pool_pre_ping=True)


def get_session() -> Iterator[Session]:
    # Objects stay readable after commit so routes can serialise them without a reload.
    with Session(get_engine(), expire_on_commit=False) as session:
        yield session
