import io
import zipfile
from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient
from httpx import Response

from app.core.config import MEBIBYTE, Settings
from tests.api.test_validation import assert_rejected, shapefile_members
from tests.geodata import FIXTURES, zip_archive

Upload = Callable[[str, bytes], Response]


@pytest.mark.parametrize(
    "name",
    [
        "../layer.shp",
        "/etc/layer.shp",
        "..\\layer.shp",
        "a/../../b.shp",
        "C:\\Windows\\layer.shp",
    ],
)
def test_path_traversal_entries_are_rejected(upload: Upload, name: str) -> None:
    members = {**shapefile_members(), name: b"payload"}

    assert_rejected(upload("plots.zip", zip_archive(members)), "unsafe file paths")


def test_multiple_shapefiles_are_rejected(upload: Upload) -> None:
    members = shapefile_members()
    duplicate_name = next(name for name in members if name.endswith(".shp"))
    members[f"nested/{duplicate_name}"] = members[duplicate_name]

    assert_rejected(upload("plots.zip", zip_archive(members)), "exactly one Shapefile")


def test_duplicate_components_within_one_dataset_are_rejected(upload: Upload) -> None:
    members = shapefile_members()
    shapefile_name = next(name for name in members if name.endswith(".shp"))
    members[f"{shapefile_name.upper()}"] = members[shapefile_name]

    assert_rejected(
        upload("plots.zip", zip_archive(members)), "duplicate Shapefile components"
    )


def test_too_many_entries_are_rejected(upload: Upload) -> None:
    members = {f"file{index}.txt": b"x" for index in range(11)}

    assert_rejected(upload("plots.zip", zip_archive(members)), "more than 10 entries")


def test_archives_that_expand_past_the_limit_are_rejected(
    upload: Upload, settings: Settings
) -> None:
    members = {**shapefile_members(), "padding.txt": bytes(2 * MEBIBYTE)}
    archive = zip_archive(members)
    assert len(archive) < settings.max_upload_bytes

    assert_rejected(upload("plots.zip", archive), "more data than is allowed")


def test_archives_with_a_dishonest_size_header_are_stopped_while_extracting(
    client: TestClient, upload: Upload, settings: Settings
) -> None:
    members = shapefile_members()
    members["layer.dbf"] = bytes(2 * MEBIBYTE)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    # Rewrite the declared size of the big member to a small number, as a hostile client could.
    raw = bytearray(buffer.getvalue())
    declared = (2 * MEBIBYTE).to_bytes(4, "little")
    liar = (100).to_bytes(4, "little")
    position = raw.find(declared)
    while position != -1:
        raw[position : position + 4] = liar
        position = raw.find(declared, position + 4)

    response = upload("plots.zip", bytes(raw))

    # Either the tampered archive is refused at upload or the job fails; it never succeeds.
    if response.status_code == 202:
        assert client.get(f"/api/files/{response.json()['id']}/").json()["status"] == "FAILED"
    else:
        assert response.status_code == 400


def test_encrypted_entries_are_rejected(upload: Upload) -> None:
    archive = bytearray(zip_archive(shapefile_members()))
    # zipfile cannot write encrypted entries, so set the "encrypted" bit in every
    # central-directory record (general purpose flags sit 8 bytes into each record).
    position = archive.find(b"PK\x01\x02")
    while position != -1:
        archive[position + 8] |= 0x1
        position = archive.find(b"PK\x01\x02", position + 4)

    assert_rejected(upload("plots.zip", bytes(archive)), "encrypted")


def test_macos_metadata_entries_are_ignored(upload: Upload) -> None:
    members = {
        **{f"survey/{n}": c for n, c in shapefile_members().items()},
        "__MACOSX/survey/._layer.shp": b"resource fork data",
    }

    assert upload("plots.zip", zip_archive(members)).status_code == 202


def test_client_filename_is_never_used_as_a_path(
    client: TestClient, upload: Upload, settings: Settings
) -> None:
    response = upload("../../evil.kml", (FIXTURES / "survey.kml").read_bytes())

    assert response.status_code == 202
    assert response.json()["filename"] == "evil.kml"
    assert not (settings.upload_dir.parent.parent / "evil.kml").exists()
    assert list(settings.upload_dir.iterdir()) == []


def test_responses_do_not_expose_storage_details(client: TestClient, upload: Upload) -> None:
    created = upload("survey.kml", (FIXTURES / "survey.kml").read_bytes()).json()

    info = client.get(f"/api/files/{created['id']}/").json()

    assert "storage_name" not in info
    assert "path" not in info
    assert str(created["id"]) not in info["filename"]
