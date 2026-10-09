from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import ColumnElement, func, insert, select
from sqlalchemy.orm import Session, defer

from app.db.models import (
    WGS84_SRID,
    Feature,
    FeatureStatus,
    MeasurementType,
    ProcessingJob,
    UploadedFile,
)

BoundingBox = tuple[float, float, float, float]


@dataclass(frozen=True)
class FeatureFilter:
    status: FeatureStatus | None = None
    measurement_type: MeasurementType | None = None
    geometry_type: str | None = None
    bbox: BoundingBox | None = None


class FileRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, record: UploadedFile) -> None:
        self._session.add(record)

    def get(self, file_id: str) -> UploadedFile | None:
        return self._session.get(UploadedFile, file_id)


class JobRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, job: ProcessingJob) -> None:
        self._session.add(job)

    def get(self, job_id: str) -> ProcessingJob | None:
        return self._session.get(ProcessingJob, job_id)

    def get_for_update(self, job_id: str) -> ProcessingJob | None:
        """Lock a job row while claiming it so duplicate deliveries cannot process it twice."""
        statement = select(ProcessingJob).where(ProcessingJob.id == job_id).with_for_update()
        return self._session.scalars(statement).one_or_none()


class FeatureRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add_batch(self, rows: Sequence[dict[str, Any]]) -> None:
        """Insert many features with one statement instead of one INSERT per row."""
        self._session.execute(insert(Feature), list(rows))

    def page(
        self,
        file_id: str,
        filters: FeatureFilter,
        *,
        limit: int,
        offset: int,
        include_details: bool,
    ) -> tuple[Sequence[Feature], int]:
        """Return one page of a file's features and the total number matching the filters.

        Measurement listings skip the geometry and property columns, which dominate row size.
        The WGS 84 index copy is never returned.
        """
        conditions = _conditions(file_id, filters)
        statement = (
            select(Feature)
            .where(*conditions)
            .order_by(Feature.feature_index)
            .limit(limit)
            .offset(offset)
            .options(defer(Feature.geom))
        )
        if not include_details:
            statement = statement.options(defer(Feature.geometry), defer(Feature.properties))

        items = self._session.scalars(statement).all()
        total = self._session.scalar(select(func.count()).select_from(Feature).where(*conditions))
        return items, total or 0


def _conditions(file_id: str, filters: FeatureFilter) -> list[ColumnElement[bool]]:
    conditions: list[ColumnElement[bool]] = [Feature.file_id == file_id]
    if filters.status is not None:
        conditions.append(Feature.status == filters.status)
    if filters.measurement_type is not None:
        conditions.append(Feature.measurement_type == filters.measurement_type)
    if filters.geometry_type is not None:
        conditions.append(Feature.geometry_type == filters.geometry_type)
    if filters.bbox is not None:
        min_x, min_y, max_x, max_y = filters.bbox
        envelope = func.ST_MakeEnvelope(min_x, min_y, max_x, max_y, WGS84_SRID)
        conditions.append(func.ST_Intersects(Feature.geom, envelope))
    return conditions
