"""Starts a queue worker: `python -m app.workers.run`."""

from redis import Redis
from rq import Worker

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.workers.queue import QUEUE_NAME


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    Worker([QUEUE_NAME], connection=Redis.from_url(settings.redis_url)).work()


if __name__ == "__main__":
    main()
