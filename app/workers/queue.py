import logging
import uuid
from functools import lru_cache
from typing import Protocol

from redis import Redis
from redis.exceptions import RedisError
from rq import Queue

from app.core.config import get_settings

logger = logging.getLogger(__name__)

QUEUE_NAME = "processing"
TASK_PATH = "app.workers.tasks.process_file_job"


class QueueUnavailableError(Exception):
    """The job queue could not be reached."""


class JobQueue(Protocol):
    """What the API needs from a job queue. Tests substitute an in-process implementation."""

    def enqueue(self, job_id: str) -> None: ...

    def ping(self) -> bool: ...


class RqJobQueue:
    def __init__(self, redis_url: str, job_timeout_seconds: int) -> None:
        self._redis = Redis.from_url(redis_url)
        self._queue = Queue(QUEUE_NAME, connection=self._redis)
        self._job_timeout_seconds = job_timeout_seconds

    def enqueue(self, job_id: str) -> None:
        try:
            self._queue.enqueue(
                TASK_PATH,
                job_id,
                # A new queue id lets the reconciler retry even if an older RQ job failed.
                job_id=f"{job_id}-{uuid.uuid4().hex}",
                job_timeout=self._job_timeout_seconds,
                result_ttl=0,
            )
        except RedisError as error:
            logger.error("queue_enqueue_failed", extra={"job_id": job_id, "detail": str(error)})
            raise QueueUnavailableError("The job queue is unavailable.") from error

    def ping(self) -> bool:
        try:
            return bool(self._redis.ping())
        except RedisError:
            return False


@lru_cache
def get_job_queue() -> JobQueue:
    settings = get_settings()
    return RqJobQueue(settings.redis_url, settings.job_timeout_seconds)
