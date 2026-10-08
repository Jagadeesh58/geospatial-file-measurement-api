"""Reads uploaded KML files and zipped Shapefiles into plain feature records."""

import io
import tempfile
import zipfile
import zlib
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path, PurePosixPath
from typing import Any

import geopandas as gpd
import pandas as pd
from pyogrio.errors import DataLayerError, DataSourceError
from pyproj import CRS
from shapely.geometry.base import BaseGeometry

from app.core.config import Settings
from app.db.models import FileType

KML_HEADER_BYTES = 64 * 1024
COPY_CHUNK_BYTES = 64 * 1024
REQUIRED_SHAPEFILE_SUFFIXES = (".shp", ".shx", ".dbf", ".prj")
SHAPEFILE_SUFFIXES = {*REQUIRED_SHAPEFILE_SUFFIXES, ".cpg"}
ARCHIVE_EXTENSIONS = {".kml": FileType.KML, ".zip": FileType.SHAPEFILE}


class InvalidGeospatialFile(Exception):
    """The upload is not a usable KML file or Shapefile archive."""


@dataclass(frozen=True)
class SourceFeature:
    index: int
    geometry: BaseGeometry | None
    properties: dict[str, Any]


@dataclass(frozen=True)
class GeospatialDataset:
    crs: CRS
    features: list[SourceFeature]


def detect_file_type(filename: str) -> FileType:
    extension = Path(filename).suffix.lower()
    try:
        return ARCHIVE_EXTENSIONS[extension]
    except KeyError:
        raise InvalidGeospatialFile(
            f"Unsupported file type '{extension}'. Upload a .kml file or a .zip Shapefile."
        ) from None


def describe_crs(crs: CRS) -> str:
    authority = crs.to_authority()
    return f"{authority[0]}:{authority[1]}" if authority else crs.name


def load_dataset(file_type: FileType, content: bytes, settings: Settings) -> GeospatialDataset:
    with tempfile.TemporaryDirectory(prefix="geofile-") as work_directory:
        directory = Path(work_directory)
        if file_type is FileType.KML:
            frame = _read_kml(content, directory)
        else:
            frame = _read_shapefile(content, directory, settings)

    if frame.empty:
        raise InvalidGeospatialFile("The file contains no features.")
    if frame.crs is None:
        raise InvalidGeospatialFile("The file does not declare a coordinate reference system.")
    return GeospatialDataset(crs=frame.crs, features=_extract_features(frame))


def _read_kml(content: bytes, directory: Path) -> gpd.GeoDataFrame:
    _check_kml_content(content)
    path = directory / "upload.kml"
    path.write_bytes(content)
    try:
        # GDAL exposes every KML folder as a separate layer, so all of them are read.
        layer_names = gpd.list_layers(path)["name"]
        frames = [gpd.read_file(path, layer=name) for name in layer_names]
    except (DataSourceError, DataLayerError) as error:
        raise InvalidGeospatialFile(f"The KML file could not be read: {error}") from error

    frames = [frame for frame in frames if not frame.empty]
    if not frames:
        return gpd.GeoDataFrame()
    return pd.concat(frames, ignore_index=True)


def _check_kml_content(content: bytes) -> None:
    header = content[:KML_HEADER_BYTES]
    root_position = header.find(b"<kml")
    if root_position < 0:
        raise InvalidGeospatialFile("The file is not a KML document.")
    # KML never needs a DTD; refusing one rules out entity-expansion attacks.
    if b"<!DOCTYPE" in header[:root_position]:
        raise InvalidGeospatialFile("KML documents with a DOCTYPE declaration are not accepted.")


def _read_shapefile(content: bytes, directory: Path, settings: Settings) -> gpd.GeoDataFrame:
    shapefile_path = _extract_shapefile(content, directory, settings)
    try:
        return gpd.read_file(shapefile_path)
    except (DataSourceError, DataLayerError) as error:
        raise InvalidGeospatialFile(f"The Shapefile could not be read: {error}") from error


