from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, field_serializer

from app.db.models import FeatureStatus, FileStatus, FileType, MeasurementType

VALUE_DECIMAL_PLACES = 3


class FileInfo(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    filename: str
    file_type: FileType
    status: FileStatus
    feature_count: int
    crs: str | None
    created_at: datetime
    error: str | None


class FeatureMeasurementResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    feature_index: int
    geometry_type: str | None
    geometry: dict[str, Any] | None
    crs: str | None
    properties: dict[str, Any]
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


class MeasurementsResponse(BaseModel):
    file_id: str
    measurements: list[FeatureMeasurementResponse]
