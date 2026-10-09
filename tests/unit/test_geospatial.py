import io
import zipfile
from pathlib import Path

import geopandas as gpd
import pytest
from shapely.geometry import Point

from app.core.config import Settings
from app.db.models import FileType
from app.services.geospatial import (
    InvalidGeospatialFile,
    describe_crs,
    detect_file_type,
    open_dataset,
    validate_upload,
)
from tests.geodata import FIXTURES, UTM_44N, shapefile_zip, squares_frame, zip_archive


def write(directory: Path, name: str, content: bytes) -> Path:
    path = directory / name
    path.write_bytes(content)
    return path


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("a.kml", FileType.KML),
        ("A.KML", FileType.KML),
        ("a.zip", FileType.SHAPEFILE),
        ("dir/a.b.ZIP", FileType.SHAPEFILE),
    ],
)
def test_detect_file_type(filename: str, expected: FileType) -> None:
    assert detect_file_type(filename) is expected


@pytest.mark.parametrize("filename", ["a.shp", "a.txt", "kml", ""])
def test_detect_file_type_rejects_other_names(filename: str) -> None:
    with pytest.raises(InvalidGeospatialFile, match="Unsupported file type"):
        detect_file_type(filename)


def test_kml_layers_are_read_separately_with_continuous_indexes(settings: Settings) -> None:
    path = FIXTURES / "survey.kml"

    with open_dataset(FileType.KML, path, settings) as dataset:
        batches = list(dataset.batches(batch_size=3))

    # The file has three layers (two folders and the document itself); batches never span layers.
    assert [len(batch.features) for batch in batches] == [2, 1, 1]
    assert [feature.index for batch in batches for feature in batch.features] == [0, 1, 2, 3]
    assert describe_crs(batches[0].crs) == "EPSG:4326"
    names = {feature.properties["Name"] for batch in batches for feature in batch.features}
    assert names == {"Plot A", "Access road", "Gate", "Marker with route"}


def test_shapefile_is_read_in_batches(tmp_path: Path, settings: Settings) -> None:
    archive = write(tmp_path, "plots.zip", shapefile_zip(squares_frame(5)))

    with open_dataset(FileType.SHAPEFILE, archive, settings) as dataset:
        batches = list(dataset.batches(batch_size=2))

    assert [len(batch.features) for batch in batches] == [2, 2, 1]
    indexes = [feature.index for batch in batches for feature in batch.features]
    assert indexes == [0, 1, 2, 3, 4]
    assert describe_crs(batches[0].crs) == "EPSG:32644"


def test_exact_multiple_of_the_batch_size_yields_no_empty_batch(
    tmp_path: Path, settings: Settings
) -> None:
    archive = write(tmp_path, "plots.zip", shapefile_zip(squares_frame(4)))

    with open_dataset(FileType.SHAPEFILE, archive, settings) as dataset:
        batches = list(dataset.batches(batch_size=2))

    assert [len(batch.features) for batch in batches] == [2, 2]


def test_attribute_values_become_json_safe(tmp_path: Path, settings: Settings) -> None:
    frame = gpd.GeoDataFrame(
        {"label": ["a", "b"], "reading": [1.5, None]},
        geometry=[Point(500_000, 1_830_000), Point(500_010, 1_830_000)],
        crs=UTM_44N,
    )
    archive = write(tmp_path, "points.zip", shapefile_zip(frame))

    with open_dataset(FileType.SHAPEFILE, archive, settings) as dataset:
        [batch] = list(dataset.batches(batch_size=10))

    assert [feature.properties for feature in batch.features] == [
        {"label": "a", "reading": 1.5},
        {"label": "b", "reading": None},
    ]


def test_working_directory_is_removed_after_reading(
    tmp_path: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    created: list[Path] = []
    real_extract = __import__("app.services.geospatial", fromlist=["x"])._extract_shapefile

    def spy(path: Path, directory: Path, config: Settings) -> Path:
        created.append(directory)
        return real_extract(path, directory, config)

    monkeypatch.setattr("app.services.geospatial._extract_shapefile", spy)
    archive = write(tmp_path, "plots.zip", shapefile_zip(squares_frame(1)))

    with open_dataset(FileType.SHAPEFILE, archive, settings) as dataset:
        list(dataset.batches(batch_size=10))

    assert created
    assert not created[0].exists()


def test_validate_upload_accepts_a_good_archive(tmp_path: Path, settings: Settings) -> None:
    archive = write(tmp_path, "plots.zip", shapefile_zip(squares_frame(1)))

    validate_upload(FileType.SHAPEFILE, archive, settings)


def test_validate_upload_names_the_missing_component(tmp_path: Path, settings: Settings) -> None:
    with zipfile.ZipFile(io.BytesIO(shapefile_zip(squares_frame(1)))) as source:
        members = {n: source.read(n) for n in source.namelist() if not n.endswith(".prj")}
    archive = write(tmp_path, "plots.zip", zip_archive(members))

    with pytest.raises(InvalidGeospatialFile, match=r"\.prj"):
        validate_upload(FileType.SHAPEFILE, archive, settings)


def test_validate_upload_rejects_text_named_as_kml(tmp_path: Path, settings: Settings) -> None:
    path = write(tmp_path, "notes.kml", b"hello")

    with pytest.raises(InvalidGeospatialFile, match="not a KML"):
        validate_upload(FileType.KML, path, settings)
