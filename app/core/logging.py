"""Structured logging with request and job identifiers attached to every record."""

import json
import logging
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
job_id_var: ContextVar[str | None] = ContextVar("job_id", default=None)

# Attributes every LogRecord has; anything else was passed through `extra=` and is emitted too.
_STANDARD_RECORD_FIELDS = {
    *logging.LogRecord("", 0, "", 0, "", (), None).__dict__,
    "message",
    "asctime",
    "taskName",
}
CONSOLE_FORMAT = "%(asctime)s %(levelname)-7s [%(request_id)s %(job_id)s] %(name)s: %(message)s"


class ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get() or "-"
        record.job_id = job_id_var.get() or "-"
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        payload.update(
            {
                key: value
                for key, value in record.__dict__.items()
                if key not in _STANDARD_RECORD_FIELDS and value not in (None, "-")
            }
        )
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str, log_format: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(ContextFilter())
    handler.setFormatter(
        JsonFormatter() if log_format == "json" else logging.Formatter(CONSOLE_FORMAT)
    )
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())
