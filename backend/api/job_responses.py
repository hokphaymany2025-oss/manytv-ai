"""Builds JobStatusResponse from persisted job/shot state.

Split out of backend/api/routes/storyboard.py (v1.2 Phase 3) into its own
module specifically to avoid a circular import: backend/api/routes/storyboard.py
needs _job_events_stream from backend/api/sse.py, and _job_events_stream needs
_build_job_status_response -- keeping the latter in the routes file would
make storyboard.py and sse.py import each other.
"""

import json
from typing import Any, Optional

from fastapi import HTTPException

from backend.core.artifacts import _artifact_from_path
from backend.core.job_store import JobStore
from backend.core.worker import ShotStatus
from backend.models.schemas import JobStatusResponse, ShotStatusResponse


async def _build_job_status_response(
    store: JobStore, job_id: str, job_row: Optional[dict[str, Any]] = None,
) -> JobStatusResponse:
    """Builds JobStatusResponse from persisted state.

    Factored out of get_job_status so retry_job/cancel_job can return the
    same shape after mutating job/shot state, without re-deriving this
    shots/result-construction logic a second and third time. list_jobs_route
    passes an already-fetched `job_row` (from its own list_jobs() query) to
    avoid an N+1 re-fetch per job in the list; every other caller omits it
    and gets the existing fetch-by-id-or-404 behavior.
    """
    if job_row is None:
        job_row = await store.get_job(job_id)
        if job_row is None:
            raise HTTPException(status_code=404, detail="Job not found.")

    shots_response: Optional[list[ShotStatusResponse]] = None
    result: Optional[dict[str, Any]] = None

    if job_row["kind"] == "storyboard":
        shot_rows = await store.get_shots(job_id)
        shots_response = [
            ShotStatusResponse(
                shot_index=r["shot_index"],
                status=r["status"],
                prompt_id=r["prompt_id"],
                files=[_artifact_from_path(f) for f in json.loads(r["files"])] if r["files"] else None,
                error=r["error"],
                created_at=r["created_at"],
                submitted_at=r["submitted_at"],
                finished_at=r["finished_at"],
            )
            for r in shot_rows
        ]
        # Built from shot rows at read time rather than the job's own
        # `result` column, so this stays a single source of truth and shows
        # partial progress (BUG-4) even while the job is still RUNNING, not
        # only once it reaches a terminal state.
        done_shots = [s for s in shots_response if s.status == ShotStatus.DONE.value]
        if done_shots:
            result = {
                "shots": [
                    {
                        "shot_index": s.shot_index,
                        "prompt_id": s.prompt_id,
                        "files": [f.model_dump() for f in (s.files or [])],
                    }
                    for s in done_shots
                ]
            }
    else:
        result = json.loads(job_row["result"]) if job_row["result"] else None

    return JobStatusResponse(
        id=job_row["id"],
        kind=job_row["kind"],
        status=job_row["status"],
        project_id=job_row["project_id"],
        workflow_name=job_row["workflow_name"],
        result=result,
        error=job_row["error"],
        shots=shots_response,
        created_at=job_row["created_at"],
        started_at=job_row["started_at"],
        finished_at=job_row["finished_at"],
    )
