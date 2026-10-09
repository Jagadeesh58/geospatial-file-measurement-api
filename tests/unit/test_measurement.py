import pytest
from pyproj import CRS
from shapely.geometry import (
    GeometryCollection,
    LineString,
    MultiLineString,
    MultiPolygon,
    Point,
    box,
)

from app.db.models import FeatureStatus, MeasurementType
from app.services.measurement import FeatureMeasurer
from tests.geodata import UTM_44N, UTM_ORIGIN, bowtie, utm_square

UTM_CRS = CRS.from_user_input(UTM_44N)
WGS84 = CRS.from_epsg(4326)


def test_polygon_area_is_in_square_metres() -> None:
    result = FeatureMeasurer(UTM_CRS).measure(utm_square(100))

    assert result.status is FeatureStatus.MEASURED
    assert result.measurement_type is MeasurementType.AREA
    assert result.unit == "m²"
    assert result.value == pytest.approx(10_000.0)


def test_polygon_holes_are_subtracted() -> None:
    x, y = UTM_ORIGIN
    with_hole = box(x, y, x + 100, y + 100).difference(box(x + 25, y + 25, x + 75, y + 75))

    assert FeatureMeasurer(UTM_CRS).measure(with_hole).value == pytest.approx(7_500.0)


def test_multipolygon_area_is_the_sum_of_its_parts() -> None:
    parts = MultiPolygon([utm_square(100), utm_square(50, offset_metres=500)])

    assert FeatureMeasurer(UTM_CRS).measure(parts).value == pytest.approx(12_500.0)


def test_linestring_length_is_in_metres() -> None:
    x, y = UTM_ORIGIN
    road = LineString([(x, y), (x + 300, y + 400)])

    result = FeatureMeasurer(UTM_CRS).measure(road)

    assert result.measurement_type is MeasurementType.LENGTH
    assert result.unit == "m"
    assert result.value == pytest.approx(500.0)


def test_multilinestring_length_is_the_sum_of_its_parts() -> None:
    x, y = UTM_ORIGIN
    roads = MultiLineString([[(x, y), (x + 100, y)], [(x, y + 10), (x, y + 60)]])

    assert FeatureMeasurer(UTM_CRS).measure(roads).value == pytest.approx(150.0)


def test_heights_do_not_change_the_length() -> None:
    x, y = UTM_ORIGIN
    flat = LineString([(x, y), (x + 300, y + 400)])
    raised = LineString([(x, y, 0), (x + 300, y + 400, 500)])

    assert (
        FeatureMeasurer(UTM_CRS).measure(raised).value
        == FeatureMeasurer(UTM_CRS).measure(flat).value
    )


def test_point_needs_no_measurement() -> None:
    result = FeatureMeasurer(UTM_CRS).measure(Point(*UTM_ORIGIN))

    assert result.status is FeatureStatus.NO_MEASUREMENT
    assert result.value is None


def test_geometry_collection_is_reported_as_unsupported() -> None:
    collection = GeometryCollection([Point(*UTM_ORIGIN), utm_square(10)])

    result = FeatureMeasurer(UTM_CRS).measure(collection)

    assert result.status is FeatureStatus.UNSUPPORTED
    assert "GeometryCollection" in (result.error or "")


def test_invalid_polygon_is_reported_instead_of_measured() -> None:
    result = FeatureMeasurer(UTM_CRS).measure(bowtie())

    assert result.status is FeatureStatus.FAILED
    assert "Self-intersection" in (result.error or "")


@pytest.mark.parametrize("geometry", [None, Point(), LineString()])
def test_missing_or_empty_geometry_is_reported(geometry: object) -> None:
    result = FeatureMeasurer(UTM_CRS).measure(geometry)

    assert result.status is FeatureStatus.FAILED
    assert result.error == "Feature has no geometry."


@pytest.mark.filterwarnings("ignore:invalid value encountered:RuntimeWarning")
@pytest.mark.parametrize("bad_value", [float("inf"), float("nan")])
def test_non_finite_coordinates_are_reported(bad_value: float) -> None:
    result = FeatureMeasurer(WGS84).measure(LineString([(0.0, 0.0), (bad_value, 1.0)]))

    assert result.status is FeatureStatus.FAILED
    assert result.error


def test_to_wgs84_converts_projected_geometries() -> None:
    converted = FeatureMeasurer(UTM_CRS).to_wgs84(utm_square(100))

    assert converted is not None
    min_x, min_y, max_x, max_y = converted.bounds
    assert 80.99 < min_x < max_x < 81.01
    assert 16.0 < min_y < max_y < 17.0


def test_to_wgs84_returns_nothing_for_missing_geometry() -> None:
    assert FeatureMeasurer(UTM_CRS).to_wgs84(None) is None
    assert FeatureMeasurer(UTM_CRS).to_wgs84(Point()) is None


def test_points_are_reprojected_to_the_right_location() -> None:
    converted = FeatureMeasurer(UTM_CRS).to_wgs84(Point(500_000, 1_830_000))

    assert converted is not None
    assert converted.x == pytest.approx(81.0, abs=1e-6)
    assert converted.y == pytest.approx(16.5524, abs=1e-3)
