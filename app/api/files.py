from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, UploadFile, status
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.db.database import get_session
from app.db.models import FileStatus, UploadedFile
from app.schemas.files import FeatureMeasurementResponse, FileInfo, MeasurementsResponse
from app.services import files as file_service
from app.services.geospatial import InvalidGeospatialFile

# Multipart framing adds a little to the declared body size beyond the file itself.
MULTIPART_OVERHEAD_BYTES = 64 * 1024

router = APIRouter(prefix="/api/files", tags=["files"])

SessionDep = Annotated[Session, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


@router.post("/", response_model=FileInfo, status_code=status.HTTP_201_CREATED)
def upload_file(
    file: UploadFile,
    session: SessionDep,
    settings: SettingsDep,
    content_length: Annotated[int | None, Header()] = None,
) -> UploadedFile:
    """Upload a .kml file or a .zip containing a Shapefile and measure its features."""
    max_bytes = settings.max_upload_bytes
    if content_length is not None and content_length > max_bytes + MULTIPART_OVERHEAD_BYTES:
        raise _payload_too_large(max_bytes)

    content = file.file.read(max_bytes + 1)
    if len(content) > max_bytes:
        raise _payload_too_large(max_bytes)
    if not content:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "The uploaded file is empty.")

    try:
        return file_service.process_upload(session, file.filename or "", content, settings)
    except InvalidGeospatialFile as error:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(error)) from error


@router.get("/{file_id}/", response_model=FileInfo)
def get_file_info(file_id: str, session: SessionDep) -> UploadedFile:
    return _get_file_or_404(session, file_id)


@router.get("/{file_id}/measurements/", response_model=MeasurementsResponse)
def get_measurements(file_id: str, session: SessionDep) -> MeasurementsResponse:
    record = _get_file_or_404(session, file_id)
    if record.status is FileStatus.FAILED:
        raise HTTPException(status.HTTP_409_CONFLICT, f"File processing failed: {record.error}")
    if record.status is not FileStatus.COMPLETED:
        raise HTTPException(status.HTTP_409_CONFLICT, "File processing has not finished.")

    measurements = file_service.list_measurements(session, file_id)
    return MeasurementsResponse(
        file_id=record.id,
        measurements=[
            FeatureMeasurementResponse.model_validate(measurement) for measurement in measurements
        ],
    )


def _get_file_or_404(session: Session, file_id: str) -> UploadedFile:
    record = file_service.get_file(session, file_id)
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "File not found.")
    return record


def _payload_too_large(max_bytes: int) -> HTTPException:
    return HTTPException(
        status.HTTP_413_CONTENT_TOO_LARGE, f"Uploads are limited to {max_bytes} bytes."
    )
