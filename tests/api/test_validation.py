import io
import zipfile
from collections.abc import Callable

import geopandas as gpd
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from app.core.config import MEBIBYTE, Settings
from app.db.models import UploadedFile
from tests.geodata import UTM_44N, shapefile_zip, utm_square, zip_archive

Upload = Callable[[str, bytes], Response]

EMPTY_KML = b'<kml xmlns="http://www.opengis.net/kml/2.2"><Document/></kml>'


def shapefile_members() -> dict[str, bytes]:
    frame = gpd.GeoDataFrame({"name": ["a"]}, geometry=[utm_square(10)], crs=UTM_44N)
    with zipfile.ZipFile(io.BytesIO(shapefile_zip(frame))) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def assert_rejected(response: Response, message_part: str) -> None:
    assert response.status_code == 400
    assert message_part.lower() in response.json()["detail"].lower()


def failed_reason(client: TestClient, upload_response: Response) -> str:
    assert upload_response.status_code == 202
    info = client.get(f"/api/files/{upload_response.json()['id']}/").json()
    assert info["status"] == "FAILED"
    reason: str = info["error"]
    return reason


def test_missing_file_is_a_validation_error(client: TestClient) -> None:
    assert client.post("/api/files/").status_code == 422


def test_unsupported_extensions_are_rejected(upload: Upload) -> None:
    for filename in ("notes.txt", "area.geojson", "archive", "photo.png"):
        assert_rejected(upload(filename, b"content"), "unsupported file type")


def test_empty_upload_is_rejected(upload: Upload) -> None:
    assert_rejected(upload("empty.kml", b""), "empty")


def test_upload_over_the_size_limit_is_rejected(upload: Upload, settings: Settings) -> None:
    response = upload("big.kml", b"x" * (settings.max_upload_bytes + 1))

    assert response.status_code == 413
    assert list(settings.upload_dir.iterdir()) == []


def test_text_named_as_kml_is_rejected(upload: Upload) -> None:
    assert_rejected(upload("notes.kml", b"just some text"), "not a kml document")


def test_kml_with_a_doctype_is_rejected(upload: Upload) -> None:
    content = b'<?xml version="1.0"?><!DOCTYPE kml [<!ENTITY a "b">]><kml></kml>'

    assert_rejected(upload("entity.kml", content), "doctype")


def test_malformed_kml_fails_the_job_without_leaking_paths(
    client: TestClient, upload: Upload
) -> None:
    response = upload("broken.kml", b"<kml><Document><Placemark></Document>")

    reason = failed_reason(client, response)
    assert "could not be read" in reason
    assert "/" not in reason


def test_kml_without_features_fails_the_job(client: TestClient, upload: Upload) -> None:
    assert failed_reason(client, upload("empty.kml", EMPTY_KML)) == "The file contains no features."


def test_invalid_zip_is_rejected(upload: Upload) -> None:
    assert_rejected(upload("plots.zip", b"this is not a zip"), "not a valid zip")


def test_zip_without_a_shapefile_is_rejected(upload: Upload) -> None:
    assert_rejected(
        upload("plots.zip", zip_archive({"readme.txt": b"hello"})), "does not contain a .shp"
    )


def test_shapefile_missing_a_required_component_is_rejected(upload: Upload) -> None:
    for suffix in (".prj", ".shx", ".dbf"):
        members = {n: c for n, c in shapefile_members().items() if not n.endswith(suffix)}

        assert_rejected(upload("plots.zip", zip_archive(members)), suffix)


def test_unreadable_shapefile_content_fails_the_job(client: TestClient, upload: Upload) -> None:
    members = {**shapefile_members(), "layer.shp": b"not shapefile data" * 20}

    reason = failed_reason(client, upload("plots.zip", zip_archive(members)))

    assert "could not be read" in reason
    assert "/" not in reason


def test_rejected_uploads_leave_no_record_or_file(
    upload: Upload, database: Engine, settings: Settings
) -> None:
    upload("notes.kml", b"just some text")
    upload("plots.zip", b"this is not a zip")
    upload("big.kml", b"x" * (MEBIBYTE + 1))

    with Session(database) as session:
        assert session.scalar(select(func.count()).select_from(UploadedFile)) == 0
    assert list(settings.upload_dir.iterdir()) == []


def test_declared_oversized_bodies_are_refused_before_reading_them(
    upload: Upload, settings: Settings
) -> None:
    response = upload("big.kml", b"x" * (settings.max_upload_bytes + 128 * 1024))

    assert response.status_code == 413
