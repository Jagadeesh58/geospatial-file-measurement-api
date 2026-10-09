import hmac
import logging
import re
import time
import uuid
from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from app.core.config import Settings
from app.core.logging import request_id_var
from app.core.rate_limit import RateLimiterUnavailable, get_rate_limiter

logger = logging.getLogger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"
API_KEY_HEADER = "X-API-Key"
PAYLOAD_TOO_LARGE = 413
MULTIPART_OVERHEAD_BYTES = 64 * 1024
_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


async def request_context(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    supplied = request.headers.get(REQUEST_ID_HEADER, "")
    request_id = supplied if _VALID_REQUEST_ID.fullmatch(supplied) else uuid.uuid4().hex
    request.state.request_id = request_id
    token = request_id_var.set(request_id)
    started = time.perf_counter()
    response: Response
    settings = request.app.state.settings

    try:
        response = _upload_size_response(request, settings.max_upload_bytes)
        if (
            response is None
            and settings.environment == "production"
            and request.url.path.startswith("/api/")
        ):
            response = await _production_api_response(request, settings)
        if response is None:
            response = await call_next(request)

        response.headers[REQUEST_ID_HEADER] = request_id
        logger.info(
            "request_completed",
            extra={
                "method": request.method,
                "path": request.url.path,
                "status_code": response.status_code,
                "duration_ms": round((time.perf_counter() - started) * 1000, 1),
            },
        )
        return response
    finally:
        request_id_var.reset(token)


def _upload_size_response(request: Request, max_upload_bytes: int) -> Response | None:
    if request.method != "POST" or request.url.path not in {"/api/files", "/api/files/"}:
        return None

    content_length = request.headers.get("content-length")
    if content_length is None:
        return None
    try:
        declared_bytes = int(content_length)
    except ValueError:
        return JSONResponse(status_code=400, content={"detail": "Invalid Content-Length header."})

    max_request_bytes = max_upload_bytes + MULTIPART_OVERHEAD_BYTES
    if declared_bytes < 0:
        return JSONResponse(status_code=400, content={"detail": "Invalid Content-Length header."})
    if declared_bytes > max_request_bytes:
        return JSONResponse(
            status_code=PAYLOAD_TOO_LARGE,
            content={"detail": f"Uploads are limited to {max_upload_bytes} bytes."},
        )
    return None


async def _production_api_response(request: Request, settings: Settings) -> Response | None:
    client_host = request.client.host if request.client is not None else "unknown"
    try:
        allowed, retry_after = await run_in_threadpool(
            get_rate_limiter(settings.redis_url).allow,
            client_host,
            settings.rate_limit_requests,
            settings.rate_limit_window_seconds,
        )
    except RateLimiterUnavailable:
        logger.error("rate_limiter_unavailable")
        return JSONResponse(
            status_code=503,
            content={"detail": "Request protection is temporarily unavailable."},
        )

    if not allowed:
        return JSONResponse(
            status_code=429,
            content={"detail": "Rate limit exceeded. Try again later."},
            headers={"Retry-After": str(retry_after)},
        )

    expected = settings.api_key.get_secret_value() if settings.api_key is not None else ""
    supplied = request.headers.get(API_KEY_HEADER, "")
    if not hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8")):
        return JSONResponse(status_code=401, content={"detail": "A valid API key is required."})
    return None
