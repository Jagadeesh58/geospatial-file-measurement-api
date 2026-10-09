import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.api.dependencies import JobQueueDep, SessionDep

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])


@router.get("/health", summary="Liveness")
def health() -> dict[str, str]:
    """The process is running. Does not touch any dependency."""
    return {"status": "ok"}


@router.get("/ready", summary="Readiness")
def ready(session: SessionDep, queue: JobQueueDep) -> JSONResponse:
    """`200` when the database and the job queue both respond, otherwise `503`."""
    checks = {"database": _database_responds(session), "queue": queue.ping()}
    healthy = all(checks.values())
    return JSONResponse(
        status_code=200 if healthy else 503,
        content={"status": "ready" if healthy else "unavailable", "checks": checks},
    )


def _database_responds(session: Session) -> bool:
    try:
        session.execute(text("SELECT 1"))
    except SQLAlchemyError as error:
        logger.error("database_check_failed", extra={"detail": str(error)})
        return False
    return True
