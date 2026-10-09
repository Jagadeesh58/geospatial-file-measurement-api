"""Processing of a queued job: read the upload, measure every feature, store the results."""

import logging
import math
import time
from datetime import UTC, datetime
from typing import Any

from geoalchemy2.shape import from_shape
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.logging import job_id_var
from app.db.models import WGS84_SRID, ProcessingJob, ProcessingStatus, UploadedFile
from app.db.repositories import FeatureRepository, FileRepository, JobRepository
from app.services.geospatial import (
    InvalidGeospatialFile,
    SourceFeature,
    describe_crs,
    open_dataset,
)
from app.services.measurement import FeatureMeasurer

logger = logging.getLogger(__name__)

UNEXPECTED_ERROR_MESSAGE = "Unexpected error while processing the file."


def run_processing_job(session: Session, settings: Settings, job_id: str) -> None:
    """Run one job to completion and always leave it COMPLETED or FAILED."""
    token = job_id_var.set(job_id)
    try:
        _run(session, settings, job_id)
    finally:
        job_id_var.reset(token)


def _run(session: Session, settings: Settings, job_id: str) -> None:
    job = JobRepository(session).get_for_update(job_id)
    if job is None:
        logger.warning("job_not_found")
        session.rollback()
        return
    if job.status is not ProcessingStatus.PENDING:
        # A queue may deliver a job more than once; finished or active work is not repeated.
        logger.info("job_skipped", extra={"status": job.status.value})
        session.rollback()
        return

    file_id = job.file_id
    record = FileRepository(session).get(file_id)
    if record is None:
        job.status = ProcessingStatus.FAILED
        job.error = "The uploaded file record no longer exists."
        job.finished_at = datetime.now(UTC)
        session.commit()
        logger.error("job_file_missing", extra={"file_id": file_id})
        return

    job.status = record.status = ProcessingStatus.PROCESSING
    job.started_at = datetime.now(UTC)
    job.attempt_count += 1
    job.error = record.error = None
    session.commit()
    logger.info("job_started", extra={"file_id": file_id, "attempt": job.attempt_count})

    started = time.perf_counter()
    storage_path = settings.upload_dir / record.storage_name
    remove_upload = False
    try:
        feature_count, crs_label = _process(session, settings, record)
        _complete(session, job_id, feature_count, crs_label)
        remove_upload = True
        logger.info(
            "job_completed",
            extra={
                "file_id": file_id,
                "feature_count": feature_count,
                "duration_ms": round((time.perf_counter() - started) * 1000, 1),
            },
        )
    except InvalidGeospatialFile as error:
        session.rollback()
        _fail(session, job_id, str(error))
        remove_upload = True
        logger.warning("job_failed", extra={"file_id": file_id, "reason": str(error)})
    except SQLAlchemyError:
        # Keep the source file when persistence fails; the reconciler can retry the job.
        session.rollback()
        logger.exception("job_database_error", extra={"file_id": file_id})
        raise
    except Exception:
        session.rollback()
        logger.exception("job_crashed", extra={"file_id": file_id})
        _fail(session, job_id, UNEXPECTED_ERROR_MESSAGE)
        remove_upload = True
    finally:
        # Never delete the only source copy until a terminal state is committed.
        if remove_upload:
            storage_path.unlink(missing_ok=True)


def _process(session: Session, settings: Settings, record: UploadedFile) -> tuple[int, str]:
    features = FeatureRepository(session)
    storage_path = settings.upload_dir / record.storage_name
    total = 0
    crs_label = ""

    with open_dataset(record.file_type, storage_path, settings) as dataset:
        for batch in dataset.batches(settings.processing_batch_size):
            crs_label = crs_label or describe_crs(batch.crs)
            measurer = FeatureMeasurer(batch.crs)
            features.add_batch(
                [
                    _feature_row(record.id, feature, measurer, crs_label)
                    for feature in batch.features
                ]
            )
            total += len(batch.features)

    if total == 0:
        raise InvalidGeospatialFile("The file contains no features.")
    return total, crs_label


def _feature_row(
    file_id: str, feature: SourceFeature, measurer: FeatureMeasurer, crs_label: str
) -> dict[str, Any]:
    # Each feature is measured on its own so one bad geometry cannot fail the whole file.
    result = measurer.measure(feature.geometry)
    geometry = feature.geometry
    wgs84_geometry = measurer.to_wgs84(geometry)
    source_geometry_is_storable = (
        geometry is not None
        and not geometry.is_empty
        and all(math.isfinite(bound) for bound in geometry.bounds)
    )
    return {
        "file_id": file_id,
        "feature_index": feature.index,
        "geometry_type": geometry.geom_type if geometry is not None else None,
        "geometry": (
            geometry.__geo_interface__
            if source_geometry_is_storable and geometry is not None
            else None
        ),
        "geom": from_shape(wgs84_geometry, srid=WGS84_SRID) if wgs84_geometry is not None else None,
        "crs": crs_label,
        "properties": feature.properties,
        "status": result.status,
        "measurement_type": result.measurement_type,
        "value": result.value,
        "unit": result.unit,
        "projected_crs": result.projected_crs,
        "error": result.error,
    }


def _complete(session: Session, job_id: str, feature_count: int, crs_label: str) -> None:
    job, record = _load(session, job_id)
    record.feature_count = feature_count
    record.crs = crs_label
    job.status = record.status = ProcessingStatus.COMPLETED
    job.finished_at = datetime.now(UTC)
    session.commit()


def _fail(session: Session, job_id: str, message: str) -> None:
    job, record = _load(session, job_id)
    job.status = record.status = ProcessingStatus.FAILED
    job.error = record.error = message
    job.finished_at = datetime.now(UTC)
    session.commit()


def _load(session: Session, job_id: str) -> tuple[ProcessingJob, UploadedFile]:
    job = JobRepository(session).get(job_id)
    assert job is not None  # the job was loaded at the start of this run
    record = FileRepository(session).get(job.file_id)
    assert record is not None
    return job, record
