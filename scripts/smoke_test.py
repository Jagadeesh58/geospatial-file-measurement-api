"""End-to-end check of a running stack: upload a file, wait for its job, read the results.

python scripts/smoke_test.py --base-url http://localhost:8000
"""

import argparse
import sys
import time
from pathlib import Path

import httpx

DEFAULT_FILE = Path(__file__).parents[1] / "tests" / "fixtures" / "survey.kml"
POLL_INTERVAL_SECONDS = 0.5
# "Plot A" in the sample file is roughly 107 m x 111 m, so its area is near 11,800 m².
EXPECTED_PLOT_AREA_RANGE = (11_000.0, 12_500.0)


def fail(message: str) -> None:
    print(f"smoke test failed: {message}", file=sys.stderr)
    raise SystemExit(1)


def wait_for_job(client: httpx.Client, job_id: str, timeout_seconds: float) -> dict[str, object]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}/").raise_for_status().json()
        if job["status"] in {"COMPLETED", "FAILED"}:
            return dict(job)
        time.sleep(POLL_INTERVAL_SECONDS)
    fail(f"job {job_id} did not finish within {timeout_seconds} seconds")
    return {}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--file", type=Path, default=DEFAULT_FILE)
    parser.add_argument("--timeout", type=float, default=60.0)
    arguments = parser.parse_args()

    with httpx.Client(base_url=arguments.base_url, timeout=30.0) as client:
        ready = client.get("/ready")
        if ready.status_code != 200:
            fail(f"/ready returned {ready.status_code}: {ready.text}")

        with arguments.file.open("rb") as stream:
            response = client.post("/api/files/", files={"file": (arguments.file.name, stream)})
        if response.status_code != 202:
            fail(f"upload returned {response.status_code}: {response.text}")
        created = response.json()
        print(f"uploaded {created['filename']} as file {created['id']} (job {created['job_id']})")

        job = wait_for_job(client, created["job_id"], arguments.timeout)
        if job["status"] != "COMPLETED":
            fail(f"job ended as {job['status']}: {job['error']}")
        print(f"job completed in {job['duration_seconds']} s")

        info = client.get(f"/api/files/{created['id']}/").raise_for_status().json()
        if info["feature_count"] != 4 or info["crs"] != "EPSG:4326":
            fail(f"unexpected file info: {info}")

        measurements = (
            client.get(f"/api/files/{created['id']}/measurements/?limit=2")
            .raise_for_status()
            .json()
        )
        if measurements["total"] != 4 or len(measurements["measurements"]) != 2:
            fail(f"unexpected measurements page: {measurements}")

        areas = (
            client.get(f"/api/files/{created['id']}/measurements/?measurement_type=area")
            .raise_for_status()
            .json()["measurements"]
        )
        low, high = EXPECTED_PLOT_AREA_RANGE
        if len(areas) != 1 or not low < areas[0]["value"] < high:
            fail(f"unexpected polygon area: {areas}")

        features = (
            client.get(f"/api/files/{created['id']}/features/?bbox=80.4,16.4,80.6,16.6")
            .raise_for_status()
            .json()
        )
        if features["total"] != 4:
            fail(f"unexpected features page: {features['total']} features in bbox")

    print("smoke test passed")


if __name__ == "__main__":
    main()
