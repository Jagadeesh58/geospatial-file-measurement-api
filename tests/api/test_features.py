from collections.abc import Callable

import geopandas as gpd
import pytest
from fastapi.testclient import TestClient
from httpx import Response

from tests.geodata import FIXTURES, UTM_44N, shapefile_zip, squares_frame, utm_square

Upload = Callable[[str, bytes], Response]


def test_features_include_geometry_crs_and_properties(client: TestClient, upload: Upload) -> None:
    frame = gpd.GeoDataFrame(
        {"name": ["north"], "parcels": [3]}, geometry=[utm_square(10)], crs=UTM_44N
    )
    file_id = upload("plots.zip", shapefile_zip(frame)).json()["id"]

    body = client.get(f"/api/files/{file_id}/features/").json()

    assert body["total"] == 1
    [feature] = body["features"]
    assert feature["feature_index"] == 0
    assert feature["geometry_type"] == "Polygon"
    assert feature["geometry"]["type"] == "Polygon"
    assert feature["crs"] == "EPSG:32644"
    assert feature["properties"] == {"name": "north", "parcels": 3}


def test_features_are_paginated(client: TestClient, upload: Upload) -> None:
    file_id = upload("plots.zip", shapefile_zip(squares_frame(5))).json()["id"]

    page = client.get(f"/api/files/{file_id}/features/?limit=2&offset=2").json()

    assert page["total"] == 5
    assert [item["properties"]["name"] for item in page["features"]] == ["sq-2", "sq-3"]


def test_bbox_filter_uses_wgs84_coordinates(client: TestClient, upload: Upload) -> None:
    file_id = upload("survey.kml", (FIXTURES / "survey.kml").read_bytes()).json()["id"]

    def names(bbox: str) -> list[str]:
        response = client.get(f"/api/files/{file_id}/features/?bbox={bbox}")
        assert response.status_code == 200
        return [item["properties"]["Name"] for item in response.json()["features"]]

    # The plot covers 80.5-80.501 E, the road runs east to 80.51 E at the same latitude.
    assert set(names("80.4995,16.4995,80.5005,16.5005")) == {
        "Plot A",
        "Access road",
        "Gate",
        "Marker with route",
    }
    # East of the plot only the two features with a line running to 80.51 E remain.
    assert set(names("80.505,16.4995,80.52,16.5005")) == {"Access road", "Marker with route"}
    assert names("10,10,11,11") == []


@pytest.mark.parametrize(
    "bbox",
    ["1,2,3", "a,b,c,d", "10,10,5,20", "0,0,200,10", "0,-100,10,10"],
)
def test_invalid_bbox_is_rejected(client: TestClient, upload: Upload, bbox: str) -> None:
    file_id = upload("plots.zip", shapefile_zip(squares_frame(1))).json()["id"]

    assert client.get(f"/api/files/{file_id}/features/?bbox={bbox}").status_code == 422


def test_bbox_works_for_projected_sources(client: TestClient, upload: Upload) -> None:
    # The UTM 44N fixtures sit on the 81°E meridian at about 16.5°N.
    file_id = upload("plots.zip", shapefile_zip(squares_frame(1))).json()["id"]

    hit = client.get(f"/api/files/{file_id}/features/?bbox=80.99,16.4,81.01,16.6").json()
    miss = client.get(f"/api/files/{file_id}/features/?bbox=70,10,71,11").json()

    assert hit["total"] == 1
    assert miss["total"] == 0
