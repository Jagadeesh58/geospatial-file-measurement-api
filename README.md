# Geospatial File Measurement API

A small FastAPI service that accepts a KML file or a zipped Shapefile, extracts its features and
returns the area of every polygon and the length of every line in square metres and metres.

Measurements are never taken from raw latitude/longitude values. Each feature is first projected
into the UTM zone that contains it (see [CRS strategy](#crs-strategy)).

## Requirements

- Python 3.11 or newer
- No system GDAL or PROJ install: GeoPandas, Shapely, PyProj and pyogrio are installed from PyPI
  wheels. Developed and tested on Linux.

## Installation

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

## Configuration

All settings are optional environment variables. `.env.example` lists them with their defaults.
The application does not read `.env` itself; export the variables in your shell or pass them to
your process manager.

| Variable | Default | Meaning |
| --- | --- | --- |
| `DATABASE_URL` | `sqlite:///data/app.db` | SQLAlchemy URL. Tables are created on startup. |
| `MAX_UPLOAD_BYTES` | `20971520` (20 MiB) | Largest accepted upload. |
| `MAX_ZIP_UNCOMPRESSED_BYTES` | `104857600` (100 MiB) | Largest size a ZIP may expand to. |
| `MAX_ZIP_MEMBERS` | `50` | Largest number of entries in a ZIP. |

## Running locally

```bash
uvicorn app.main:app --reload
```

The interactive OpenAPI documentation is served at <http://127.0.0.1:8000/docs>.

With Docker:

```bash
docker compose up --build
```

## Running tests and checks

```bash
pytest
ruff check .
ruff format --check .
mypy app tests
```

The tests build their Shapefile fixtures in memory and use one small hand-written KML file
(`tests/fixtures/survey.kml`). They need no network access.

## API

### `POST /api/files/`

Multipart upload with a single `file` field. Accepts a `.kml` file or a `.zip` that contains one
Shapefile (`.shp`, `.shx`, `.dbf` and `.prj` are required, `.cpg` is used when present).
The file is processed before the response is sent.

```bash
curl -F "file=@plots.zip" http://127.0.0.1:8000/api/files/
```

```json
{
  "id": "ff33e3ec5cc1449aadd76fb403e232db",
  "filename": "plots.zip",
  "file_type": "shapefile",
  "status": "COMPLETED",
  "feature_count": 2,
  "crs": "EPSG:32644",
  "created_at": "2026-10-08T05:42:20.773734Z",
  "error": null
}
```

Returns `201` on success.

### `GET /api/files/{id}/`

Returns the same object as the upload response.

### `GET /api/files/{id}/measurements/`

Returns one entry per feature, ordered by `feature_index`. Geometry is GeoJSON in the file's own
CRS (named in `crs`), so it is standard GeoJSON for KML and for Shapefiles stored in WGS 84, and
projected coordinates for Shapefiles stored in a projected CRS.

```json
{
  "file_id": "ff33e3ec5cc1449aadd76fb403e232db",
  "measurements": [
    {
      "feature_index": 0,
      "geometry_type": "Polygon",
      "geometry": {
        "type": "Polygon",
        "coordinates": [
          [[500100.0, 1830000.0], [500000.0, 1830000.0], [500000.0, 1830100.0],
           [500100.0, 1830100.0], [500100.0, 1830000.0]]
        ]
      },
      "crs": "EPSG:32644",
      "properties": {"name": "plot-1"},
      "status": "MEASURED",
      "measurement_type": "area",
      "value": 10000.0,
      "unit": "m²",
      "projected_crs": "EPSG:32644",
      "error": null
    },
    {
      "feature_index": 1,
      "geometry_type": "Polygon",
      "geometry": {
        "type": "Polygon",
        "coordinates": [
          [[500000.0, 1830000.0], [500010.0, 1830010.0], [500010.0, 1830000.0],
           [500000.0, 1830010.0], [500000.0, 1830000.0]]
        ]
      },
      "crs": "EPSG:32644",
      "properties": {"name": "plot-2"},
      "status": "FAILED",
      "measurement_type": null,
      "value": null,
      "unit": null,
      "projected_crs": null,
      "error": "Invalid geometry: Self-intersection[500005 1830005]"
    }
  ]
}
```

Feature `status` is one of:

| Status | Meaning |
| --- | --- |
| `MEASURED` | Polygon or MultiPolygon (`area`, `m²`), LineString or MultiLineString (`length`, `m`). |
| `NO_MEASUREMENT` | Point or MultiPoint. Nothing is measured. |
| `UNSUPPORTED` | Any other geometry type, for example a GeometryCollection. |
| `FAILED` | Missing or empty geometry, invalid geometry, or a coordinate transformation error. `error` says why. |

`value` is stored at full precision and rounded to three decimals in the response.

### `GET /health`

Returns `{"status": "ok"}`.

### Errors

Errors use the body `{"detail": "..."}`. Request-validation errors (`422`) use FastAPI's standard
`detail` list.

| Status | When |
| --- | --- |
| `400` | Unsupported extension, empty upload, malformed or unreadable KML/ZIP/Shapefile, missing Shapefile component (including `.prj`), unsafe or oversized ZIP, no features. |
| `404` | Unknown file id. |
| `409` | Measurements requested for a file that failed or has not finished processing. |
| `413` | Upload larger than `MAX_UPLOAD_BYTES`. |
| `422` | No `file` field in the request. |
| `500` | Unexpected failure. The file is marked `FAILED` and the cause is logged, not returned. |

A rejected upload (`400`) is not stored. A failure on one feature never fails the file; it shows
up on that feature, and the other features are still measured.

## Architecture

```text
HTTP request
    ↓
FastAPI route                    app/api/files.py
    ↓
file-processing service          app/services/files.py
    ↓
GeoPandas / Shapely              app/services/geospatial.py   (read, validate, extract)
    ↓
CRS selection + transformation   app/services/measurement.py
    ↓
measurement calculation          app/services/measurement.py
    ↓
database                         app/db/models.py (SQLAlchemy, SQLite)
    ↓
API response                     app/schemas/files.py
```

- `geospatial.py` turns an upload into plain records (CRS plus a list of features). It has no
  database access.
- `measurement.py` measures one geometry at a time and has no database access either, so the
  CRS logic can be tested with Shapely objects alone.
- `files.py` connects the two and stores the result.

For ZIP uploads the archive is inspected before anything is written. Entries with absolute or `..`
paths, encrypted entries, too many entries and archives that expand past the size limit are
rejected. Only the Shapefile components are extracted, and they are written under fixed names in a
temporary directory that is removed afterwards, so member names never become file paths.
macOS `__MACOSX` metadata entries are ignored.

KML uploads must contain a `<kml>` root and no DOCTYPE declaration. GDAL exposes each KML folder as
a layer, and all layers are read and merged; `feature_index` counts across them.

## CRS strategy

For every feature:

1. Take the centroid of the geometry in its source CRS and transform it to WGS 84 (EPSG:4326).
2. Pick the UTM zone for that longitude, north (`EPSG:326xx`) or south (`EPSG:327xx`) by the sign of the latitude.
3. Transform the geometry from the source CRS to that UTM zone, with `always_xy=True` so the axis
   order matches how Shapely stores coordinates.
4. Compute `area` or `length` on the projected geometry. UTM units are metres.

This is applied to projected source CRSs as well as geographic ones. A projected CRS is not
automatically suitable for measuring: at 16.5°N the raw area of a polygon in Web Mercator
(EPSG:3857) is about 9% larger than its ground area, and the error grows towards the poles.

Limitations:

- UTM is only defined from 80°S to 84°N. Features outside that range are reported as `FAILED`.
- UTM is conformal, not equal-area. Its scale factor is 0.9996 on a zone's central meridian and
  about 1.001 at the zone edge near the equator, so lengths are off by up to roughly 0.1% and
  areas by up to roughly 0.2%. Very large features, or features that span several zones, are measured in the zone of their
  centroid and carry more error.
- The zone is chosen from the centroid, so features that cross the antimeridian are not handled
  specially.
- Datum shifts from non-WGS 84 source CRSs use PROJ's default transformation. Without the optional
  PROJ grid files that can be less accurate than a regional transformation.
- Heights are dropped; measurements are planar.

## Design decisions

- **FastAPI and Pydantic.** Typed request and response models and generated OpenAPI documentation
  for a small API.
- **SQLite via SQLAlchemy.** The data is a file record and a flat list of per-feature rows. No
  spatial queries are needed, so a spatial database would add setup without a benefit. SQLAlchemy
  keeps the database URL configurable.
- **Synchronous processing.** Uploads are limited to 20 MiB by default, so the upload is processed inside
  the request. The client gets a final result in one call and there
  is no queue to operate. Route handlers are plain `def` functions, so FastAPI runs them in its
  thread pool and the event loop stays free. `PENDING` exists in the status vocabulary for when
  processing moves to a background task; today clients see `COMPLETED` or `FAILED`.
- **GeoPandas, Shapely, PyProj.** GeoPandas (through pyogrio) reads KML and Shapefile, Shapely
  provides validity checks and the area/length calculations, and PyProj performs the transformations.
- **Per-feature UTM instead of one CRS per file.** Files can span several zones, so choosing per
  feature keeps each measurement close to its own zone, at the cost of one extra centroid
  transform per feature. Transformer objects are cached.
- **Invalid geometries are reported, not repaired.** Repairing a self-intersecting polygon
  changes its area in ways the caller cannot see.
- **Missing `.prj` is an error.** Guessing a CRS would silently produce wrong measurements.
- **Upload size.** The limit is checked against the `Content-Length` header and against the bytes
  actually read. The framework still buffers an upload before the route runs, so a reverse
  proxy should enforce a body limit when the service is exposed publicly.

## Development

```bash
ruff check .
ruff format .
mypy app tests
pytest
```

Ruff handles linting and formatting (configuration in `pyproject.toml`). mypy runs with
`check_untyped_defs`; GeoPandas, pandas, Shapely and pyogrio have no usable type information
and are imported without type checking.

## Contributing

1. Create a branch and make the change.
2. Add or update tests for the behaviour you changed.
3. Run the four commands above and make sure they pass.
4. Open a pull request that explains what changed and why.

## Learning and future scope

What building this showed:

- GDAL's KML driver exposes each folder as its own layer, so reading only the default layer
  silently drops most placemarks in a typical export.
- Axis order is easy to get wrong when transforming coordinates; `always_xy=True` keeps it
  consistent.
- A projected CRS is no guarantee of accurate measurements. Web Mercator distorts area by a
  factor that depends on latitude.
- Pandas converts some values (NaN, NaT, timestamps) into things JSON cannot represent, so
  properties are normalised before storage.
- Per-feature isolation needs to be designed in: invalid geometries, missing geometries and
  unsupported types all have to be reported on the feature rather than raised.

Future scope:

- Paginate the measurements endpoint for files with very large feature counts.
- Move processing to a background task and use the `PENDING` and `PROCESSING` statuses.
- Support GeoJSON uploads.
- Add an equal-area or geodesic option for very large features.
- Add Alembic migrations once the schema starts to change.

## License

MIT. See [LICENSE](LICENSE).
