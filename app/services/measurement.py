"""Projected CRS selection and per-feature area/length measurement."""

import math
from dataclasses import dataclass
from functools import lru_cache

import numpy as np
import shapely
from pyproj import CRS, Transformer
from pyproj.exceptions import ProjError
from shapely.errors import ShapelyError
from shapely.geometry.base import BaseGeometry
from shapely.validation import explain_validity

from app.db.models import FeatureStatus, MeasurementType

WGS84 = CRS.from_epsg(4326)

# UTM is only defined between these latitudes; the polar caps use a different projection.
UTM_MIN_LATITUDE = -80.0
UTM_MAX_LATITUDE = 84.0
UTM_ZONE_WIDTH_DEGREES = 6
UTM_ZONE_COUNT = 60
UTM_NORTH_EPSG_BASE = 32600
UTM_SOUTH_EPSG_BASE = 32700

AREA_GEOMETRY_TYPES = {"Polygon", "MultiPolygon"}
LENGTH_GEOMETRY_TYPES = {"LineString", "MultiLineString"}
POINT_GEOMETRY_TYPES = {"Point", "MultiPoint"}

SQUARE_METRES = "m²"
METRES = "m"


class MeasurementError(Exception):
    """A single feature could not be projected or measured."""


@dataclass(frozen=True)
class FeatureResult:
    status: FeatureStatus
    measurement_type: MeasurementType | None = None
    value: float | None = None
    unit: str | None = None
    projected_crs: str | None = None
    error: str | None = None


def select_projected_crs(longitude: float, latitude: float) -> CRS:
    """Return the WGS 84 / UTM zone that contains the given position."""
    if not -180.0 <= longitude <= 180.0:
        raise MeasurementError(f"Longitude {longitude} is outside the range -180 to 180.")
    if not UTM_MIN_LATITUDE <= latitude <= UTM_MAX_LATITUDE:
        raise MeasurementError(
            f"Latitude {latitude} is outside the UTM range "
            f"({UTM_MIN_LATITUDE} to {UTM_MAX_LATITUDE})."
        )

    # Longitude 180 would compute zone 61, so it is folded into the last zone.
    zone = min(int((longitude + 180.0) // UTM_ZONE_WIDTH_DEGREES) + 1, UTM_ZONE_COUNT)
    epsg_base = UTM_NORTH_EPSG_BASE if latitude >= 0 else UTM_SOUTH_EPSG_BASE
    return CRS.from_epsg(epsg_base + zone)


@lru_cache(maxsize=64)
def _transformer(source_crs: CRS, target_crs: CRS) -> Transformer:
    # always_xy keeps coordinates in (x, y) order regardless of the CRS axis definition,
    # which matches how GeoPandas and Shapely store them.
    return Transformer.from_crs(source_crs, target_crs, always_xy=True)


def _reproject(geometry: BaseGeometry, transformer: Transformer) -> BaseGeometry:
    def transform_coordinates(coordinates: np.ndarray) -> np.ndarray:
        x, y = transformer.transform(coordinates[:, 0], coordinates[:, 1])
        return np.column_stack((x, y))

    return shapely.transform(geometry, transform_coordinates)


def _project_to_local_utm(geometry: BaseGeometry, source_crs: CRS) -> tuple[BaseGeometry, CRS]:
    try:
        centroid = geometry.centroid
        longitude, latitude = _transformer(source_crs, WGS84).transform(centroid.x, centroid.y)
        if not (math.isfinite(longitude) and math.isfinite(latitude)):
            raise MeasurementError("Coordinates cannot be transformed to WGS 84.")

        utm_crs = select_projected_crs(longitude, latitude)
        # Heights play no part in planar measurements and would invite vertical-datum shifts.
        flat_geometry = shapely.force_2d(geometry)
        projected = _reproject(flat_geometry, _transformer(source_crs, utm_crs))
    except (ProjError, ShapelyError) as error:
        raise MeasurementError(f"Coordinate transformation failed: {error}") from error

    if not all(math.isfinite(bound) for bound in projected.bounds):
        raise MeasurementError("Coordinates are outside the valid range of the source CRS.")
    return projected, utm_crs


def measure_feature(geometry: BaseGeometry | None, source_crs: CRS) -> FeatureResult:
    if geometry is None or geometry.is_empty:
        return FeatureResult(FeatureStatus.FAILED, error="Feature has no geometry.")

    geometry_type = geometry.geom_type
    if geometry_type in POINT_GEOMETRY_TYPES:
        return FeatureResult(FeatureStatus.NO_MEASUREMENT)
    if geometry_type not in AREA_GEOMETRY_TYPES | LENGTH_GEOMETRY_TYPES:
        return FeatureResult(
            FeatureStatus.UNSUPPORTED,
            error=f"Geometry type {geometry_type} is not supported for measurement.",
        )

    # A self-intersecting polygon has no well-defined area, so it is reported, not measured.
    if not geometry.is_valid:
        return FeatureResult(
            FeatureStatus.FAILED, error=f"Invalid geometry: {explain_validity(geometry)}"
        )

    try:
        projected, utm_crs = _project_to_local_utm(geometry, source_crs)
    except MeasurementError as error:
        return FeatureResult(FeatureStatus.FAILED, error=str(error))

    projected_label = utm_crs.to_string()
    if geometry_type in AREA_GEOMETRY_TYPES:
        return FeatureResult(
            FeatureStatus.MEASURED,
            MeasurementType.AREA,
            projected.area,
            SQUARE_METRES,
            projected_label,
        )
    return FeatureResult(
        FeatureStatus.MEASURED, MeasurementType.LENGTH, projected.length, METRES, projected_label
    )
