"""Guards against N+1 queries and row-by-row inserts."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager

from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import Engine, event

from app.core.config import Settings
from tests.geodata import shapefile_zip, squares_frame
from tests.queues import InlineJobQueue

Upload = Callable[[str, bytes], Response]


@contextmanager
def recorded_statements(engine: Engine) -> Iterator[list[str]]:
    statements: list[str] = []

    def record(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", record)


def test_features_are_inserted_in_batches_not_row_by_row(
    upload: Upload, database: Engine, settings: Settings, queue: InlineJobQueue
) -> None:
    queue.run_immediately = False
    settings.processing_batch_size = 10
    upload("plots.zip", shapefile_zip(squares_frame(25)))

    with recorded_statements(database) as statements:
        queue.run_pending()

    inserts = [s for s in statements if s.startswith("INSERT INTO features")]
    assert len(inserts) == 3  # 25 features in batches of 10


def test_a_page_of_results_needs_a_fixed_number_of_queries(
    client: TestClient, upload: Upload, database: Engine
) -> None:
    file_id = upload("plots.zip", shapefile_zip(squares_frame(30))).json()["id"]

    with recorded_statements(database) as statements:
        client.get(f"/api/files/{file_id}/measurements/?limit=30")
        measurement_queries = len(statements)
        statements.clear()
        client.get(f"/api/files/{file_id}/features/?limit=30")
        feature_queries = len(statements)

    # file lookup (with its job), the page, and the total: independent of the page size.
    assert measurement_queries == 3
    assert feature_queries == 3
