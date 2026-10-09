# Geospatial File Measurement API

A FastAPI service for accepting KML files and zipped Shapefiles, extracting their features, and
returning geometry measurements and attributes. File processing runs in a Redis-backed worker;
PostgreSQL/PostGIS stores file metadata, jobs, features, measurements, and a spatially indexed copy
of each feature's geometry.

## Capabilities

- Upload `.kml` files or `.zip` archives containing one Shapefile.
- Calculate polygon area in square metres and line length in metres.
- Reproject coordinates before measurement; latitude/longitude degree values are never used as
  area or distance units.
- Preserve feature geometry and properties, with per-feature statuses for missing, invalid, or
  unsupported geometries.
- Paginate and filter features and measurements; filter features by a WGS 84 bounding box.
- Process files asynchronously and recover jobs left pending or processing after an interrupted
  dispatch or worker process.
- Validate uploads, limit archive expansion, sanitize display filenames, and keep storage paths
  out of API responses.
- Provide structured logs, request IDs, database migrations, Docker Compose, and automated tests.
- Require a shared API key and enforce a Redis-backed request limit when `ENVIRONMENT=production`.

## Architecture

```mermaid
flowchart LR
    Client --> API[FastAPI]
    API --> Intake[Validate and store upload]
    Intake --> DB[(PostgreSQL / PostGIS)]
    Intake --> Redis[(Redis / RQ)]
    Redis --> Worker[Processing worker]
    Worker --> Geo[GeoPandas / Shapely / PyProj]
    Geo --> DB
    Reconciler[Job reconciler] --> DB
    Reconciler --> Redis
    Client --> API
```

See [Architecture](docs/ARCHITECTURE.md) for the processing lifecycle, persistence choices, and
recovery behavior. See [Security](docs/SECURITY.md) before exposing the service beyond a trusted
environment.

## Quick start

### Docker Compose

Docker and Docker Compose are required.

```bash
docker compose up --build
```

The local stack starts PostgreSQL/PostGIS, Redis, the database migration step, the API, the worker,
and the job reconciler. Open <http://localhost:8000/docs> for the interactive API documentation.

To run the end-to-end smoke test from the host, install its HTTP client and run it against the
stack:

```bash
python -m pip install httpx
python scripts/smoke_test.py --base-url http://localhost:8000
```

The fixture `tests/fixtures/survey.kml` is intentionally small and is included in the repository.
The Compose configuration uses local-development credentials and is not a production deployment
configuration.

### Local Python environment

For local development outside Docker, use Python 3.11 or newer, PostgreSQL with PostGIS, and Redis.
On Windows, run the RQ worker under Docker or WSL because RQ uses process forking.

```bash
python -m venv .venv
# Linux/macOS/WSL
source .venv/bin/activate
# Windows PowerShell: .\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
cp .env.example .env
# On Windows PowerShell: Copy-Item .env.example .env
alembic upgrade head
uvicorn app.main:app --reload
```

Run the worker and reconciler in separate terminals:

```bash
python -m app.workers.run
python -m app.workers.reconcile
```

Adjust `.env` for the database and Redis addresses used by the local environment. The migration
role must be allowed to create the PostGIS extension when it is not already installed.

## API reference

The interactive OpenAPI document is at `/docs`. In development, API routes do not require an API
key. In production, requests under `/api/` require `X-API-Key` and are subject to the configured
Redis-backed per-client-IP rate limit.

| Method and path | Purpose |
| --- | --- |
| `POST /api/files/` | Upload a KML file or zipped Shapefile; returns `202 Accepted`. |
| `GET /api/files/{file_id}/` | File metadata and processing status. |
| `GET /api/jobs/{job_id}/` | Job state, timestamps, attempt count, and failure reason. |
| `GET /api/files/{file_id}/measurements/` | Paginated measurements and per-feature measurement status. |
| `GET /api/files/{file_id}/features/` | Paginated geometry and properties. |
| `GET /health` | Process liveness check. |
| `GET /ready` | Database and queue readiness check. |

List endpoints accept `limit` (1–1000, default 100) and `offset` (default 0), and return an exact
`total`. Measurements can be filtered by `status`, `measurement_type`, and `geometry_type`.
Features can be filtered by `geometry_type` and `bbox`, where `bbox` is
`min_lon,min_lat,max_lon,max_lat` in WGS 84 degrees.

### Upload example

```bash
curl -i -F "file=@survey.kml" http://localhost:8000/api/files/
```

A successful upload returns `202 Accepted`, a file ID, a job ID, and status `PENDING`. Poll the job
with `GET /api/jobs/{job_id}/` or the file with `GET /api/files/{file_id}/` until the status is
`COMPLETED` or `FAILED`. The values below are illustrative.

```json
{
  "id": "<file_id>",
  "filename": "survey.kml",
  "file_type": "kml",
  "status": "PENDING",
  "feature_count": 0,
  "crs": null,
  "created_at": "2026-10-09T12:00:00Z",
  "error": null,
  "job_id": "<job_id>"
}
```

### Measurements example

`GET /api/files/{file_id}/measurements/?limit=10`

```json
{
  "file_id": "<file_id>",
  "total": 1,
  "limit": 10,
  "offset": 0,
  "measurements": [
    {
      "feature_index": 0,
      "geometry_type": "Polygon",
      "status": "MEASURED",
      "measurement_type": "area",
      "value": 10000.0,
      "unit": "m²",
      "projected_crs": "EPSG:32644",
      "error": null
    }
  ]
}
```

