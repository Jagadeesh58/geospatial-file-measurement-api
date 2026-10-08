"""Upload processing: loads a dataset, measures each feature and persists the results."""

import logging
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.models import FeatureMeasurement, FileStatus, UploadedFile
from app.services.geospatial import (
    GeospatialDataset,
    describe_crs,
    detect_file_type,
    load_dataset,
)
from app.services.measurement import measure_feature

logger = logging.getLogger(__name__)

MAX_DISPLAY_FILENAME_LENGTH = 255
UNEXPECTED_ERROR_MESSAGE = "Unexpected error while processing the file."


def process_upload(
    session: Session, filename: str, content: bytes, settings: Settings
) -> UploadedFile:
    """Process an upload synchronously.

    Raises InvalidGeospatialFile when the content is rejected; nothing is stored then.
    """
    file_type = detect_file_type(filename)
    dataset = load_dataset(file_type, content, settings)

    record = UploadedFile(
        filename=_display_name(filename),
        file_type=file_type,
        status=FileStatus.PROCESSING,
        feature_count=len(dataset.features),
        crs=describe_crs(dataset.crs),
    )
    session.add(record)
    session.commit()

    try:
        session.add_all(_measure_features(record.id, dataset))
        record.status = FileStatus.COMPLETED
        session.commit()
    except Exception:
        # Broad on purpose: the failure is recorded on the file and then re-raised.
        session.rollback()
        logger.exception("Processing failed for uploaded file %s", record.id)
        _mark_failed(session, record.id)
        raise
    return record


def get_file(session: Session, file_id: str) -> UploadedFile | None:
    return session.get(UploadedFile, file_id)


def list_measurements(session: Session, file_id: str) -> Sequence[FeatureMeasurement]:
    statement = (
        select(FeatureMeasurement)
        .where(FeatureMeasurement.file_id == file_id)
        .order_by(FeatureMeasurement.feature_index)
    )
    return session.scalars(statement).all()


def _measure_features(file_id: str, dataset: GeospatialDataset) -> list[FeatureMeasurement]:
    crs_label = describe_crs(dataset.crs)
    measurements = []
    for feature in dataset.features:
        # Each feature is measured on its own so one bad geometry cannot fail the file.
        result = measure_feature(feature.geometry, dataset.crs)
        measurements.append(
            FeatureMeasurement(
                file_id=file_id,
                feature_index=feature.index,
                geometry_type=feature.geometry.geom_type if feature.geometry else None,
                geometry=feature.geometry.__geo_interface__ if feature.geometry else None,
                crs=crs_label,
                properties=feature.properties,
                status=result.status,
                measurement_type=result.measurement_type,
                value=result.value,
                unit=result.unit,
                projected_crs=result.projected_crs,
                error=result.error,
            )
        )
    return measurements


def _mark_failed(session: Session, file_id: str) -> None:
    record = session.get(UploadedFile, file_id)
    if record is None:
        return
    record.status = FileStatus.FAILED
    record.error = UNEXPECTED_ERROR_MESSAGE
    session.commit()


def _display_name(filename: str) -> str:
    # Stored for display only; it is never used to build a filesystem path.
    name = filename.replace("\\", "/").rsplit("/", 1)[-1]
    return name[:MAX_DISPLAY_FILENAME_LENGTH]
