import math

import geopandas as gpd
import pytest
from pyproj import CRS, Geod
from shapely.geometry import LineString, Polygon, box

from app.db.models import FeatureStatus, MeasurementType
from app.services.measurement import FeatureMeasurer, MeasurementError, select_projected_crs

GEOD = Geod(ellps="WGS84")
WGS84 = CRS.from_epsg(4326)


@pytest.mark.parametrize(
    ("longitude", "latitude", "expected_epsg"),
    [
        (80.5, 16.5, 32644),
        (-0.1, 51.5, 32630),
        (151.2, -33.9, 32756),
        (-179.9, 10.0, 32601),
        (180.0, 10.0, 32660),
    ],
)
def test_select_projected_crs_picks_the_utm_zone_of_the_position(
    longitude: float, latitude: float, expected_epsg: int
) -> None:
    assert select_projected_crs(longitude, latitude).to_epsg() == expected_epsg


@pytest.mark.parametrize(("longitude", "latitude"), [(10.0, 85.0), (10.0, -81.0), (181.0, 0.0)])
def test_select_projected_crs_rejects_positions_outside_utm_coverage(
    longitude: float, latitude: float
) -> None:
    with pytest.raises(MeasurementError):
        select_projected_crs(longitude, latitude)


def test_polygon_in_degrees_is_reprojected_before_measuring() -> None:
    plot = box(80.5, 16.5, 80.501, 16.501)

    result = FeatureMeasurer(WGS84).measure(plot)

    expected_area = abs(GEOD.geometry_area_perimeter(plot)[0])
    assert result.status is FeatureStatus.MEASURED
    assert result.measurement_type is MeasurementType.AREA
    assert result.unit == "m²"
    assert result.projected_crs == "EPSG:32644"
    assert result.value == pytest.approx(expected_area, rel=1e-3)
    # The raw area in degrees is about 1e-6, which is what a naive calculation would report.
    assert plot.area < 1e-5


def test_line_in_degrees_is_measured_in_metres() -> None:
    road = LineString([(80.5, 16.5), (80.51, 16.5)])

    result = FeatureMeasurer(WGS84).measure(road)

    assert result.measurement_type is MeasurementType.LENGTH
    assert result.unit == "m"
    assert result.value == pytest.approx(GEOD.geometry_length(road), rel=1e-3)


def test_southern_hemisphere_uses_a_southern_utm_zone() -> None:
    plot = box(151.2, -33.9, 151.201, -33.899)

    result = FeatureMeasurer(WGS84).measure(plot)

    assert result.projected_crs == "EPSG:32756"
    assert result.value == pytest.approx(abs(GEOD.geometry_area_perimeter(plot)[0]), rel=1e-3)


def test_projected_source_crs_is_not_trusted_for_measurement() -> None:
    # At 16.5°N the raw Web Mercator area of this square is about 9% larger than its
    # true ground area, so measuring in the source CRS would be visibly wrong.
    square_wgs84 = box(80.5, 16.5, 80.5009, 16.5009)
    square_web_mercator: Polygon = gpd.GeoSeries([square_wgs84], crs=WGS84).to_crs(3857).iloc[0]

    result = FeatureMeasurer(CRS.from_epsg(3857)).measure(square_web_mercator)

    expected_area = abs(GEOD.geometry_area_perimeter(square_wgs84)[0])
    assert result.value == pytest.approx(expected_area, rel=1e-3)
    assert not math.isclose(result.value or 0.0, square_web_mercator.area, rel_tol=0.01)


def test_coordinates_outside_the_valid_range_fail_the_feature() -> None:
    result = FeatureMeasurer(WGS84).measure(box(80.5, 95.0, 80.6, 95.1))

    assert result.status is FeatureStatus.FAILED
    assert result.error
