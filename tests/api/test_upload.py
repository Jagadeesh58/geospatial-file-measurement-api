from collections.abc import Callable

import geopandas as gpd
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.models import ProcessingStatus, UploadedFile
from app.workers.queue import QueueUnavailableError
from tests.geodata import FIXTURES, UTM_44N, shapefile_zip, utm_square
from tests.queues import InlineJobQueue

Upload = Callable[[str, bytes], Response]


def test_upload_is_accepted_and_queued(
    upload: Upload, queue: InlineJobQueue, client: TestClient
) -> None:
    queue.run_immediately = False

    response = upload("survey.kml", (FIXTURES / "survey.kml").read_bytes())

    assert response.status_code == 202
    body = response.json()
    assert body["filename"] == "survey.kml"
    assert body["file_type"] == "kml"
    assert body["status"] == "PENDING"
    assert body["feature_count"] == 0
    assert body["crs"] is None
    assert body["job_id"]
    assert body["created_at"].endswith("Z")
    assert response.headers["Location"] == f"/api/files/{body['id']}/"
    assert queue.pending == [body["job_id"]]


def test_kml_upload_is_processed(upload: Upload, client: TestClient) -> None:
    created = upload("survey.kml", (FIXTURES / "survey.kml").read_bytes()).json()

    info = client.get(f"/api/files/{created['id']}/").json()

    assert info["status"] == "COMPLETED"
    assert info["feature_count"] == 4
    assert info["crs"] == "EPSG:4326"
    assert info["error"] is None


def test_zipped_shapefile_upload_is_processed(upload: Upload, client: TestClient) -> None:
    frame = gpd.GeoDataFrame(
        {"name": ["a", "b"]}, geometry=[utm_square(10), utm_square(20, 100)], crs=UTM_44N
    )

    created = upload("plots.zip", shapefile_zip(frame)).json()

    info = client.get(f"/api/files/{created['id']}/").json()
    assert info["file_type"] == "shapefile"
    assert info["status"] == "COMPLETED"
    assert info["feature_count"] == 2
    assert info["crs"] == "EPSG:32644"


def test_stored_upload_is_removed_after_processing(upload: Upload, settings: Settings) -> None:
    upload("survey.kml", (FIXTURES / "survey.kml").read_bytes())

    assert list(settings.upload_dir.iterdir()) == []


def test_unavailable_queue_marks_the_file_failed(
    upload: Upload, queue: InlineJobQueue, database: Engine, settings: Settings
) -> None:
    def refuse(job_id: str) -> None:
        raise QueueUnavailableError("The job queue is unavailable.")

    queue.enqueue = refuse  # type: ignore[method-assign]

    response = upload("survey.kml", (FIXTURES / "survey.kml").read_bytes())

    assert response.status_code == 503
    with Session(database) as session:
        [record] = session.scalars(select(UploadedFile)).all()
    assert record.status is ProcessingStatus.FAILED
    assert list(settings.upload_dir.iterdir()) == []
