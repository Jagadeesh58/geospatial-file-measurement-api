from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.core.config import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, Settings, get_settings
from app.db.database import get_session
from app.db.repositories import BoundingBox
from app.workers.queue import JobQueue, get_job_queue

SessionDep = Annotated[Session, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
JobQueueDep = Annotated[JobQueue, Depends(get_job_queue)]

UNPROCESSABLE = 422  # named differently across Starlette versions, so spelled out
MAX_LONGITUDE = 180.0
MAX_LATITUDE = 90.0


@dataclass(frozen=True)
class Pagination:
    limit: int
    offset: int


def get_pagination(
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE, description="Items per page.")] = (
        DEFAULT_PAGE_SIZE
    ),
    offset: Annotated[int, Query(ge=0, description="Items to skip.")] = 0,
) -> Pagination:
    return Pagination(limit=limit, offset=offset)


def get_bbox(
    bbox: Annotated[
        str | None,
        Query(
            description="Bounding box 'min_lon,min_lat,max_lon,max_lat' in WGS 84 degrees.",
            examples=["80.4,16.4,80.6,16.6"],
        ),
    ] = None,
) -> BoundingBox | None:
    if bbox is None:
        return None
    try:
        min_x, min_y, max_x, max_y = (float(part) for part in bbox.split(","))
    except ValueError:
        raise _invalid_bbox("bbox must be four comma-separated numbers.") from None
    if not (-MAX_LONGITUDE <= min_x < max_x <= MAX_LONGITUDE):
        raise _invalid_bbox("bbox longitudes must satisfy -180 <= min < max <= 180.")
    if not (-MAX_LATITUDE <= min_y < max_y <= MAX_LATITUDE):
        raise _invalid_bbox("bbox latitudes must satisfy -90 <= min < max <= 90.")
    return (min_x, min_y, max_x, max_y)


def _invalid_bbox(message: str) -> HTTPException:
    return HTTPException(UNPROCESSABLE, message)


PaginationDep = Annotated[Pagination, Depends(get_pagination)]
BoundingBoxDep = Annotated[BoundingBox | None, Depends(get_bbox)]
