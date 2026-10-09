import logging

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from app.api.middleware import REQUEST_ID_HEADER, request_context
from app.api.routes import files, health, jobs
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging

logger = logging.getLogger(__name__)


def create_app(settings_override: Settings | None = None) -> FastAPI:
    settings = settings_override or get_settings()
    configure_logging(settings.log_level, settings.log_format)

    app = FastAPI(
        title="Geospatial File Processing & Measurement Platform",
        description=(
            "Upload a KML file or a zipped Shapefile. Features are extracted and measured "
            "(polygon area, line length) by a background worker; results are served from "
            "PostgreSQL/PostGIS with pagination."
        ),
        version="0.2.0",
    )
    app.state.settings = settings
    if settings_override is not None:
        app.dependency_overrides[get_settings] = lambda: settings
    app.middleware("http")(request_context)
    app.include_router(health.router)
    app.include_router(files.router)
    app.include_router(jobs.router)

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, error: Exception) -> JSONResponse:
        # Keep the response stable while retaining diagnostic details in server logs.
        logger.error(
            "request_unhandled_exception",
            exc_info=(type(error), error, error.__traceback__),
            extra={"request_id": getattr(request.state, "request_id", "")},
        )
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": "Internal server error."},
            headers={REQUEST_ID_HEADER: getattr(request.state, "request_id", "")},
        )

    return app


app = create_app()
