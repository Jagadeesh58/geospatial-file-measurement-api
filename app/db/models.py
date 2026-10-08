import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    JSON,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

from app.db.database import Base


class FileType(StrEnum):
    KML = "kml"
    SHAPEFILE = "shapefile"


class FileStatus(StrEnum):
    # PENDING is part of the public status vocabulary for when processing moves to a
    # background task. Uploads are processed inside the request today, so a client
    # only ever observes PROCESSING (if the process died mid-request), COMPLETED or FAILED.
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class FeatureStatus(StrEnum):
    MEASURED = "MEASURED"
    NO_MEASUREMENT = "NO_MEASUREMENT"
    UNSUPPORTED = "UNSUPPORTED"
    FAILED = "FAILED"


class MeasurementType(StrEnum):
    AREA = "area"
    LENGTH = "length"


class UTCDateTime(TypeDecorator[datetime]):
    """Stores UTC timestamps; SQLite drops tzinfo, so it is restored on read."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("Timestamps must be timezone-aware.")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        return None if value is None else value.replace(tzinfo=UTC)


def _enum_column(enum_class: type[StrEnum]) -> Enum:
    # Persist the enum values ("kml") rather than member names ("KML").
    return Enum(
        enum_class,
        native_enum=False,
        length=20,
        values_callable=lambda members: [member.value for member in members],
    )


def _new_file_id() -> str:
    return uuid.uuid4().hex


class UploadedFile(Base):
    __tablename__ = "uploaded_files"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_file_id)
    filename: Mapped[str] = mapped_column(String(255))
    file_type: Mapped[FileType] = mapped_column(_enum_column(FileType))
    status: Mapped[FileStatus] = mapped_column(_enum_column(FileStatus))
    feature_count: Mapped[int] = mapped_column(Integer, default=0)
    crs: Mapped[str | None] = mapped_column(String(255))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=lambda: datetime.now(UTC))


class FeatureMeasurement(Base):
    __tablename__ = "feature_measurements"
    __table_args__ = (UniqueConstraint("file_id", "feature_index"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    file_id: Mapped[str] = mapped_column(ForeignKey("uploaded_files.id"), index=True)
    feature_index: Mapped[int] = mapped_column(Integer)
    geometry_type: Mapped[str | None] = mapped_column(String(50))
    # GeoJSON-style geometry in the file's own CRS; kept so the API can return it.
    geometry: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    crs: Mapped[str | None] = mapped_column(String(255))
    properties: Mapped[dict[str, Any]] = mapped_column(JSON)
    status: Mapped[FeatureStatus] = mapped_column(_enum_column(FeatureStatus))
    measurement_type: Mapped[MeasurementType | None] = mapped_column(_enum_column(MeasurementType))
    value: Mapped[float | None] = mapped_column(Float)
    unit: Mapped[str | None] = mapped_column(String(10))
    projected_crs: Mapped[str | None] = mapped_column(String(255))
    error: Mapped[str | None] = mapped_column(Text)
