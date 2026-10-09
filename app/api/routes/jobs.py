from fastapi import APIRouter, HTTPException, status

from app.api.dependencies import SessionDep
from app.schemas.jobs import JobInfo
from app.services import files as file_service

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


@router.get(
    "/{job_id}/",
    response_model=JobInfo,
    responses={status.HTTP_404_NOT_FOUND: {"description": "No job with this id."}},
    summary="Processing job status",
)
def get_job(job_id: str, session: SessionDep) -> JobInfo:
    """State and timings of a processing job, including the failure reason when it failed."""
    job = file_service.get_job(session, job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Job not found.")
    return JobInfo.model_validate(job)
