from collections.abc import Callable
from typing import Any

import geopandas as gpd
import pytest
from fastapi.testclient import TestClient
from httpx import Response
from pyproj import CRS, Geod
from shapely.geometry import (
    GeometryCollection,
    LineString,
    MultiLineString,
    MultiPolygon,
    Point,
    shape,
)

from app.db.models import FeatureStatus, MeasurementType
from app.services.measurement import measure_feature
from tests.geodata import FIXTURES, UTM_44N, UTM_ORIGIN, bowtie, shapefile_zip, utm_square

UTM_CRS = CRS.from_user_input(UTM_44N)
GEOD = Geod(ellps="WGS84")
Upload = Callable[[str, bytes], Response]


def test_polygon_area_is_in_square_metres() -> None:
    result = measure_feature(utm_square(100), UTM_CRS)

    assert result.status is FeatureStatus.MEASURED
    assert result.measurement_type is MeasurementType.AREA
    assert result.unit == "m²"
    assert result.value == pytest.approx(10_000.0)


def test_multipolygon_area_is_the_sum_of_its_parts() -> None:
    parts = MultiPolygon([utm_square(100), utm_square(50, offset_metres=500)])

    assert measure_feature(parts, UTM_CRS).value == pytest.approx(12_500.0)


def test_linestring_length_is_in_metres() -> None:
    x, y = UTM_ORIGIN
    road = LineString([(x, y), (x + 300, y + 400)])

    result = measure_feature(road, UTM_CRS)

    assert result.measurement_type is MeasurementType.LENGTH
    assert result.unit == "m"
    assert result.value == pytest.approx(500.0)


def test_multilinestring_length_is_the_sum_of_its_parts() -> None:
    x, y = UTM_ORIGIN
    roads = MultiLineString([[(x, y), (x + 100, y)], [(x, y + 10), (x, y + 60)]])

    assert measure_feature(roads, UTM_CRS).value == pytest.approx(150.0)


def test_point_needs_no_measurement() -> None:
    result = measure_feature(Point(*UTM_ORIGIN), UTM_CRS)

    assert result.status is FeatureStatus.NO_MEASUREMENT
    assert result.value is None


def test_geometry_collection_is_reported_as_unsupported() -> None:
    collection = GeometryCollection([Point(*UTM_ORIGIN), utm_square(10)])

    result = measure_feature(collection, UTM_CRS)

    assert result.status is FeatureStatus.UNSUPPORTED
    assert "GeometryCollection" in (result.error or "")


def test_invalid_polygon_is_reported_instead_of_measured() -> None:
    result = measure_feature(bowtie(), UTM_CRS)

    assert result.status is FeatureStatus.FAILED
    assert "Self-intersection" in (result.error or "")


def test_missing_geometry_is_reported() -> None:
    assert measure_feature(None, UTM_CRS).status is FeatureStatus.FAILED


def test_one_unmeasurable_feature_does_not_stop_the_others(
    client: TestClient, upload: Upload
) -> None:
    frame = gpd.GeoDataFrame(
        {"name": ["bad", "good", "missing"]},
        geometry=[bowtie(), utm_square(100), None],
        crs=UTM_44N,
    )

    created = upload("plots.zip", shapefile_zip(frame))
    assert created.status_code == 201
    assert created.json()["status"] == "COMPLETED"

    measurements = _get_measurements(client, created.json()["id"])
    statuses = {item["properties"]["name"]: item for item in measurements}
    assert statuses["bad"]["status"] == "FAILED"
    assert "Self-intersection" in statuses["bad"]["error"]
    assert statuses["missing"]["status"] == "FAILED"
    assert statuses["good"]["status"] == "MEASURED"
    assert statuses["good"]["value"] == pytest.approx(10_000.0)


def test_kml_features_are_measured_from_degrees(client: TestClient, upload: Upload) -> None:
    created = upload("survey.kml", (FIXTURES / "survey.kml").read_bytes())
    assert created.status_code == 201

    by_name = {
        item["properties"]["Name"]: item for item in _get_measurements(client, created.json()["id"])
    }

    plot = by_name["Plot A"]
    plot_expected = abs(GEOD.geometry_area_perimeter(shape(plot["geometry"]))[0])
    assert plot["geometry_type"] == "Polygon"
    assert plot["crs"] == "EPSG:4326"
    assert plot["projected_crs"] == "EPSG:32644"
    assert plot["measurement_type"] == "area"
    assert plot["unit"] == "m²"
    assert plot["value"] == pytest.approx(plot_expected, rel=1e-3)

    road = by_name["Access road"]
    assert road["measurement_type"] == "length"
    assert road["unit"] == "m"
    assert road["value"] == pytest.approx(GEOD.geometry_length(shape(road["geometry"])), rel=1e-3)

    assert by_name["Gate"]["status"] == "NO_MEASUREMENT"
    assert by_name["Marker with route"]["status"] == "UNSUPPORTED"


def test_shapefile_attributes_and_geometry_are_returned(client: TestClient, upload: Upload) -> None:
    frame = gpd.GeoDataFrame(
        {"name": ["north"], "parcels": [3]}, geometry=[utm_square(10)], crs=UTM_44N
    )

    created = upload("plots.zip", shapefile_zip(frame))
    [feature] = _get_measurements(client, created.json()["id"])

    assert feature["feature_index"] == 0
    assert feature["properties"] == {"name": "north", "parcels": 3}
    assert feature["geometry"]["type"] == "Polygon"
    assert feature["crs"] == "EPSG:32644"


def test_values_are_rounded_only_in_the_response(client: TestClient, upload: Upload) -> None:
    frame = gpd.GeoDataFrame({"name": ["tiny"]}, geometry=[utm_square(1.23456)], crs=UTM_44N)

    created = upload("plots.zip", shapefile_zip(frame))
    [feature] = _get_measurements(client, created.json()["id"])

    assert feature["value"] == round(1.23456**2, 3)


def _get_measurements(client: TestClient, file_id: str) -> list[dict[str, Any]]:
    response = client.get(f"/api/files/{file_id}/measurements/")
    assert response.status_code == 200
    measurements: list[dict[str, Any]] = response.json()["measurements"]
    return measurements
