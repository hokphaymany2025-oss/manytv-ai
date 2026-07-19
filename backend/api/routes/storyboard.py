"""POST /api/storyboard and the job-management/SSE routes.

Route handlers only (v1.2 Phase 3 split) -- the actual work lives in:
- backend/core/storyboard_engine.py -- shot-splitting, workflow templating,
  and _run_storyboard_job (the job-execution engine registered with the
  worker).
- backend/core/artifacts.py -- per-artifact display metadata.
- backend/api/job_responses.py -- builds JobStatusResponse from persisted
  state (shared by most routes below and by the single-job SSE stream).
- backend/api/sse.py -- the SSE stream generator functions.

Splitting this file previously (all four in one 653-line module) is what
made this the largest, most load-bearing file in the codebase -- see
TODO.md's v1.2 Phase 3 for the rationale.
"""

import json
import logging
import mimetypes
import time
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi import Path as FastAPIPath
from fastapi.responses import FileResponse, StreamingResponse

from backend.api.job_responses import _build_job_status_response
from backend.api.sse import _all_jobs_events_stream, _job_events_stream, _job_logs_events_stream
from backend.core.config import get_settings
from backend.core.events import get_event_bus
from backend.core.job_store import get_job_store
from backend.core.storyboard_engine import _naive_shot_split, request_shot_interrupt
from backend.core.worker import Job, JobStatus, LogLevel, LogStage, ShotStatus, worker
from backend.models.schemas import (
    JobAttemptEntry,
    JobLogEntry,
    JobStatusResponse,
    StoryboardRequest,
    StoryboardResponse,
)

logger = logging.getLogger("manytv.api.storyboard")
router = APIRouter(prefix="/api", tags=["storyboard"])


@router.post("/storyboard", response_model=StoryboardResponse)
async def create_storyboard(request: StoryboardRequest) -> StoryboardResponse:
    settings = get_settings()
    shots = request.shots or _naive_shot_split(request.script)
    if not shots:
        raise HTTPException(status_code=400, detail="Script produced no shots to storyboard.")
    if len(shots) > settings.max_shots_per_job:
        raise HTTPException(
            status_code=400,
            detail=f"{len(shots)} shots exceeds MAX_SHOTS_PER_JOB ({settings.max_shots_per_job}).",
        )

    job = await worker.submit(
        "storyboard",
        {"shots": [s.model_dump() for s in shots], "workflow_name": request.workflow_name},
        project_id=request.project_id,
        workflow_name=request.workflow_name,
    )
    return StoryboardResponse(job_id=job.id, status=job.status.value, shot_count=len(shots))


# Registered BEFORE GET /jobs/{job_id} below, deliberately: FastAPI/Starlette
# matches routes in registration order, and both routes have exactly two
# path segments ("jobs" + one more) -- if /jobs/{job_id} were registered
# first, a request to /jobs/events would match it with job_id="events" and
# 404, never reaching this route at all. Confirmed the hard way live (curl
# against a real running backend) before this comment was written.
@router.get("/jobs/events")
async def all_jobs_events(request: Request) -> StreamingResponse:
    return StreamingResponse(_all_jobs_events_stream(request, get_event_bus()), media_type="text/event-stream")


@router.get("/jobs/{job_id}", response_model=JobStatusResponse)
async def get_job_status(job_id: str) -> JobStatusResponse:
    return await _build_job_status_response(get_job_store(), job_id)


@router.get("/jobs/{job_id}/events")
async def job_events(request: Request, job_id: str) -> StreamingResponse:
    store = get_job_store()
    if await store.get_job(job_id) is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    return StreamingResponse(
        _job_events_stream(request, store, job_id, get_event_bus()), media_type="text/event-stream",
    )


@router.get("/jobs/{job_id}/logs", response_model=list[JobLogEntry])
async def get_job_logs(job_id: str) -> list[JobLogEntry]:
    """Curated execution-log lines for a job -- a separate endpoint (not a
    field on JobStatusResponse) since GET /api/jobs (the dashboard list)
    reuses _build_job_status_response for every job in view and has no use
    for full log history on every 3s poll; only the detail page needs this.
    """
    store = get_job_store()
    if await store.get_job(job_id) is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    rows = await store.get_job_logs(job_id)
    return [
        JobLogEntry(
            timestamp=r["timestamp"], level=r["level"], stage=r["stage"],
            message=r["message"], shot_index=r["shot_index"],
        )
        for r in rows
    ]


@router.get("/jobs/{job_id}/attempts", response_model=list[JobAttemptEntry])
async def get_job_attempts(job_id: str) -> list[JobAttemptEntry]:
    """Past attempts a /retry overwrote (see reset_job_for_retry) -- empty
    for a job that's never been retried. Oldest first, same convention as
    get_job_logs.
    """
    store = get_job_store()
    if await store.get_job(job_id) is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    rows = await store.get_job_attempts(job_id)
    return [
        JobAttemptEntry(
            status=r["status"], error=r["error"], started_at=r["started_at"],
            finished_at=r["finished_at"], recorded_at=r["recorded_at"],
        )
        for r in rows
    ]


@router.get("/jobs/{job_id}/logs/events")
async def job_logs_events(request: Request, job_id: str) -> StreamingResponse:
    store = get_job_store()
    if await store.get_job(job_id) is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    return StreamingResponse(
        _job_logs_events_stream(request, store, job_id, get_event_bus()), media_type="text/event-stream",
    )


