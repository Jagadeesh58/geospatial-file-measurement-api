import io
import zipfile
from collections.abc import Callable

import geopandas as gpd
import pytest
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from app.core.config import MEBIBYTE
from app.db.models import UploadedFile
from tests.geodata import UTM_44N, shapefile_zip, utm_square, zip_archive

Upload = Callable[[str, bytes], Response]

EMPTY_KML = b'<kml xmlns="http://www.opengis.net/kml/2.2"><Document/></kml>'


def _shapefile_members() -> dict[str, bytes]:
    frame = gpd.GeoDataFrame({"name": ["a"]}, geometry=[utm_square(10)], crs=UTM_44N)
    with zipfile.ZipFile(io.BytesIO(shapefile_zip(frame))) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def _assert_rejected(response: Response, message_part: str) -> None:
    assert response.status_code == 400
    assert message_part.lower() in response.json()["detail"].lower()


def test_missing_file_is_a_validation_error(client: TestClient) -> None:
    assert client.post("/api/files/").status_code == 422


@pytest.mark.parametrize("filename", ["notes.txt", "area.geojson", "archive", "photo.png"])
def test_unsupported_extension_is_rejected(upload: Upload, filename: str) -> None:
    _assert_rejected(upload(filename, b"content"), "unsupported file type")


def test_empty_upload_is_rejected(upload: Upload) -> None:
    _assert_rejected(upload("empty.kml", b""), "empty")


def test_upload_over_the_size_limit_is_rejected(upload: Upload) -> None:
    response = upload("big.kml", b"x" * (MEBIBYTE + 1))

    assert response.status_code == 413


def test_malformed_kml_is_rejected(upload: Upload) -> None:
    _assert_rejected(upload("broken.kml", b"<kml><Document><Placemark></Document>"), "kml")


def test_text_named_as_kml_is_rejected(upload: Upload) -> None:
    _assert_rejected(upload("notes.kml", b"just some text"), "not a kml document")


def test_kml_with_a_doctype_is_rejected(upload: Upload) -> None:
    content = b'<?xml version="1.0"?><!DOCTYPE kml [<!ENTITY a "b">]><kml></kml>'

    _assert_rejected(upload("entity.kml", content), "doctype")


def test_kml_without_features_is_rejected(upload: Upload) -> None:
    _assert_rejected(upload("empty.kml", EMPTY_KML), "no features")


def test_invalid_zip_is_rejected(upload: Upload) -> None:
    _assert_rejected(upload("plots.zip", b"this is not a zip"), "not a valid zip")


def test_zip_without_a_shapefile_is_rejected(upload: Upload) -> None:
    archive = zip_archive({"readme.txt": b"hello"})

    _assert_rejected(upload("plots.zip", archive), "does not contain a .shp")


@pytest.mark.parametrize("suffix", [".prj", ".shx", ".dbf"])
def test_shapefile_missing_a_required_component_is_rejected(upload: Upload, suffix: str) -> None:
    members = {n: c for n, c in _shapefile_members().items() if not n.endswith(suffix)}

    _assert_rejected(upload("plots.zip", zip_archive(members)), suffix)


def test_zip_with_two_shapefiles_is_rejected(upload: Upload) -> None:
    members = _shapefile_members()
    members.update({f"second{name[len('layer') :]}": content for name, content in members.items()})

    _assert_rejected(upload("plots.zip", zip_archive(members)), "exactly one shapefile")


@pytest.mark.parametrize("name", ["../layer.shp", "/etc/layer.shp", "..\\layer.shp"])
def test_zip_with_path_traversal_is_rejected(upload: Upload, name: str) -> None:
    members = {**_shapefile_members(), name: b"payload"}

    _assert_rejected(upload("plots.zip", zip_archive(members)), "unsafe file paths")


def test_zip_with_too_many_entries_is_rejected(upload: Upload) -> None:
    members = {f"file{index}.txt": b"x" for index in range(11)}

    _assert_rejected(upload("plots.zip", zip_archive(members)), "more than 10 entries")


def test_zip_that_expands_past_the_limit_is_rejected(upload: Upload) -> None:
    members = {**_shapefile_members(), "padding.txt": bytes(2 * MEBIBYTE)}
    archive = zip_archive(members)
    assert len(archive) < MEBIBYTE

    _assert_rejected(upload("plots.zip", archive), "more data than is allowed")


def test_unreadable_shapefile_content_is_rejected(upload: Upload) -> None:
    members = {**_shapefile_members(), "layer.shp": b"not shapefile data" * 20}

    _assert_rejected(upload("plots.zip", zip_archive(members)), "could not be read")


def test_rejected_uploads_leave_no_record(upload: Upload, engine: Engine) -> None:
    upload("broken.kml", b"<kml><Document>")
    upload("plots.zip", b"this is not a zip")

    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(UploadedFile)) == 0
