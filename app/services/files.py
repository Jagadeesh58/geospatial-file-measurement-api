"""Upload intake and file queries."""

import logging
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.security import sanitize_filename
from app.db.models import Feature, ProcessingJob, ProcessingStatus, UploadedFile
from app.db.repositories import FeatureFilter, FeatureRepository, FileRepository, JobRepository
from app.services.geospatial import (
    COPY_CHUNK_BYTES,
    STORAGE_EXTENSIONS,
    InvalidGeospatialFile,
    detect_file_type,
    validate_upload,
)
from app.workers.queue import JobQueue, QueueUnavailableError

logger = logging.getLogger(__name__)

QUEUE_UNAVAILABLE_MESSAGE = "The job queue was unavailable when the upload was accepted."


class UploadTooLargeError(Exception):
    """The upload is larger than the configured limit."""


def accept_upload(
    session: Session, queue: JobQueue, stream: BinaryIO, filename: str, settings: Settings
) -> UploadedFile:
    """Validate and store an upload, record it, and queue its processing job."""
    file_type = detect_file_type(filename)
    file_id = uuid.uuid4().hex
    storage_name = f"{file_id}{STORAGE_EXTENSIONS[file_type]}"
    path = settings.upload_dir / storage_name

    recorded = False
    try:
        size_bytes = _store(stream, path, settings.max_upload_bytes)
        validate_upload(file_type, path, settings)

        record = UploadedFile(
            id=file_id,
            filename=sanitize_filename(filename),
            file_type=file_type,
            status=ProcessingStatus.PENDING,
            size_bytes=size_bytes,
            storage_name=storage_name,
        )
        job = ProcessingJob(file_id=file_id, status=ProcessingStatus.PENDING)
        FileRepository(session).add(record)
        JobRepository(session).add(job)
        session.commit()
        recorded = True
    finally:
        if not recorded:
            path.unlink(missing_ok=True)

    try:
        # Enqueued only after the commit, so the worker always finds the job it is given.
        queue.enqueue(job.id)
        job.last_dispatch_attempt_at = datetime.now(UTC)
        session.commit()
    except QueueUnavailableError:
        _fail_unqueued(session, file_id, path)
        raise
    logger.info(
        "upload_accepted",
        extra={"file_id": file_id, "job_id": job.id, "size_bytes": size_bytes},
    )
    return record


def get_file(session: Session, file_id: str) -> UploadedFile | None:
    return FileRepository(session).get(file_id)


def get_job(session: Session, job_id: str) -> ProcessingJob | None:
    return JobRepository(session).get(job_id)


def list_features(
    session: Session,
    file_id: str,
    filters: FeatureFilter,
    *,
    limit: int,
    offset: int,
    include_details: bool,
) -> tuple[Sequence[Feature], int]:
    return FeatureRepository(session).page(
        file_id, filters, limit=limit, offset=offset, include_details=include_details
    )


def _store(stream: BinaryIO, path: Path, max_bytes: int) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with path.open("wb") as target:
        while chunk := stream.read(COPY_CHUNK_BYTES):
            written += len(chunk)
            if written > max_bytes:
                raise UploadTooLargeError(f"Uploads are limited to {max_bytes} bytes.")
            target.write(chunk)
    if written == 0:
        raise InvalidGeospatialFile("The uploaded file is empty.")
    return written


def _fail_unqueued(session: Session, file_id: str, path: Path) -> None:
    record = FileRepository(session).get(file_id)
    if record is not None and record.job is not None:
        record.status = record.job.status = ProcessingStatus.FAILED
        record.error = record.job.error = QUEUE_UNAVAILABLE_MESSAGE
        session.commit()
    path.unlink(missing_ok=True)