@router.get("/jobs", response_model=list[JobStatusResponse])
async def list_jobs_route(status: Optional[str] = None) -> list[JobStatusResponse]:
    """Lists jobs, newest first. `status` is an optional comma-separated
    filter (e.g. `?status=failed,cancelled`); omitted means every job --
    this is the full-history endpoint the frontend's client-tracked
    localStorage job list was standing in for until now (see TODO.md).
    """
    store = get_job_store()
    status_in = [s.strip() for s in status.split(",") if s.strip()] if status else None
    rows = await store.list_jobs(status_in=status_in)
    return [await _build_job_status_response(store, row["id"], job_row=row) for row in rows]


_RETRYABLE_STATUSES = {JobStatus.FAILED.value, JobStatus.CANCELLED.value}


@router.post("/jobs/{job_id}/retry", response_model=JobStatusResponse)
async def retry_job(job_id: str) -> JobStatusResponse:
    store = get_job_store()
    job_row = await store.get_job(job_id)
    if job_row is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    if job_row["status"] not in _RETRYABLE_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=f"Job is '{job_row['status']}'; only failed or cancelled jobs can be retried.",
        )
    if job_row["started_at"] is None:
        # A job cancelled while still QUEUED never created any shot rows and
        # its original queue entry was never consumed -- there is zero
        # partial progress to resume from, and resubmitting it here would
        # create a second queue entry for the same job id racing against
        # whichever the worker pops first. Nothing is lost by rejecting this
        # and asking for a fresh submission instead.
        raise HTTPException(
            status_code=409,
            detail="Job never started running (cancelled while queued) -- nothing to resume; submit a new job instead.",
        )

    await store.reset_job_for_retry(job_id, JobStatus.QUEUED.value)
    if job_row["kind"] == "storyboard":
        # Only rows truly FAILED are reset -- SUBMITTED rows deliberately
        # keep going through _run_storyboard_job's existing history-
        # reconcile-or-resubmit branch untouched, and DONE rows are already
        # correct.
        await store.reset_shots_by_status(job_id, ShotStatus.FAILED.value, ShotStatus.PENDING.value)

    updated_row = await store.get_job(job_id)
    job = Job(
        id=updated_row["id"],
        kind=updated_row["kind"],
        payload=json.loads(updated_row["payload"]),
        status=JobStatus(updated_row["status"]),
        created_at=updated_row["created_at"],
    )
    await worker.resubmit(job)
    logger.info("Job %s retried.", job_id)
    await store.add_job_log(job_id, LogLevel.INFO.value, LogStage.JOB.value, f"Job {job_id} retried.")
    return await _build_job_status_response(store, job_id)


@router.post("/jobs/{job_id}/cancel", response_model=JobStatusResponse)
async def cancel_job(job_id: str) -> JobStatusResponse:
    store = get_job_store()
    job_row = await store.get_job(job_id)
    if job_row is None:
        raise HTTPException(status_code=404, detail="Job not found.")

    status = job_row["status"]
    if status in (JobStatus.CANCELLED.value, JobStatus.CANCELLING.value):
        pass  # already cancelled or on its way there -- idempotent no-op
    elif status == JobStatus.QUEUED.value:
        await store.update_job_status(job_id, JobStatus.CANCELLED.value, finished_at=time.time())
    elif status in (JobStatus.RUNNING.value, JobStatus.RESUMING.value):
        # Sets CANCELLING first, unconditionally, for both kinds -- if the
        # job is still sitting in the queue (a RESUMING row not yet
        # re-dequeued), the worker's own dequeue-time check already
        # finalizes a CANCELLING row to CANCELLED without ever running the
        # handler, no further action needed. The calls below are the
        # *immediate* path, for a job already actually executing right now:
        # storyboard has its own per-shot cooperative check plus ComfyUI's
        # own /interrupt (request_shot_interrupt); generate_script's single
        # blocking LLM call has no checkpoint of its own to notice
        # CANCELLING, so cancel_current_job() cancels its asyncio Task
        # directly instead.
        await store.update_job_status(job_id, JobStatus.CANCELLING.value)
        if job_row["kind"] == "storyboard":
            await request_shot_interrupt(job_id)
        elif job_row["kind"] == "generate_script":
            worker.cancel_current_job(job_id)
    else:  # DONE, FAILED
        raise HTTPException(status_code=409, detail=f"Job is '{status}'; nothing to cancel.")

    logger.info("Job %s cancel requested (was %s).", job_id, status)
    await store.add_job_log(
        job_id, LogLevel.INFO.value, LogStage.JOB.value, f"Job {job_id} cancel requested (was {status})."
    )
    return await _build_job_status_response(store, job_id)


@router.get("/jobs/{job_id}/files/{shot_index}/{filename}")
async def download_shot_file(
    job_id: str = FastAPIPath(..., pattern=r"^[A-Za-z0-9_-]+$"),
    shot_index: int = FastAPIPath(...),
    filename: str = FastAPIPath(..., pattern=r"^[A-Za-z0-9_.-]+$"),
) -> FileResponse:
    # Both patterns exclude path separators entirely, so joining them onto
    # a fixed directory below can't escape it by construction -- same
    # reasoning as the workflow_name fix for BUG-2 in models/schemas.py.
    # Deliberately not a blanket StaticFiles mount over the whole output/
    # dir: Settings.db_path defaults to output/jobs.db, which would then be
    # fetchable by anyone who can reach this origin.
    settings = get_settings()
    path = settings.output_path / job_id / f"shot_{shot_index:03d}" / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="File not found.")
    return FileResponse(path, media_type=mimetypes.guess_type(filename)[0])
