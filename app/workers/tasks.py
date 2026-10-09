from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.database import get_engine
from app.services.jobs import run_processing_job


def process_file_job(job_id: str) -> None:
    """Entry point executed by the queue worker."""
    with Session(get_engine(), expire_on_commit=False) as session:
        run_processing_job(session, get_settings(), job_id)
