"""Upload -> processing -> database -> query, against a real PostgreSQL/PostGIS database."""

from collections.abc import Callable

import geopandas as gpd
import pytest
from fastapi.testclient import TestClient
from httpx import Response
from shapely.geometry.base import BaseGeometry
from sqlalchemy import Engine, func, select, text
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.models import Feature, FeatureStatus, ProcessingJob, ProcessingStatus, UploadedFile
from app.services.measurement import FeatureMeasurer, FeatureResult
from tests.geodata import FIXTURES, UTM_44N, bowtie, shapefile_zip, squares_frame, utm_square
from tests.queues import InlineJobQueue

Upload = Callable[[str, bytes], Response]


def count(engine: Engine, model: type) -> int:
    with Session(engine) as session:
        return session.scalar(select(func.count()).select_from(model)) or 0


def test_job_moves_from_pending_to_completed(
    client: TestClient, upload: Upload, queue: InlineJobQueue, database: Engine
) -> None:
    queue.run_immediately = False
    created = upload("plots.zip", shapefile_zip(squares_frame(3))).json()
    assert count(database, Feature) == 0

    queue.run_pending()

    with Session(database) as session:
        job = session.get(ProcessingJob, created["job_id"])
        record = session.get(UploadedFile, created["id"])
        assert job is not None and record is not None
        assert job.status is ProcessingStatus.COMPLETED
        assert job.started_at is not None and job.finished_at is not None
        assert job.finished_at >= job.started_at
        assert record.status is ProcessingStatus.COMPLETED
        assert record.feature_count == 3
    assert count(database, Feature) == 3


def test_every_batch_is_stored_with_contiguous_indexes(
    client: TestClient, upload: Upload, database: Engine
) -> None:
    created = upload("plots.zip", shapefile_zip(squares_frame(7))).json()

    with Session(database) as session:
        indexes = session.scalars(
            select(Feature.feature_index)
            .where(Feature.file_id == created["id"])
            .order_by(Feature.feature_index)
        ).all()
    assert list(indexes) == list(range(7))


def test_wgs84_index_geometry_is_stored_for_spatial_queries(
    client: TestClient, upload: Upload, database: Engine
) -> None:
    created = upload("survey.kml", (FIXTURES / "survey.kml").read_bytes()).json()

    with database.connect() as connection:
        stored = connection.execute(
            text(
                "SELECT count(*) FROM features WHERE file_id = :id AND geom IS NOT NULL "
                "AND ST_SRID(geom) = 4326"
            ),
            {"id": created["id"]},
        ).scalar_one()
    assert stored == 4


def test_failed_features_are_stored_next_to_measured_ones(
    client: TestClient, upload: Upload, database: Engine
) -> None:
    frame = gpd.GeoDataFrame(
        {"name": ["bad", "good", "missing"]},
        geometry=[bowtie(), utm_square(100), None],
        crs=UTM_44N,
    )
    created = upload("plots.zip", shapefile_zip(frame)).json()

    with Session(database) as session:
        statuses = session.scalars(
            select(Feature.status)
            .where(Feature.file_id == created["id"])
            .order_by(Feature.feature_index)
        ).all()
    assert list(statuses) == [FeatureStatus.FAILED, FeatureStatus.MEASURED, FeatureStatus.FAILED]


def test_a_crash_midway_rolls_back_every_feature(
    client: TestClient,
    upload: Upload,
    database: Engine,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_measure = FeatureMeasurer.measure
    calls = 0

    def crash_on_fifth_feature(
        self: FeatureMeasurer, geometry: BaseGeometry | None
    ) -> FeatureResult:
        nonlocal calls
        calls += 1
        if calls == 5:
            raise RuntimeError("simulated crash")
        return real_measure(self, geometry)

    monkeypatch.setattr(FeatureMeasurer, "measure", crash_on_fifth_feature)

    created = upload("plots.zip", shapefile_zip(squares_frame(7))).json()

    # Two batches had already been inserted when the crash happened; none may remain.
    assert count(database, Feature) == 0
    info = client.get(f"/api/files/{created['id']}/").json()
    assert info["status"] == "FAILED"
    assert info["error"] == "Unexpected error while processing the file."
    assert "simulated" not in info["error"]
    job = client.get(f"/api/jobs/{created['job_id']}/").json()
    assert job["status"] == "FAILED"
    assert list(settings.upload_dir.iterdir()) == []


def test_a_job_that_is_delivered_twice_runs_once(
    client: TestClient, upload: Upload, queue: InlineJobQueue, database: Engine
) -> None:
    created = upload("plots.zip", shapefile_zip(squares_frame(3))).json()

    queue.enqueue(created["job_id"])

    assert count(database, Feature) == 3
    assert client.get(f"/api/files/{created['id']}/").json()["status"] == "COMPLETED"


def test_unknown_job_ids_are_ignored(queue: InlineJobQueue, database: Engine) -> None:
    queue.enqueue("does-not-exist")

    assert count(database, ProcessingJob) == 0
