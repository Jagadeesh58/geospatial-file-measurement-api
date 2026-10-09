from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, field_serializer

from app.db.models import FeatureStatus, FileType, MeasurementType, ProcessingStatus

VALUE_DECIMAL_PLACES = 3


class FileInfo(BaseModel):
    model_config = ConfigDict(
        from_attributes=True,
        json_schema_extra={
            "example": {
                "id": "ff33e3ec5cc1449aadd76fb403e232db",
                "filename": "plots.zip",
                "file_type": "shapefile",
                "status": "PENDING",
                "feature_count": 0,
                "crs": None,
                "created_at": "2026-10-08T05:42:20.773734Z",
                "error": None,
                "job_id": "9a1d0b6f4a7c4f0d8f5e2c7b1d3a6e90",
            }
        },
    )

    id: str
    filename: str
    file_type: FileType
    status: ProcessingStatus
    feature_count: int
    crs: str | None
    created_at: datetime
    error: str | None
    job_id: str | None


class MeasurementItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    feature_index: int
    geometry_type: str | None
    status: FeatureStatus
    measurement_type: MeasurementType | None
    value: float | None
    unit: str | None
    projected_crs: str | None
    error: str | None

    @field_serializer("value")
    def round_value(self, value: float | None) -> float | None:
        # Full precision is stored; rounding only happens when serialising the response.
        return None if value is None else round(value, VALUE_DECIMAL_PLACES)


class FeatureItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    feature_index: int
    geometry_type: str | None
    geometry: dict[str, Any] | None
    crs: str | None
    properties: dict[str, Any]


class Page(BaseModel):
    file_id: str
    total: int
    limit: int
    offset: int


class MeasurementsPage(Page):
    measurements: list[MeasurementItem]


class FeaturesPage(Page):
    features: list[FeatureItem]
