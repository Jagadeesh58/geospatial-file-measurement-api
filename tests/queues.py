from sqlalchemy import Engine
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.services.jobs import run_processing_job


class InlineJobQueue:
    """Runs jobs in-process, either at once or when a test calls `run_pending`."""

    def __init__(self, engine: Engine, settings: Settings) -> None:
        self._engine = engine
        self._settings = settings
        self.run_immediately = True
        self.pending: list[str] = []

    def enqueue(self, job_id: str) -> None:
        self.pending.append(job_id)
        if self.run_immediately:
            self.run_pending()

    def run_pending(self) -> None:
        job_ids, self.pending = self.pending, []
        for job_id in job_ids:
            with Session(self._engine, expire_on_commit=False) as session:
                run_processing_job(session, self._settings, job_id)

    def ping(self) -> bool:
        return True
