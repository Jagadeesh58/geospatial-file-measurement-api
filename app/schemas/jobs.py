from datetime import datetime

from pydantic import BaseModel, ConfigDict, computed_field

from app.db.models import ProcessingStatus

DURATION_DECIMAL_PLACES = 3


class JobInfo(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    file_id: str
    status: ProcessingStatus
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    error: str | None
    attempt_count: int

    @computed_field  # type: ignore[prop-decorator]
    @property
    def duration_seconds(self) -> float | None:
        if self.started_at is None or self.finished_at is None:
            return None
        return round((self.finished_at - self.started_at).total_seconds(), DURATION_DECIMAL_PLACES)
