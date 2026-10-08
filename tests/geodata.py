"""Builders for small, deterministic geospatial upload fixtures."""

import io
import tempfile
import zipfile
from pathlib import Path

import geopandas as gpd
from shapely.geometry import Polygon, box

FIXTURES = Path(__file__).parent / "fixtures"

# WGS 84 / UTM zone 44N: x=500000 is the zone's central meridian (81°E), so a square
# placed there is measured in the same CRS and keeps its exact metric size.
UTM_44N = "EPSG:32644"
UTM_ORIGIN = (500_000.0, 1_830_000.0)


def utm_square(side_metres: float, offset_metres: float = 0.0) -> Polygon:
    x, y = UTM_ORIGIN
    return box(x + offset_metres, y, x + offset_metres + side_metres, y + side_metres)


def bowtie() -> Polygon:
    """A self-intersecting polygon, drawn in the same place as utm_square."""
    x, y = UTM_ORIGIN
    return Polygon([(x, y), (x + 10, y + 10), (x + 10, y), (x, y + 10)])


def shapefile_zip(frame: gpd.GeoDataFrame, *, omit_suffixes: tuple[str, ...] = ()) -> bytes:
    with tempfile.TemporaryDirectory() as directory:
        frame.to_file(Path(directory) / "layer.shp")
        members = {
            path.name: path.read_bytes()
            for path in Path(directory).iterdir()
            if path.suffix not in omit_suffixes
        }
    return zip_archive(members)


def zip_archive(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    return buffer.getvalue()
