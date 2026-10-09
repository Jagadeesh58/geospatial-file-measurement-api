import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

from app.core.config import get_settings
from app.db.database import Base
from tests.conftest import TEST_DATABASE_URL

MIGRATION_DATABASE = "geo_migration_check"
ALEMBIC_INI = Path(__file__).parents[2] / "alembic.ini"


@pytest.fixture
def migration_url(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    admin_url = make_url(TEST_DATABASE_URL).set(database="postgres")
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as connection:
            connection.execute(text(f"DROP DATABASE IF EXISTS {MIGRATION_DATABASE}"))
            connection.execute(text(f"CREATE DATABASE {MIGRATION_DATABASE}"))
    except OperationalError as error:
        if os.environ.get("CI"):
            raise
        pytest.skip(f"PostgreSQL unavailable: {error}")

    url = make_url(TEST_DATABASE_URL).set(database=MIGRATION_DATABASE)
    monkeypatch.setenv("DATABASE_URL", url.render_as_string(hide_password=False))
    get_settings.cache_clear()
    yield url.render_as_string(hide_password=False)
    get_settings.cache_clear()
    with admin.connect() as connection:
        connection.execute(text(f"DROP DATABASE IF EXISTS {MIGRATION_DATABASE}"))
    admin.dispose()


def test_migrations_create_the_schema_the_models_expect(migration_url: str) -> None:
    command.upgrade(Config(str(ALEMBIC_INI)), "head")

    engine = create_engine(migration_url)
    inspector = inspect(engine)
    assert {"uploaded_files", "processing_jobs", "features"} <= set(inspector.get_table_names())
    for table in Base.metadata.sorted_tables:
        expected = {column.name for column in table.columns}
        actual = {column["name"] for column in inspector.get_columns(table.name)}
        assert actual == expected, table.name
    index_names = {index["name"] for index in inspector.get_indexes("features")}
    assert {"ix_features_geom", "ix_features_file_id_status"} <= index_names
    engine.dispose()


def test_migrations_can_be_reverted(migration_url: str) -> None:
    config = Config(str(ALEMBIC_INI))
    command.upgrade(config, "head")

    command.downgrade(config, "base")

    engine = create_engine(migration_url)
    assert "features" not in inspect(engine).get_table_names()
    engine.dispose()
