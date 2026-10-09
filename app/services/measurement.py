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

WGS84_EPSG = 4326

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


@lru_cache(maxsize=128)
def _crs_from_epsg(code: int) -> CRS:
    return CRS.from_epsg(code)


def select_projected_crs(longitude: float, latitude: float) -> CRS:
    """Return the WGS 84 / UTM zone that contains the given position."""
    return _crs_from_epsg(_utm_epsg(longitude, latitude))


def _utm_epsg(longitude: float, latitude: float) -> int:
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
    return epsg_base + zone


def _has_finite_bounds(geometry: BaseGeometry) -> bool:
    return all(math.isfinite(bound) for bound in geometry.bounds)


class FeatureMeasurer:
    """Measures the features of one dataset.

    Building a pyproj Transformer is expensive, so they are created on first use and kept
    for the lifetime of the measurer. They are keyed by EPSG code because hashing a CRS
    object serialises it to WKT, which dominated the time spent per feature.
    """

    def __init__(self, source_crs: CRS) -> None:
        self._source_crs = source_crs
        self._transformers: dict[int, Transformer] = {}

    def measure(self, geometry: BaseGeometry | None) -> FeatureResult:
        if geometry is None or geometry.is_empty:
            return FeatureResult(FeatureStatus.FAILED, error="Feature has no geometry.")
        if not _has_finite_bounds(geometry):
            return FeatureResult(
                FeatureStatus.FAILED, error="Geometry contains invalid coordinates."
            )

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
            projected, utm_epsg = self._project_to_local_utm(geometry)
        except MeasurementError as error:
            return FeatureResult(FeatureStatus.FAILED, error=str(error))

        projected_label = f"EPSG:{utm_epsg}"
        if geometry_type in AREA_GEOMETRY_TYPES:
            return FeatureResult(
                FeatureStatus.MEASURED,
                MeasurementType.AREA,
                projected.area,
                SQUARE_METRES,
                projected_label,
            )
        return FeatureResult(
            FeatureStatus.MEASURED,
            MeasurementType.LENGTH,
            projected.length,
            METRES,
            projected_label,
        )

    def to_wgs84(self, geometry: BaseGeometry | None) -> BaseGeometry | None:
        """Return a 2D WGS 84 copy for spatial indexing, or None when it cannot be produced."""
        if geometry is None or geometry.is_empty:
            return None
        try:
            converted = self._reproject(shapely.force_2d(geometry), WGS84_EPSG)
        except (ProjError, ShapelyError):
            return None
        return converted if _has_finite_bounds(converted) else None

    def _project_to_local_utm(self, geometry: BaseGeometry) -> tuple[BaseGeometry, int]:
        try:
            centroid = geometry.centroid
            longitude, latitude = self._transformer_to(WGS84_EPSG).transform(centroid.x, centroid.y)
            if not (math.isfinite(longitude) and math.isfinite(latitude)):
                raise MeasurementError("Coordinates cannot be transformed to WGS 84.")

            utm_epsg = _utm_epsg(longitude, latitude)
            # Heights play no part in planar measurements and would invite vertical-datum shifts.
            projected = self._reproject(shapely.force_2d(geometry), utm_epsg)
        except (ProjError, ShapelyError) as error:
            raise MeasurementError(f"Coordinate transformation failed: {error}") from error

        if not _has_finite_bounds(projected):
            raise MeasurementError("Coordinates are outside the valid range of the source CRS.")
        return projected, utm_epsg

    def _reproject(self, geometry: BaseGeometry, target_epsg: int) -> BaseGeometry:
        transformer = self._transformer_to(target_epsg)

        def transform_coordinates(coordinates: np.ndarray) -> np.ndarray:
            if len(coordinates) == 1:
                # pyproj's one-element array path is deprecated in NumPy; use scalars for points.
                x, y = transformer.transform(float(coordinates[0, 0]), float(coordinates[0, 1]))
                return np.array([[x, y]])
            x, y = transformer.transform(coordinates[:, 0], coordinates[:, 1])
            return np.column_stack((x, y))

        return shapely.transform(geometry, transform_coordinates)

    def _transformer_to(self, target_epsg: int) -> Transformer:
        transformer = self._transformers.get(target_epsg)
        if transformer is None:
            # always_xy keeps coordinates in (x, y) order regardless of the CRS axis
            # definition, which matches how GeoPandas and Shapely store them.
            transformer = Transformer.from_crs(
                self._source_crs, _crs_from_epsg(target_epsg), always_xy=True
            )
            self._transformers[target_epsg] = transformer
        return transformer
