from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException, Query, Response, UploadFile, status

from app.api.dependencies import (
    BoundingBoxDep,
    JobQueueDep,
    PaginationDep,
    SessionDep,
    SettingsDep,
)
from app.db.models import FeatureStatus, MeasurementType, ProcessingStatus, UploadedFile
from app.db.repositories import FeatureFilter
from app.schemas.files import (
    FeatureItem,
    FeaturesPage,
    FileInfo,
    MeasurementItem,
    MeasurementsPage,
)
from app.services import files as file_service
from app.services.files import UploadTooLargeError
from app.services.geospatial import InvalidGeospatialFile
from app.workers.queue import QueueUnavailableError

PAYLOAD_TOO_LARGE = 413  # renamed between Starlette versions, so spelled out
# Multipart framing adds a little to the declared body size beyond the file itself.
MULTIPART_OVERHEAD_BYTES = 64 * 1024

router = APIRouter(prefix="/api/files", tags=["files"])

Responses = dict[int | str, dict[str, Any]]

NOT_FOUND: Responses = {status.HTTP_404_NOT_FOUND: {"description": "No file with this id."}}
NOT_READY: Responses = {
    status.HTTP_409_CONFLICT: {"description": "Processing failed or has not finished yet."}
}


@router.post(
    "/",
    response_model=FileInfo,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Upload a KML file or a zipped Shapefile",
    responses={
        status.HTTP_400_BAD_REQUEST: {
            "description": "Unsupported type, empty file, or a malformed or unsafe KML/ZIP."
        },
        PAYLOAD_TOO_LARGE: {"description": "Upload exceeds the size limit."},
        status.HTTP_503_SERVICE_UNAVAILABLE: {"description": "The job queue is unavailable."},
    },
)
def upload_file(
    file: UploadFile,
    response: Response,
    session: SessionDep,
    settings: SettingsDep,
    queue: JobQueueDep,
    content_length: Annotated[int | None, Header()] = None,
) -> UploadedFile:
    """Store the upload and queue it for processing.

    The response is `202 Accepted` with the file in `PENDING` state. Poll
    `GET /api/files/{id}/` (or `GET /api/jobs/{job_id}/`) until the status is `COMPLETED`
    or `FAILED`.
    """
    if content_length is not None and content_length > (
        settings.max_upload_bytes + MULTIPART_OVERHEAD_BYTES
    ):
        raise _payload_too_large(settings.max_upload_bytes)

    try:
        record = file_service.accept_upload(
            session, queue, file.file, file.filename or "", settings
        )
    except UploadTooLargeError:
        raise _payload_too_large(settings.max_upload_bytes) from None
    except InvalidGeospatialFile as error:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(error)) from error
    except QueueUnavailableError as error:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(error)) from error

    response.headers["Location"] = f"/api/files/{record.id}/"
    return record


@router.get("/{file_id}/", response_model=FileInfo, responses=NOT_FOUND, summary="File status")
def get_file_info(file_id: str, session: SessionDep) -> UploadedFile:
    """Metadata and processing status of an uploaded file."""
    return _get_file_or_404(session, file_id)


@router.get(
    "/{file_id}/measurements/",
    response_model=MeasurementsPage,
    responses={**NOT_FOUND, **NOT_READY},
    summary="Measurements of a file's features",
)
def get_measurements(
    file_id: str,
    session: SessionDep,
    pagination: PaginationDep,
    status_filter: Annotated[
        FeatureStatus | None, Query(alias="status", description="Only features with this status.")
    ] = None,
    measurement_type: Annotated[
        MeasurementType | None, Query(description="Only area or only length measurements.")
    ] = None,
    geometry_type: Annotated[
        str | None, Query(description="Only features of this geometry type, e.g. Polygon.")
    ] = None,
) -> MeasurementsPage:
    """Area (`m²`) and length (`m`) per feature, ordered by `feature_index`.

    Features that could not be measured are listed with their status and an `error`.
    """
    record = _get_completed_file(session, file_id)
    filters = FeatureFilter(
        status=status_filter, measurement_type=measurement_type, geometry_type=geometry_type
    )
    items, total = file_service.list_features(
        session,
        record.id,
        filters,
        limit=pagination.limit,
        offset=pagination.offset,
        include_details=False,
    )
    return MeasurementsPage(
        file_id=record.id,
        total=total,
        limit=pagination.limit,
        offset=pagination.offset,
        measurements=[MeasurementItem.model_validate(item) for item in items],
    )


@router.get(
    "/{file_id}/features/",
    response_model=FeaturesPage,
    responses={**NOT_FOUND, **NOT_READY},
    summary="Geometry and properties of a file's features",
)
def get_features(
    file_id: str,
    session: SessionDep,
    pagination: PaginationDep,
    bbox: BoundingBoxDep,
    geometry_type: Annotated[
        str | None, Query(description="Only features of this geometry type, e.g. Polygon.")
    ] = None,
) -> FeaturesPage:
    """Features as GeoJSON geometry in the file's own CRS (see `crs`), with their properties."""
    record = _get_completed_file(session, file_id)
    filters = FeatureFilter(geometry_type=geometry_type, bbox=bbox)
    items, total = file_service.list_features(
        session,
        record.id,
        filters,
        limit=pagination.limit,
        offset=pagination.offset,
        include_details=True,
    )
    return FeaturesPage(
        file_id=record.id,
        total=total,
        limit=pagination.limit,
        offset=pagination.offset,
        features=[FeatureItem.model_validate(item) for item in items],
    )


def _get_file_or_404(session: SessionDep, file_id: str) -> UploadedFile:
    record = file_service.get_file(session, file_id)
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "File not found.")
    return record


def _get_completed_file(session: SessionDep, file_id: str) -> UploadedFile:
    record = _get_file_or_404(session, file_id)
    if record.status is ProcessingStatus.FAILED:
        raise HTTPException(status.HTTP_409_CONFLICT, f"File processing failed: {record.error}")
    if record.status is not ProcessingStatus.COMPLETED:
        raise HTTPException(status.HTTP_409_CONFLICT, "File processing has not finished.")
    return record


def _payload_too_large(max_bytes: int) -> HTTPException:
    return HTTPException(PAYLOAD_TOO_LARGE, f"Uploads are limited to {max_bytes} bytes.")
