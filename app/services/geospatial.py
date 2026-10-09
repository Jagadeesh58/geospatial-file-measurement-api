"""Validates uploaded KML files and zipped Shapefiles and reads them in batches."""

import logging
import tempfile
import zipfile
import zlib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

import geopandas as gpd
import pandas as pd
from pyogrio.errors import DataLayerError, DataSourceError
from pyproj import CRS
from shapely.geometry.base import BaseGeometry

from app.core.config import Settings
from app.db.models import FileType

logger = logging.getLogger(__name__)

KML_HEADER_BYTES = 64 * 1024
COPY_CHUNK_BYTES = 64 * 1024
REQUIRED_SHAPEFILE_SUFFIXES = (".shp", ".shx", ".dbf", ".prj")
SHAPEFILE_SUFFIXES = {*REQUIRED_SHAPEFILE_SUFFIXES, ".cpg"}
FILE_TYPES_BY_EXTENSION = {".kml": FileType.KML, ".zip": FileType.SHAPEFILE}
STORAGE_EXTENSIONS = {FileType.KML: ".kml", FileType.SHAPEFILE: ".zip"}
ZIP_ENCRYPTED_FLAG = 0x1


class InvalidGeospatialFile(Exception):
    """The upload is not a usable KML file or Shapefile archive."""


@dataclass(frozen=True)
class SourceFeature:
    index: int
    geometry: BaseGeometry | None
    properties: dict[str, Any]


@dataclass(frozen=True)
class FeatureBatch:
    crs: CRS
    features: list[SourceFeature]


def detect_file_type(filename: str) -> FileType:
    extension = Path(filename).suffix.lower()
    try:
        return FILE_TYPES_BY_EXTENSION[extension]
    except KeyError:
        raise InvalidGeospatialFile(
            f"Unsupported file type '{extension}'. Upload a .kml file or a .zip Shapefile."
        ) from None


def describe_crs(crs: CRS) -> str:
    authority = crs.to_authority()
    return f"{authority[0]}:{authority[1]}" if authority else crs.name


def validate_upload(file_type: FileType, path: Path, settings: Settings) -> None:
    """Cheap structural checks run before a job is queued; nothing is parsed or extracted."""
    if file_type is FileType.KML:
        _check_kml_header(path)
        return
    with _open_archive(path) as archive:
        _select_shapefile_parts(archive, settings)


@dataclass
class Dataset:
    """A readable dataset made of one or more layers."""

    source: Path
    layers: list[str]

    def batches(self, batch_size: int) -> Iterator[FeatureBatch]:
        """Yield features in batches so memory use does not grow with the file size."""
        next_index = 0
        for layer in self.layers:
            start = 0
            while True:
                frame = self._read(layer, start, batch_size)
                if frame.empty:
                    break
                if frame.crs is None:
                    raise InvalidGeospatialFile(
                        "The file does not declare a coordinate reference system."
                    )
                yield FeatureBatch(frame.crs, _extract_features(frame, next_index))
                next_index += len(frame)
                start += len(frame)
                if len(frame) < batch_size:
                    break

    def _read(self, layer: str, start: int, count: int) -> gpd.GeoDataFrame:
        try:
            return gpd.read_file(self.source, layer=layer, rows=slice(start, start + count))
        except (DataSourceError, DataLayerError) as error:
            raise _unreadable("The file could not be read.", error) from error


@contextmanager
def open_dataset(file_type: FileType, path: Path, settings: Settings) -> Iterator[Dataset]:
    with tempfile.TemporaryDirectory(prefix="geofile-") as work_directory:
        if file_type is FileType.KML:
            source = path
        else:
            source = _extract_shapefile(path, Path(work_directory), settings)
        try:
            # GDAL exposes every KML folder as a separate layer, so all of them are read.
            layers = list(gpd.list_layers(source)["name"])
        except (DataSourceError, DataLayerError) as error:
            raise _unreadable("The file could not be read.", error) from error
        yield Dataset(source, layers)


def _unreadable(message: str, error: Exception) -> InvalidGeospatialFile:
    # GDAL messages can contain server-side paths, so they are logged but not returned.
    logger.warning("dataset_unreadable", extra={"detail": str(error)})
    return InvalidGeospatialFile(message)


def _check_kml_header(path: Path) -> None:
    with path.open("rb") as stream:
        header = stream.read(KML_HEADER_BYTES)
    root_position = header.find(b"<kml")
    if root_position < 0:
        raise InvalidGeospatialFile("The file is not a KML document.")
    # KML never needs a DTD; refusing one rules out entity-expansion attacks.
    if b"<!DOCTYPE" in header[:root_position]:
        raise InvalidGeospatialFile("KML documents with a DOCTYPE declaration are not accepted.")


@contextmanager
def _open_archive(path: Path) -> Iterator[zipfile.ZipFile]:
    try:
        archive = zipfile.ZipFile(path)
    except zipfile.BadZipFile as error:
        raise InvalidGeospatialFile("The upload is not a valid ZIP archive.") from error
    with archive:
        yield archive


def _extract_shapefile(path: Path, directory: Path, settings: Settings) -> Path:
    with _open_archive(path) as archive:
        parts = _select_shapefile_parts(archive, settings)
        remaining_bytes = settings.max_zip_uncompressed_bytes
        try:
            for suffix, member in parts.items():
                # Member names never reach the filesystem; every part is written under a
                # fixed name inside the temporary directory.
                target_path = directory / f"dataset{suffix}"
                with archive.open(member) as source, target_path.open("wb") as target:
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
        normalized_name = member.filename.replace("\\", "/")
        path = PurePosixPath(normalized_name)
        windows_path = PureWindowsPath(member.filename)
        if (
            path.is_absolute()
            or windows_path.is_absolute()
            or windows_path.drive
            or ".." in path.parts
        ):
            raise InvalidGeospatialFile("The ZIP archive contains unsafe file paths.")
        if member.is_dir() or _is_archive_metadata(path):
            continue
        suffix = path.suffix.lower()
        if suffix not in SHAPEFILE_SUFFIXES:
            continue
        if member.flag_bits & ZIP_ENCRYPTED_FLAG:
            raise InvalidGeospatialFile("Encrypted ZIP archives are not supported.")
        stem = str(path.with_suffix("")).lower()
        parts = datasets.setdefault(stem, {})
        if suffix in parts:
            raise InvalidGeospatialFile("The ZIP archive contains duplicate Shapefile components.")
        parts[suffix] = member

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


def _extract_features(frame: gpd.GeoDataFrame, first_index: int) -> list[SourceFeature]:
    attributes = frame.drop(columns=frame.geometry.name)
    column_names = [str(name) for name in attributes.columns]
    rows = attributes.itertuples(index=False, name=None)
    return [
        SourceFeature(
            index=first_index + offset,
            geometry=geometry,
            properties={
                name: _to_json_value(value) for name, value in zip(column_names, row, strict=True)
            },
        )
        for offset, (geometry, row) in enumerate(zip(frame.geometry, rows, strict=True))
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