Polygon area is reported in `m²` and line length in `m`. Point geometries have status
`NO_MEASUREMENT`; invalid or empty geometries have status `FAILED`; unsupported geometry types are
reported as `UNSUPPORTED`. A feature-level issue does not by itself fail the entire file.

## Configuration

Settings are read from environment variables or `.env`. `.env.example` documents the defaults.
The production-only API key is a shared service credential, not a user identity system.

| Variable | Default | Purpose |
| --- | --- | --- |
| `ENVIRONMENT` | `development` | `development`, `test`, or `production`. |
| `API_KEY` | unset | Required in production; send as `X-API-Key`. |
| `DATABASE_URL` | local `geo` database | SQLAlchemy connection URL using `postgresql+psycopg://`. |
| `REDIS_URL` | `redis://localhost:6379/0` | RQ and rate-limit storage. |
| `UPLOAD_DIR` | `data/uploads` | Upload location shared by API, worker, and reconciler. |
| `MAX_UPLOAD_BYTES` | 100 MiB | Maximum accepted file size. |
| `MAX_ZIP_UNCOMPRESSED_BYTES` | 500 MiB | Maximum expanded archive size. |
| `MAX_ZIP_MEMBERS` | 50 | Maximum ZIP entries. |
| `PROCESSING_BATCH_SIZE` | 1000 | Features processed per read/insert batch. |
| `JOB_TIMEOUT_SECONDS` | 1800 | RQ worker timeout. |
| `MAX_PROCESSING_ATTEMPTS` | 3 | Processing attempts before a stale job is marked failed. |
| `RECONCILIATION_INTERVAL_SECONDS` | 15 | Time between reconciliation passes. |
| `QUEUE_RETRY_INTERVAL_SECONDS` | 60 | Minimum delay before re-dispatching a pending job. |
| `JOB_RECOVERY_GRACE_SECONDS` | 60 | Grace beyond the worker timeout before recovering a stale job. |
| `RECONCILIATION_BATCH_SIZE` | 100 | Maximum stale jobs and pending jobs handled per reconciliation pass. |
| `RATE_LIMIT_REQUESTS` | 120 | Requests allowed per client IP per rate-limit window in production. |
| `RATE_LIMIT_WINDOW_SECONDS` | 60 | Rate-limit window. |
| `LOG_LEVEL`, `LOG_FORMAT` | `INFO`, `json` | Log verbosity and output format. |

Production requires an explicit `DATABASE_URL` and a random `API_KEY` with at least 32 characters.
Use a secret manager or the deployment environment for credentials; never commit `.env`.

## Coordinate reference systems

For each feature, the service transforms its centroid to WGS 84, selects the UTM zone for that
position, transforms the geometry into that projected CRS, and then measures area or length. The
selected projected CRS is returned alongside the measurement. Transformer objects are reused for
the lifetime of a dataset measurer to avoid rebuilding them for every feature.

UTM is not suitable for polar regions, extremely large geometries, features spanning multiple
zones, or features crossing the antimeridian. These cases may require a geodesic or equal-area
measurement strategy. See the limitations and trade-offs in [Architecture](docs/ARCHITECTURE.md).

## Development and tests

Install the development dependencies with `python -m pip install -e ".[dev]"`. The API and integration tests use PostgreSQL/PostGIS and Redis; the lower-level unit tests can run
without those services.

```bash
ruff check .
ruff format --check .
mypy app tests scripts
pytest --cov=app --cov-report=term-missing
```

The default test connections are `postgresql+psycopg://geo:geo@localhost:5432/geo_test` and
`redis://localhost:6379/15`. Override them with `TEST_DATABASE_URL` and `TEST_REDIS_URL`. When using
Docker Compose, start `db` and `redis`, then create the `geo_test` database before running the
suite. Tests run against that dedicated database and reset their tables; do not point the test
configuration at a database containing data you need to keep.

GitHub Actions runs linting, formatting, type checks, tests against PostgreSQL/PostGIS and Redis,
and a Docker Compose smoke test on pushes to `main` and pull requests. The test results for a
particular revision are available in that revision's Actions run.

## Performance benchmark

`scripts/benchmark.py` generates polygon Shapefiles and reports processing throughput, peak RSS,
and representative API latencies. It requires a migrated, disposable benchmark database and
truncates the application's feature, job, and upload tables. For safety, the script only runs when
the configured database name ends in `_benchmark`.

```bash
# Example: use a dedicated database named geo_benchmark and migrate it first.
DATABASE_URL=postgresql+psycopg://geo:geo@localhost:5432/geo_benchmark alembic upgrade head
DATABASE_URL=postgresql+psycopg://geo:geo@localhost:5432/geo_benchmark python scripts/benchmark.py
```

Run benchmarks on the hardware and data sizes relevant to the deployment and retain the output
with the environment details when publishing a performance comparison. This repository does not
publish universal throughput or latency guarantees.

## Learning and future scope

This implementation makes several operational boundaries explicit: accepting an upload is distinct
from processing it, successful queue dispatch is distinct from durable job state, and a worker
crash needs recovery behavior rather than only exception handling. Tests cover key data, validation,
and job-state transitions; the reconciler handles dispatch gaps and stale `PROCESSING` records.

Future work could add per-user ownership and authorization, object storage for multi-host workers,
virus scanning in a controlled upload pipeline, geodesic measurements for large or polar geometries,
keyset pagination for deep result pages, and metrics/tracing for operational monitoring.

## Contributing

Keep changes focused, explain non-obvious design decisions, and add tests for behavior and failure
conditions. For schema changes, add an Alembic migration. Before opening a pull request, run the
quality and test commands above and document any checks that require external services.

## License

MIT. See [LICENSE](LICENSE).
