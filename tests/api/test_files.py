from collections.abc import Callable

import geopandas as gpd
import pytest
from fastapi.testclient import TestClient
from httpx import Response

from tests.geodata import FIXTURES, UTM_44N, bowtie, shapefile_zip, utm_square
from tests.queues import InlineJobQueue

Upload = Callable[[str, bytes], Response]


def test_file_info_matches_the_upload_response(client: TestClient, upload: Upload) -> None:
    created = upload("survey.kml", (FIXTURES / "survey.kml").read_bytes()).json()

    response = client.get(f"/api/files/{created['id']}/")

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == created["id"]
    assert body["job_id"] == created["job_id"]
    assert body["filename"] == "survey.kml"


def test_job_reports_state_and_timings(
    client: TestClient, upload: Upload, queue: InlineJobQueue
) -> None:
    queue.run_immediately = False
    created = upload("survey.kml", (FIXTURES / "survey.kml").read_bytes()).json()

    pending = client.get(f"/api/jobs/{created['job_id']}/").json()
    queue.run_pending()
    finished = client.get(f"/api/jobs/{created['job_id']}/").json()

    assert pending["status"] == "PENDING"
    assert pending["started_at"] is None
    assert pending["duration_seconds"] is None
    assert finished["status"] == "COMPLETED"
    assert finished["file_id"] == created["id"]
    assert finished["duration_seconds"] >= 0
    assert finished["error"] is None


@pytest.mark.parametrize(
    "path",
    [
        "/api/files/missing/",
        "/api/files/missing/measurements/",
        "/api/files/missing/features/",
        "/api/jobs/missing/",
    ],
)
def test_unknown_ids_return_404(client: TestClient, path: str) -> None:
    assert client.get(path).status_code == 404


@pytest.mark.parametrize("suffix", ["measurements", "features"])
def test_results_are_not_available_until_processing_finishes(
    client: TestClient, upload: Upload, queue: InlineJobQueue, suffix: str
) -> None:
    queue.run_immediately = False
    created = upload("survey.kml", (FIXTURES / "survey.kml").read_bytes()).json()

    response = client.get(f"/api/files/{created['id']}/{suffix}/")

    assert response.status_code == 409
    assert "not finished" in response.json()["detail"]


@pytest.mark.parametrize("suffix", ["measurements", "features"])
def test_results_of_a_failed_file_return_409_with_the_reason(
    client: TestClient, upload: Upload, suffix: str
) -> None:
    broken = b'<kml xmlns="http://www.opengis.net/kml/2.2"><Document/></kml>'
    created = upload("empty.kml", broken).json()

    info = client.get(f"/api/files/{created['id']}/").json()
    response = client.get(f"/api/files/{created['id']}/{suffix}/")

    assert info["status"] == "FAILED"
    assert info["error"] == "The file contains no features."
    assert response.status_code == 409
    assert "no features" in response.json()["detail"]


def test_one_unmeasurable_feature_does_not_stop_the_others(
    client: TestClient, upload: Upload
) -> None:
    frame = gpd.GeoDataFrame(
        {"name": ["bad", "good", "missing"]},
        geometry=[bowtie(), utm_square(100), None],
        crs=UTM_44N,
    )
    created = upload("plots.zip", shapefile_zip(frame)).json()

    assert client.get(f"/api/files/{created['id']}/").json()["status"] == "COMPLETED"
    measurements = client.get(f"/api/files/{created['id']}/measurements/").json()["measurements"]
    by_index = {item["feature_index"]: item for item in measurements}
    assert by_index[0]["status"] == "FAILED"
    assert "Self-intersection" in by_index[0]["error"]
    assert by_index[1]["status"] == "MEASURED"
    assert by_index[1]["value"] == pytest.approx(10_000.0)
    assert by_index[2]["status"] == "FAILED"
    assert by_index[2]["error"] == "Feature has no geometry."


def test_unexpected_errors_return_a_json_500_with_the_request_id(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(*args: object, **kwargs: object) -> None:
        raise RuntimeError("database exploded at /secret/path")

    monkeypatch.setattr("app.services.files.get_file", explode)

    response = client.get("/api/files/anything/", headers={"X-Request-ID": "trace-9"})

    assert response.status_code == 500
    assert response.json() == {"detail": "Internal server error."}
    assert response.headers["X-Request-ID"] == "trace-9"
