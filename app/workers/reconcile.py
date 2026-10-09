"""Recover jobs whose dispatch or worker process was interrupted."""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.db.database import get_engine
from app.db.models import Feature, ProcessingJob, ProcessingStatus, UploadedFile
from app.workers.queue import JobQueue, QueueUnavailableError, get_job_queue

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReconciliationResult:
    dispatched: int = 0
    recovered: int = 0
    failed: int = 0


def reconcile_once(
    session_factory: Callable[[], Session], queue: JobQueue, settings: Settings
) -> ReconciliationResult:
    """Requeue undispatched work and recover jobs left PROCESSING after a worker crash."""
    now = datetime.now(UTC)
    stale_before = now - timedelta(
        seconds=settings.job_timeout_seconds + settings.job_recovery_grace_seconds
    )
    retry_before = now - timedelta(seconds=settings.queue_retry_interval_seconds)
    upload_paths_to_remove: list[Path] = []
    recovered = 0
    failed = 0

    with session_factory() as session:
        stale_jobs = session.scalars(
            select(ProcessingJob)
            .where(
                ProcessingJob.status == ProcessingStatus.PROCESSING,
                ProcessingJob.started_at.is_not(None),
                ProcessingJob.started_at <= stale_before,
            )
            .order_by(ProcessingJob.started_at)
            .limit(settings.reconciliation_batch_size)
            .with_for_update(skip_locked=True)
        ).all()

        for job in stale_jobs:
            record = session.get(UploadedFile, job.file_id)
            if record is None:
                job.status = ProcessingStatus.FAILED
                job.error = "The uploaded file record no longer exists."
                job.finished_at = now
                failed += 1
                continue

            session.execute(delete(Feature).where(Feature.file_id == record.id))
            if job.attempt_count >= settings.max_processing_attempts:
                failure_message = (
                    "Processing did not finish after "
                    f"{settings.max_processing_attempts} attempts."
                )
                job.status = record.status = ProcessingStatus.FAILED
                job.error = record.error = failure_message
                job.finished_at = now
                upload_paths_to_remove.append(settings.upload_dir / record.storage_name)
                failed += 1
                logger.error(
                    "stale_job_failed", extra={"job_id": job.id, "attempts": job.attempt_count}
                )
                continue

            job.status = record.status = ProcessingStatus.PENDING
            job.started_at = None
            job.finished_at = None
            job.error = record.error = None
            job.last_dispatch_attempt_at = None
            recovered += 1
            logger.warning(
                "stale_job_recovered", extra={"job_id": job.id, "attempts": job.attempt_count}
            )

        session.flush()
        eligible_jobs = session.scalars(
            select(ProcessingJob)
            .where(
                ProcessingJob.status == ProcessingStatus.PENDING,
                or_(
                    ProcessingJob.last_dispatch_attempt_at.is_(None),
                    ProcessingJob.last_dispatch_attempt_at <= retry_before,
                ),
            )
            .order_by(ProcessingJob.created_at)
            .limit(settings.reconciliation_batch_size)
            .with_for_update(skip_locked=True)
        ).all()
        job_ids = [job.id for job in eligible_jobs]
        for job in eligible_jobs:
            # Record the attempt before contacting Redis. A process crash here is retried after
            # the configured interval instead of creating an unbounded queue of duplicate jobs.
            job.last_dispatch_attempt_at = now
        session.commit()

    for path in upload_paths_to_remove:
        path.unlink(missing_ok=True)

    dispatched = 0
    for job_id in job_ids:
        try:
            queue.enqueue(job_id)
        except QueueUnavailableError:
            logger.warning("job_dispatch_deferred", extra={"job_id": job_id})
        except Exception:
            logger.exception("job_dispatch_failed", extra={"job_id": job_id})
        else:
            dispatched += 1
            logger.info("job_dispatched", extra={"job_id": job_id})

    return ReconciliationResult(dispatched=dispatched, recovered=recovered, failed=failed)


def _session_factory() -> Session:
    return Session(get_engine(), expire_on_commit=False)


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    queue = get_job_queue()
    logger.info("job_reconciler_started")

    while True:
        try:
            result = reconcile_once(_session_factory, queue, settings)
            if result != ReconciliationResult():
                logger.info(
                    "job_reconciliation_completed",
                    extra={
                        "dispatched": result.dispatched,
                        "recovered": result.recovered,
                        "failed": result.failed,
                    },
                )
        except Exception:
            logger.exception("job_reconciliation_failed")
        time.sleep(settings.reconciliation_interval_seconds)


if __name__ == "__main__":
    main()
