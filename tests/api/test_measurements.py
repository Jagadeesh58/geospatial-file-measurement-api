from collections.abc import Callable
from typing import Any

import geopandas as gpd
import pytest
from fastapi.testclient import TestClient
from httpx import Response
from pyproj import Geod
from shapely.geometry import shape

from tests.geodata import FIXTURES, UTM_44N, shapefile_zip, squares_frame, utm_square

Upload = Callable[[str, bytes], Response]
GEOD = Geod(ellps="WGS84")


def _upload_squares(upload: Upload, count: int) -> str:
    response = upload("plots.zip", shapefile_zip(squares_frame(count)))
    assert response.status_code == 202
    return str(response.json()["id"])


def _upload_survey(upload: Upload) -> str:
    return str(upload("survey.kml", (FIXTURES / "survey.kml").read_bytes()).json()["id"])


def test_kml_measurements_are_taken_in_metres_not_degrees(
    client: TestClient, upload: Upload
) -> None:
    file_id = _upload_survey(upload)

    measurements = client.get(f"/api/files/{file_id}/measurements/").json()["measurements"]
    features = client.get(f"/api/files/{file_id}/features/").json()["features"]

    by_name = {
        feature["properties"]["Name"]: (feature, measurement)
        for feature, measurement in zip(features, measurements, strict=True)
    }
    plot, plot_measurement = by_name["Plot A"]
    expected_area = abs(GEOD.geometry_area_perimeter(shape(plot["geometry"]))[0])
    assert plot_measurement["measurement_type"] == "area"
    assert plot_measurement["unit"] == "m²"
    assert plot_measurement["projected_crs"] == "EPSG:32644"
    assert plot_measurement["value"] == pytest.approx(expected_area, rel=1e-3)

    road, road_measurement = by_name["Access road"]
    assert road_measurement["unit"] == "m"
    assert road_measurement["value"] == pytest.approx(
        GEOD.geometry_length(shape(road["geometry"])), rel=1e-3
    )
    assert by_name["Gate"][1]["status"] == "NO_MEASUREMENT"
    assert by_name["Marker with route"][1]["status"] == "UNSUPPORTED"


def test_measurements_are_paginated_in_feature_order(client: TestClient, upload: Upload) -> None:
    file_id = _upload_squares(upload, 7)

    first = client.get(f"/api/files/{file_id}/measurements/?limit=3").json()
    second = client.get(f"/api/files/{file_id}/measurements/?limit=3&offset=3").json()
    last = client.get(f"/api/files/{file_id}/measurements/?limit=3&offset=6").json()

    assert first["total"] == second["total"] == last["total"] == 7
    assert first["limit"] == 3
    assert second["offset"] == 3
    indexes = [
        item["feature_index"] for page in (first, second, last) for item in page["measurements"]
    ]
    assert indexes == list(range(7))


def test_default_page_size_returns_every_small_result(client: TestClient, upload: Upload) -> None:
    file_id = _upload_squares(upload, 7)

    body = client.get(f"/api/files/{file_id}/measurements/").json()

    assert body["limit"] == 100
    assert len(body["measurements"]) == 7


@pytest.mark.parametrize("query", ["limit=0", "limit=1001", "offset=-1", "limit=abc"])
def test_invalid_pagination_is_rejected(client: TestClient, upload: Upload, query: str) -> None:
    file_id = _upload_squares(upload, 2)

    response = client.get(f"/api/files/{file_id}/measurements/?{query}")

    assert response.status_code == 422


def test_measurements_can_be_filtered(client: TestClient, upload: Upload) -> None:
    file_id = _upload_survey(upload)

    def fetch(query: str) -> list[dict[str, Any]]:
        body = client.get(f"/api/files/{file_id}/measurements/?{query}").json()
        items: list[dict[str, Any]] = body["measurements"]
        return items

    assert {item["status"] for item in fetch("status=MEASURED")} == {"MEASURED"}
    assert len(fetch("status=MEASURED")) == 2
    assert [item["measurement_type"] for item in fetch("measurement_type=length")] == ["length"]
    assert [item["geometry_type"] for item in fetch("geometry_type=Polygon")] == ["Polygon"]
    assert fetch("status=FAILED") == []


def test_unknown_filter_value_is_rejected(client: TestClient, upload: Upload) -> None:
    file_id = _upload_survey(upload)

    assert client.get(f"/api/files/{file_id}/measurements/?status=NOPE").status_code == 422


def test_measurement_items_do_not_include_geometry_or_properties(
    client: TestClient, upload: Upload
) -> None:
    file_id = _upload_survey(upload)

    [item, *_] = client.get(f"/api/files/{file_id}/measurements/").json()["measurements"]

    assert "geometry" not in item
    assert "properties" not in item


def test_values_are_rounded_only_in_the_response(client: TestClient, upload: Upload) -> None:
    frame = gpd.GeoDataFrame({"name": ["tiny"]}, geometry=[utm_square(1.23456)], crs=UTM_44N)
    file_id = upload("plots.zip", shapefile_zip(frame)).json()["id"]

    [item] = client.get(f"/api/files/{file_id}/measurements/").json()["measurements"]

    assert item["value"] == round(1.23456**2, 3)
