import io
import zipfile
from collections.abc import Callable

import geopandas as gpd
import pytest
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.db.models import FileStatus, UploadedFile
from tests.geodata import FIXTURES, UTM_44N, shapefile_zip, utm_square, zip_archive

Upload = Callable[[str, bytes], Response]


def test_kml_upload_is_processed(upload: Upload) -> None:
    response = upload("survey.kml", (FIXTURES / "survey.kml").read_bytes())

    assert response.status_code == 201
    body = response.json()
    assert body["filename"] == "survey.kml"
    assert body["file_type"] == "kml"
    assert body["status"] == "COMPLETED"
    assert body["feature_count"] == 4
    assert body["crs"] == "EPSG:4326"
    assert body["error"] is None
    assert body["created_at"].endswith("Z")


def test_zipped_shapefile_upload_is_processed(upload: Upload) -> None:
    frame = gpd.GeoDataFrame(
        {"name": ["a", "b"]}, geometry=[utm_square(10), utm_square(20, 100)], crs=UTM_44N
    )

    response = upload("plots.zip", shapefile_zip(frame))

    assert response.status_code == 201
    body = response.json()
    assert body["file_type"] == "shapefile"
    assert body["status"] == "COMPLETED"
    assert body["feature_count"] == 2
    assert body["crs"] == "EPSG:32644"


def test_shapefile_inside_a_folder_with_macos_metadata_is_accepted(upload: Upload) -> None:
    frame = gpd.GeoDataFrame({"name": ["a"]}, geometry=[utm_square(10)], crs=UTM_44N)
    flat_archive = shapefile_zip(frame)
    with zipfile.ZipFile(io.BytesIO(flat_archive)) as source:
        members = {f"survey/{name}": source.read(name) for name in source.namelist()}
    members["__MACOSX/survey/._layer.shp"] = b"resource fork data"

    response = upload("plots.zip", zip_archive(members))

    assert response.status_code == 201
    assert response.json()["feature_count"] == 1


def test_file_info_matches_the_upload_response(client: TestClient, upload: Upload) -> None:
    created = upload("survey.kml", (FIXTURES / "survey.kml").read_bytes()).json()

    response = client.get(f"/api/files/{created['id']}/")

    assert response.status_code == 200
    assert response.json() == created


def test_unknown_file_returns_404(client: TestClient) -> None:
    assert client.get("/api/files/does-not-exist/").status_code == 404
    assert client.get("/api/files/does-not-exist/measurements/").status_code == 404


def test_unexpected_processing_failure_is_recorded_on_the_file(
    client: TestClient,
    upload: Upload,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def explode(*args: object, **kwargs: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr("app.services.files.measure_feature", explode)

    response = upload("survey.kml", (FIXTURES / "survey.kml").read_bytes())

    assert response.status_code == 500
    assert response.json() == {"detail": "Internal server error."}
    with Session(engine) as session:
        [record] = session.scalars(select(UploadedFile)).all()
    assert record.status is FileStatus.FAILED

    info = client.get(f"/api/files/{record.id}/").json()
    assert info["status"] == "FAILED"
    assert info["error"]
    assert "boom" not in info["error"]
    assert client.get(f"/api/files/{record.id}/measurements/").status_code == 409
