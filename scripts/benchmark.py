"""Measures processing time, peak memory and API latency for small, medium and large uploads.

Needs a migrated PostgreSQL/PostGIS database (DATABASE_URL). Each size runs in its own
process so that the peak memory figure belongs to that size alone.

    python scripts/benchmark.py                    # 100, 10,000 and 100,000 polygons
    python scripts/benchmark.py --sizes 500 5000
"""

import argparse
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from typing import BinaryIO

import geopandas as gpd
from shapely.geometry import box

try:
    import resource
except ImportError:  # Windows has no resource module
    resource = None  # type: ignore[assignment]

UTM_44N = "EPSG:32644"
GRID_COLUMNS = 1000
CELL_METRES = 20.0
SQUARE_METRES = 10.0
LATENCY_SAMPLES = 20
DEFAULT_SIZES = (100, 10_000, 100_000)


def build_shapefile_zip(count: int, directory: Path) -> Path:
    squares = [
        box(
            500_000 + (index % GRID_COLUMNS) * CELL_METRES,
            1_830_000 + (index // GRID_COLUMNS) * CELL_METRES,
            500_000 + (index % GRID_COLUMNS) * CELL_METRES + SQUARE_METRES,
            1_830_000 + (index // GRID_COLUMNS) * CELL_METRES + SQUARE_METRES,
        )
        for index in range(count)
    ]
    frame = gpd.GeoDataFrame(
        {"name": [f"plot-{index}" for index in range(count)]}, geometry=squares, crs=UTM_44N
    )
    shapefile_directory = directory / f"shape-{count}"
    shapefile_directory.mkdir()
    frame.to_file(shapefile_directory / "plots.shp")
    archive_path = directory / f"plots-{count}.zip"
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for part in shapefile_directory.iterdir():
            archive.write(part, part.name)
    return archive_path


def peak_memory_mib() -> float | None:
    if resource is None:
        return None
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak / (1024 * 1024) if sys.platform == "darwin" else peak / 1024


class NoQueue:
    """Jobs are run directly by the benchmark, so nothing is queued."""

    def enqueue(self, job_id: str) -> None:
        return None

    def ping(self) -> bool:
        return True


def median_latency_ms(call) -> float:
    call()  # warm-up
    samples = []
    for _ in range(LATENCY_SAMPLES):
        started = time.perf_counter()
        response = call()
        samples.append((time.perf_counter() - started) * 1000)
        assert response.status_code == 200, response.text
    return statistics.median(samples)


def run_child(archive_path: Path) -> dict[str, float | int | None]:
    """Process one archive and report timings. Runs in a fresh process."""
    from fastapi.testclient import TestClient
    from sqlalchemy import text
    from sqlalchemy.orm import Session

    from app.core.config import get_settings
    from app.db.database import get_engine
    from app.main import app
    from app.services.files import accept_upload
    from app.services.jobs import run_processing_job

    settings = get_settings()
    baseline = peak_memory_mib()
    stream: BinaryIO
    with (
        Session(get_engine(), expire_on_commit=False) as session,
        archive_path.open("rb") as stream,
    ):
        started = time.perf_counter()
        record = accept_upload(session, NoQueue(), stream, "plots.zip", settings)
        upload_seconds = time.perf_counter() - started
        assert record.job_id is not None

        started = time.perf_counter()
        run_processing_job(session, settings, record.job_id)
        processing_seconds = time.perf_counter() - started
        file_id = record.id
        session.refresh(record)
        feature_count = record.feature_count
        assert record.status.value == "COMPLETED", record.error

    peak = peak_memory_mib()
    # Autovacuum would refresh the planner statistics within a minute of a bulk load; doing it
    # here keeps the latency figures from depending on that timing.
    with get_engine().connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        connection.execute(text("ANALYZE features"))
    client = TestClient(app)
    base = f"/api/files/{file_id}"
    latencies = {
        "file_info_ms": median_latency_ms(lambda: client.get(f"{base}/")),
        "measurements_page_ms": median_latency_ms(
            lambda: client.get(f"{base}/measurements/?limit=100")
        ),
        "measurements_deep_page_ms": median_latency_ms(
            lambda: client.get(
                f"{base}/measurements/?limit=100&offset={max(feature_count - 100, 0)}"
            )
        ),
        "features_page_ms": median_latency_ms(lambda: client.get(f"{base}/features/?limit=100")),
        "features_bbox_ms": median_latency_ms(
            lambda: client.get(f"{base}/features/?limit=100&bbox=80.99,16.4,81.01,16.6")
        ),
    }
    return {
        "features": feature_count,
        "upload_seconds": upload_seconds,
        "processing_seconds": processing_seconds,
        "baseline_rss_mib": baseline,
        "peak_rss_mib": peak,
        **latencies,
    }


def run_parent(sizes: list[int]) -> None:
    from sqlalchemy import text

    from app.db.database import get_engine

    results = []
    with tempfile.TemporaryDirectory(prefix="benchmark-") as work_directory:
        directory = Path(work_directory)
        for size in sizes:
            archive_path = build_shapefile_zip(size, directory)
            with get_engine().begin() as connection:
                connection.execute(
                    text(
                        "TRUNCATE features, processing_jobs, uploaded_files "
                        "RESTART IDENTITY CASCADE"
                    )
                )
            environment = {
                **os.environ,
                "UPLOAD_DIR": str(directory / "uploads"),
                "LOG_LEVEL": "WARNING",
            }
            completed = subprocess.run(
                [sys.executable, __file__, "--child", str(archive_path)],
                capture_output=True,
                text=True,
                env=environment,
                check=True,
            )
            result = json.loads(completed.stdout.strip().splitlines()[-1])
            result["archive_mib"] = archive_path.stat().st_size / (1024 * 1024)
            results.append(result)
            print(f"finished {size} features", file=sys.stderr)
    print_table(results)


def print_table(results: list[dict[str, float]]) -> None:
    print("| Features | Archive (MiB) | Processing (s) | Features/s | Peak RSS (MiB) |")
    print("| ---: | ---: | ---: | ---: | ---: |")
    for row in results:
        rate = row["features"] / row["processing_seconds"]
        print(
            f"| {row['features']:,} | {row['archive_mib']:.1f} | {row['processing_seconds']:.2f} "
            f"| {rate:,.0f} | {row['peak_rss_mib']:.0f} |"
        )
    print()
    print("Median API latency over 20 requests (ms), in-process test client:")
    print()
    print(
        "| Features | File info | Measurements p1 | Measurements last page "
        "| Features p1 | Features bbox |"
    )
    print("| ---: | ---: | ---: | ---: | ---: | ---: |")
    for row in results:
        print(
            f"| {row['features']:,} | {row['file_info_ms']:.1f} "
            f"| {row['measurements_page_ms']:.1f} | {row['measurements_deep_page_ms']:.1f} "
            f"| {row['features_page_ms']:.1f} "
            f"| {row['features_bbox_ms']:.1f} |"
        )


def ensure_disposable_benchmark_database() -> None:
    """Avoid truncating application data when the benchmark is pointed at the wrong database."""
    from sqlalchemy.engine import make_url

    from app.core.config import get_settings

    database_name = make_url(get_settings().database_url).database
    if not database_name or not database_name.endswith("_benchmark"):
        raise SystemExit(
            "The benchmark truncates application tables. Use a dedicated database whose name "
            "ends in '_benchmark'."
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--sizes", type=int, nargs="+", default=list(DEFAULT_SIZES))
    parser.add_argument("--child", type=Path, help=argparse.SUPPRESS)
    arguments = parser.parse_args()
    if arguments.child:
        print(json.dumps(run_child(arguments.child)))
    else:
        ensure_disposable_benchmark_database()
        run_parent(arguments.sizes)


if __name__ == "__main__":
    main()
