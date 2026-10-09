import io
import json
import logging

from app.core.logging import ContextFilter, JsonFormatter, job_id_var, request_id_var


def make_logger(stream: io.StringIO) -> logging.Logger:
    handler = logging.StreamHandler(stream)
    handler.addFilter(ContextFilter())
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("tests.structured")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)
    return logger


def test_records_are_json_with_extra_fields() -> None:
    stream = io.StringIO()

    make_logger(stream).info("job_completed", extra={"feature_count": 12, "duration_ms": 3.5})

    record = json.loads(stream.getvalue())
    assert record["message"] == "job_completed"
    assert record["level"] == "INFO"
    assert record["feature_count"] == 12
    assert record["duration_ms"] == 3.5
    assert record["timestamp"].endswith("+00:00")


def test_request_and_job_ids_are_attached_when_set() -> None:
    stream = io.StringIO()
    request_token = request_id_var.set("req-1")
    job_token = job_id_var.set("job-1")
    try:
        make_logger(stream).info("working")
    finally:
        request_id_var.reset(request_token)
        job_id_var.reset(job_token)

    record = json.loads(stream.getvalue())
    assert record["request_id"] == "req-1"
    assert record["job_id"] == "job-1"


def test_ids_are_omitted_when_not_set() -> None:
    stream = io.StringIO()

    make_logger(stream).info("idle")

    record = json.loads(stream.getvalue())
    assert "request_id" not in record
    assert "job_id" not in record


def test_exceptions_are_included() -> None:
    stream = io.StringIO()
    logger = make_logger(stream)

    try:
        raise ValueError("boom")
    except ValueError:
        logger.exception("failed")

    assert "ValueError: boom" in json.loads(stream.getvalue())["exception"]
