from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.models import FileType, ProcessingJob, ProcessingStatus, UploadedFile
from app.workers.reconcile import reconcile_once
from tests.queues import InlineJobQueue


def _create_stale_job(database: Engine, settings: Settings, attempts: int) -> str:
    now = datetime.now(UTC)
    storage_name = "stale-upload.kml"
    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    (settings.upload_dir / storage_name).write_text("temporary source", encoding="utf-8")

    with Session(database) as session:
        record = UploadedFile(
            id="stale-file",
            filename="survey.kml",
            file_type=FileType.KML,
            status=ProcessingStatus.PROCESSING,
            size_bytes=16,
            storage_name=storage_name,
        )
        job = ProcessingJob(
            id="stale-job",
            file_id=record.id,
            status=ProcessingStatus.PROCESSING,
            created_at=now - timedelta(minutes=10),
            started_at=now - timedelta(seconds=settings.job_timeout_seconds + 120),
            attempt_count=attempts,
        )
        session.add_all([record, job])
        session.commit()
    return "stale-job"


def _session_factory(database: Engine) -> Callable[[], Session]:
    return lambda: Session(database, expire_on_commit=False)


def _create_pending_job(database: Engine, settings: Settings) -> str:
    storage_name = "pending-upload.kml"
    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    (settings.upload_dir / storage_name).write_text("temporary source", encoding="utf-8")

    with Session(database) as session:
        record = UploadedFile(
            id="pending-file",
            filename="survey.kml",
            file_type=FileType.KML,
            status=ProcessingStatus.PENDING,
            size_bytes=16,
            storage_name=storage_name,
        )
        job = ProcessingJob(
            id="pending-job",
            file_id=record.id,
            status=ProcessingStatus.PENDING,
        )
        session.add_all([record, job])
        session.commit()
    return job.id


def test_stale_processing_job_is_reset_and_dispatched(
    database: Engine, settings: Settings
) -> None:
    settings.job_timeout_seconds = 1
    settings.job_recovery_grace_seconds = 1
    settings.queue_retry_interval_seconds = 1
    _create_stale_job(database, settings, attempts=1)
    queue = InlineJobQueue(database, settings)
    queue.run_immediately = False

    result = reconcile_once(_session_factory(database), queue, settings)

    assert result.recovered == 1
    assert result.dispatched == 1
    assert queue.pending == ["stale-job"]
    with Session(database) as session:
        job = session.get(ProcessingJob, "stale-job")
        record = session.get(UploadedFile, "stale-file")
        assert job is not None and job.status is ProcessingStatus.PENDING
        assert job.started_at is None and job.last_dispatch_attempt_at is not None
        assert record is not None and record.status is ProcessingStatus.PENDING


def test_job_at_attempt_limit_is_failed_and_source_is_removed(
    database: Engine, settings: Settings
) -> None:
    settings.job_timeout_seconds = 1
    settings.job_recovery_grace_seconds = 1
    _create_stale_job(database, settings, attempts=settings.max_processing_attempts)
    queue = InlineJobQueue(database, settings)
    queue.run_immediately = False

    result = reconcile_once(_session_factory(database), queue, settings)

    assert result.failed == 1
    assert result.dispatched == 0
    assert queue.pending == []
    assert not (settings.upload_dir / "stale-upload.kml").exists()
    with Session(database) as session:
        job = session.get(ProcessingJob, "stale-job")
        record = session.get(UploadedFile, "stale-file")
        assert job is not None and job.status is ProcessingStatus.FAILED
        assert record is not None and record.status is ProcessingStatus.FAILED


def test_pending_job_without_dispatch_timestamp_is_dispatched(
    database: Engine, settings: Settings
) -> None:
    job_id = _create_pending_job(database, settings)
    queue = InlineJobQueue(database, settings)
    queue.run_immediately = False

    result = reconcile_once(_session_factory(database), queue, settings)

    assert result.dispatched == 1
    assert result.recovered == 0
    assert queue.pending == [job_id]
    with Session(database) as session:
        job = session.get(ProcessingJob, job_id)
        assert job is not None
        assert job.status is ProcessingStatus.PENDING
        assert job.last_dispatch_attempt_at is not None


def test_pending_job_is_redispatched_after_the_retry_interval(
    database: Engine, settings: Settings
) -> None:
    job_id = _create_pending_job(database, settings)
    previous_attempt = datetime.now(UTC) - timedelta(
        seconds=settings.queue_retry_interval_seconds + 1
    )
    with Session(database) as session:
        job = session.get(ProcessingJob, job_id)
        assert job is not None
        job.last_dispatch_attempt_at = previous_attempt
        session.commit()

    queue = InlineJobQueue(database, settings)
    queue.run_immediately = False
    result = reconcile_once(_session_factory(database), queue, settings)

    assert result.dispatched == 1
    assert queue.pending == [job_id]
    with Session(database) as session:
        job = session.get(ProcessingJob, job_id)
        assert job is not None and job.last_dispatch_attempt_at is not None
        assert job.last_dispatch_attempt_at > previous_attempt