def _extract_shapefile(content: bytes, directory: Path, settings: Settings) -> Path:
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile as error:
        raise InvalidGeospatialFile("The upload is not a valid ZIP archive.") from error

    with archive:
        parts = _select_shapefile_parts(archive, settings)
        remaining_bytes = settings.max_zip_uncompressed_bytes
        try:
            for suffix, member in parts.items():
                # Member names never reach the filesystem; every part is written under a
                # fixed name inside the temporary directory.
                with (
                    archive.open(member) as source,
                    (directory / f"dataset{suffix}").open("wb") as target,
                ):
                    while chunk := source.read(COPY_CHUNK_BYTES):
                        remaining_bytes -= len(chunk)
                        if remaining_bytes < 0:
                            raise InvalidGeospatialFile(
                                "The ZIP archive expands to more data than is allowed."
                            )
                        target.write(chunk)
        except (zipfile.BadZipFile, zlib.error, NotImplementedError) as error:
            raise InvalidGeospatialFile(
                "The ZIP archive is corrupt or uses an unsupported compression method."
            ) from error
    return directory / "dataset.shp"


def _select_shapefile_parts(
    archive: zipfile.ZipFile, settings: Settings
) -> dict[str, zipfile.ZipInfo]:
    members = archive.infolist()
    if len(members) > settings.max_zip_members:
        raise InvalidGeospatialFile(
            f"The ZIP archive has more than {settings.max_zip_members} entries."
        )
    if sum(member.file_size for member in members) > settings.max_zip_uncompressed_bytes:
        raise InvalidGeospatialFile("The ZIP archive expands to more data than is allowed.")

    datasets: dict[str, dict[str, zipfile.ZipInfo]] = {}
    for member in members:
        path = PurePosixPath(member.filename.replace("\\", "/"))
        if path.is_absolute() or ".." in path.parts:
            raise InvalidGeospatialFile("The ZIP archive contains unsafe file paths.")
        if member.is_dir() or _is_archive_metadata(path):
            continue
        suffix = path.suffix.lower()
        if suffix not in SHAPEFILE_SUFFIXES:
            continue
        if member.flag_bits & 0x1:
            raise InvalidGeospatialFile("Encrypted ZIP archives are not supported.")
        stem = str(path.with_suffix("")).lower()
        datasets.setdefault(stem, {})[suffix] = member

    shapefiles = {stem: parts for stem, parts in datasets.items() if ".shp" in parts}
    if not shapefiles:
        raise InvalidGeospatialFile("The ZIP archive does not contain a .shp file.")
    if len(shapefiles) > 1:
        raise InvalidGeospatialFile("The ZIP archive must contain exactly one Shapefile.")

    parts = next(iter(shapefiles.values()))
    missing = [suffix for suffix in REQUIRED_SHAPEFILE_SUFFIXES if suffix not in parts]
    if missing:
        raise InvalidGeospatialFile(
            f"The Shapefile is missing required component(s): {', '.join(missing)}. "
            "The .prj file is needed to determine the coordinate reference system."
        )
    return parts


def _is_archive_metadata(path: PurePosixPath) -> bool:
    # macOS adds a __MACOSX folder of "._name.shp" resource forks that look like Shapefiles.
    return path.parts[0] == "__MACOSX" or path.name.startswith("._")


def _extract_features(frame: gpd.GeoDataFrame) -> list[SourceFeature]:
    attributes = frame.drop(columns=frame.geometry.name)
    column_names = [str(name) for name in attributes.columns]
    rows = attributes.itertuples(index=False, name=None)
    return [
        SourceFeature(
            index=index,
            geometry=geometry,
            properties={
                name: _to_json_value(value) for name, value in zip(column_names, row, strict=True)
            },
        )
        for index, (geometry, row) in enumerate(zip(frame.geometry, rows, strict=True))
    ]


def _to_json_value(value: Any) -> Any:
    if value is None or value is pd.NaT:
        return None
    if isinstance(value, float) and not pd.notna(value - value):
        # NaN and the infinities have no JSON representation.
        return None
    if isinstance(value, datetime | date):
        return value.isoformat()
    return value
