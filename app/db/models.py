import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from geoalchemy2 import Geometry
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.database import Base

WGS84_SRID = 4326


class FileType(StrEnum):
    KML = "kml"
    SHAPEFILE = "shapefile"


class ProcessingStatus(StrEnum):
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


def _enum_column(enum_class: type[StrEnum]) -> Enum:
    # Persist the enum values ("kml") rather than member names ("KML"). Allowed values are
    # enforced by the named CHECK constraints below, which the migration mirrors.
    return Enum(
        enum_class,
        native_enum=False,
        create_constraint=False,
        length=20,
        values_callable=lambda members: [member.value for member in members],
    )


def _status_check(table: str, enum_class: type[StrEnum]) -> CheckConstraint:
    allowed = ", ".join(f"'{member.value}'" for member in enum_class)
    return CheckConstraint(f"status IN ({allowed})", name=f"ck_{table}_status")


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _new_id() -> str:
    return uuid.uuid4().hex


class UploadedFile(Base):
    __tablename__ = "uploaded_files"
    __table_args__ = (
        _status_check("uploaded_files", ProcessingStatus),
        CheckConstraint("feature_count >= 0", name="ck_uploaded_files_feature_count"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    filename: Mapped[str] = mapped_column(String(255))
    file_type: Mapped[FileType] = mapped_column(_enum_column(FileType))
    status: Mapped[ProcessingStatus] = mapped_column(_enum_column(ProcessingStatus))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    # Internal name under the upload directory; never exposed through the API.
    storage_name: Mapped[str] = mapped_column(String(64))
    feature_count: Mapped[int] = mapped_column(Integer, default=0)
    crs: Mapped[str | None] = mapped_column(String(255))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, onupdate=_utc_now
    )

    job: Mapped["ProcessingJob | None"] = relationship(
        back_populates="file", uselist=False, lazy="joined"
    )

    @property
    def job_id(self) -> str | None:
        return self.job.id if self.job else None


class ProcessingJob(Base):
    __tablename__ = "processing_jobs"
    __table_args__ = (_status_check("processing_jobs", ProcessingStatus),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    file_id: Mapped[str] = mapped_column(
        ForeignKey("uploaded_files.id", ondelete="CASCADE"), unique=True
    )
    status: Mapped[ProcessingStatus] = mapped_column(_enum_column(ProcessingStatus))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_dispatch_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    file: Mapped[UploadedFile] = relationship(back_populates="job")


class Feature(Base):
    """One feature of an uploaded file together with its measurement or failure reason."""

    __tablename__ = "features"
    __table_args__ = (
        UniqueConstraint("file_id", "feature_index", name="uq_features_file_id_feature_index"),
        Index("ix_features_file_id_status", "file_id", "status"),
        Index("ix_features_geom", "geom", postgresql_using="gist"),
        _status_check("features", FeatureStatus),
        CheckConstraint("feature_index >= 0", name="ck_features_feature_index"),
        CheckConstraint("value IS NULL OR value >= 0", name="ck_features_value"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    file_id: Mapped[str] = mapped_column(ForeignKey("uploaded_files.id", ondelete="CASCADE"))
    feature_index: Mapped[int] = mapped_column(Integer)
    geometry_type: Mapped[str | None] = mapped_column(String(50))
    # GeoJSON in the file's own CRS, returned as uploaded.
    geometry: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # WGS 84 copy of the same geometry, kept only so bounding-box queries can use a spatial index.
    geom: Mapped[Any | None] = mapped_column(
        Geometry("GEOMETRY", srid=WGS84_SRID, spatial_index=False)
    )
    crs: Mapped[str | None] = mapped_column(String(255))
    properties: Mapped[dict[str, Any]] = mapped_column(JSONB)
    status: Mapped[FeatureStatus] = mapped_column(_enum_column(FeatureStatus))
    measurement_type: Mapped[MeasurementType | None] = mapped_column(_enum_column(MeasurementType))
    value: Mapped[float | None] = mapped_column(Float(53))
    unit: Mapped[str | None] = mapped_column(String(10))
    projected_crs: Mapped[str | None] = mapped_column(String(255))
    error: Mapped[str | None] = mapped_column(Text)
