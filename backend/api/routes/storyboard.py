"""POST /api/storyboard

Splits a script into shots (or accepts an explicit shot list), then queues
one image-to-video ComfyUI job per shot through the single-slot worker so
only one generation ever runs at a time on the 8GB A750. Generated files are
downloaded from ComfyUI and saved under OUTPUT_DIR/<job_id>/shot_NNN/.

Shot state is persisted via backend/core/job_store.py at two points per
shot (submitted, then done/failed) -- not on every WebSocket progress tick,
see job_store.py's module docstring for why. _run_storyboard_job() always
reads its shot list from the store rather than always starting a blank list
at index 0, which is what makes it double as the resume path: a fresh job
just has no persisted shots yet (bootstrapped as PENDING here), while a job
resumed after a backend restart (see backend/core/recovery.py) has some
shots already DONE/SUBMITTED from before the restart. Same code, no
separate resume implementation to keep in sync.
"""

import asyncio
import json
import logging
import mimetypes
import time
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi import Path as FastAPIPath
from fastapi.responses import FileResponse, StreamingResponse

from backend.core.comfyui_client import ComfyUIClient, ComfyUIError
from backend.core.config import Settings, get_settings
from backend.core.events import EventBus, get_event_bus
from backend.core.job_store import JobStore, get_job_store
from backend.core.worker import Job, JobCancelled, JobStatus, LogLevel, LogStage, ShotStatus, worker
from backend.models.schemas import (
    ArtifactResponse,
    JobLogEntry,
    JobStatusResponse,
    ShotStatusResponse,
    StoryboardRequest,
    StoryboardResponse,
    StoryboardShot,
)

logger = logging.getLogger("manytv.api.storyboard")
router = APIRouter(prefix="/api", tags=["storyboard"])

settings = get_settings()
comfyui_client = ComfyUIClient(settings)


def _naive_shot_split(script: str) -> list[StoryboardShot]:
    lines = [line.strip() for line in script.splitlines() if line.strip()]
    return [StoryboardShot(index=i, description=line, prompt=line) for i, line in enumerate(lines)]


def _load_workflow(workflow_name: str, settings: Settings) -> dict[str, Any]:
    path = settings.workflow_path / f"{workflow_name}.json"
    if not path.exists():
        raise ComfyUIError(
            f"Workflow '{workflow_name}' not found at {path}. Export one from the ComfyUI UI "
            "(Dev Mode enabled > 'Save (API Format)') and drop the JSON there — "
            "see backend/workflows/README.md."
        )
    return json.loads(path.read_text(encoding="utf-8"))


def _apply_shot_to_workflow(workflow: dict[str, Any], shot: StoryboardShot) -> dict[str, Any]:
    """Fills CLIPTextEncode nodes titled "Positive"/"Negative" with the shot's prompt text.

    Adjust the title matching here if your exported workflow names its nodes differently.
    """
    patched = json.loads(json.dumps(workflow))  # deep copy
    for node in patched.values():
        if node.get("class_type") != "CLIPTextEncode":
            continue
        title = node.get("_meta", {}).get("title", "").strip().lower()
        if title == "positive":
            node["inputs"]["text"] = shot.prompt
        elif title == "negative" and shot.negative_prompt:
            # Only override if the shot actually specifies one -- otherwise
            # keep whatever default negative prompt the workflow JSON ships
            # with, rather than clobbering it with an empty string.
            node["inputs"]["text"] = shot.negative_prompt
    return patched


async def _save_history_outputs(client: ComfyUIClient, history: dict[str, Any], dest_dir: Path) -> list[str]:
    """Downloads every file ComfyUI produced for this prompt (images, gifs, videos, ...)."""
    saved: list[str] = []
    for node_output in history.get("outputs", {}).values():
        for entries in node_output.values():
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict) or "filename" not in entry:
                    continue
                data = await client.fetch_output_bytes(
                    entry["filename"], entry.get("subfolder", ""), entry.get("type", "output")
                )
                dest_dir.mkdir(parents=True, exist_ok=True)
                out_path = dest_dir / entry["filename"]
                out_path.write_bytes(data)
                saved.append(str(out_path))
    return saved


async def _run_storyboard_job(job: Job) -> dict[str, Any]:
    settings = get_settings()
    store = get_job_store()
    payload = job.payload
    shots_by_index = {s["index"]: StoryboardShot(**s) for s in payload["shots"]}
    workflow_template = _load_workflow(payload["workflow_name"], settings)

    if not await comfyui_client.health_check():
        raise ComfyUIError(
            f"ComfyUI is not reachable at {settings.comfyui_http_url}. "
            "Start it first with scripts/run_comfyui.ps1."
        )

    shot_rows = await store.get_shots(job.id)
    if not shot_rows:
        # Fresh job -- nothing persisted yet, so this *is* the initial state.
        for index in sorted(shots_by_index):
            await store.upsert_shot(job.id, index, ShotStatus.PENDING.value)
        shot_rows = await store.get_shots(job.id)

    results = []
    for shot_row in shot_rows:
        # Checked at the top of every iteration -- including ones about to
        # be DONE-skipped below -- so a /cancel request is noticed between
        # any two shots, not just before the next one that does real work.
        # Only takes effect between shots: a shot already SUBMITTED to
        # ComfyUI keeps running to completion (bounded by
        # GENERATION_TIMEOUT_SECONDS) since there's no ComfyUI /interrupt
        # call here -- see TODO.md for why that's a deliberate scope limit.
        current_job_row = await store.get_job(job.id)
        if current_job_row is not None and current_job_row["status"] == JobStatus.CANCELLING.value:
            raise JobCancelled(f"Job {job.id} cancelled during shot processing.")

        shot_index = shot_row["shot_index"]
        shot = shots_by_index[shot_index]

        if shot_row["status"] == ShotStatus.DONE.value:
            # Already completed in a prior process -- trust it rather than
            # re-checking ComfyUI, since a DONE row is only ever written
            # after its files are confirmed on disk (see job_store.py).
            files = json.loads(shot_row["files"]) if shot_row["files"] else []
            results.append({"shot_index": shot_index, "prompt_id": shot_row["prompt_id"], "files": files})
            continue

        prompt_id: Optional[str] = None
        history: Optional[dict[str, Any]] = None

        if shot_row["status"] == ShotStatus.SUBMITTED.value and shot_row["prompt_id"]:
            # Resumed mid-flight: this shot was queued to ComfyUI before the
            # backend last stopped, but its outcome was never confirmed.
            # ComfyUI keeps executing independently of this backend's
            # lifetime (see BUG-1), so check whether it actually finished
            # before assuming it's lost.
            try:
                history = await comfyui_client.get_history(shot_row["prompt_id"])
                prompt_id = shot_row["prompt_id"]
                logger.info(
                    "Job %s shot %d: found existing ComfyUI history for prompt %s, reusing it "
                    "instead of resubmitting.", job.id, shot_index, prompt_id,
                )
                await store.add_job_log(
                    job.id, LogLevel.INFO.value, LogStage.RESUME.value,
                    f"Job {job.id} shot {shot_index}: found existing ComfyUI history for prompt "
                    f"{prompt_id}, reusing it instead of resubmitting.",
                    shot_index=shot_index,
                )
            except ComfyUIError:
                # ComfyUI has no memory of this prompt (it was likely
                # restarted independently of the backend) -- safe to
                # resubmit fresh below. Anything other than ComfyUIError
                # here (e.g. ComfyUI simply unreachable right now) is left
                # to propagate: resubmitting without knowing the original
                # prompt's fate could silently produce a different result
                # than one the caller may already be relying on.
                logger.info(
                    "Job %s shot %d: prompt %s has no ComfyUI history; resubmitting.",
                    job.id, shot_index, shot_row["prompt_id"],
                )
                await store.add_job_log(
                    job.id, LogLevel.INFO.value, LogStage.RESUME.value,
                    f"Job {job.id} shot {shot_index}: prompt {shot_row['prompt_id']} has no ComfyUI "
                    "history; resubmitting.",
                    shot_index=shot_index,
                )

        if history is None:
            workflow = _apply_shot_to_workflow(workflow_template, shot)
            prompt_id = await comfyui_client.queue_prompt(workflow)
            await store.upsert_shot(
                job.id, shot_index, ShotStatus.SUBMITTED.value,
                prompt_id=prompt_id, submitted_at=time.time(),
            )
            logger.info("Job %s: shot %d queued as ComfyUI prompt %s", job.id, shot_index, prompt_id)
            await store.add_job_log(
                job.id, LogLevel.INFO.value, LogStage.SHOT.value,
                f"Job {job.id}: shot {shot_index} queued as ComfyUI prompt {prompt_id}",
                shot_index=shot_index,
            )

            # comfyui_client.py stays job-agnostic (it only ever sees a bare
            # prompt_id, never a job_id/shot_index or JobStore) -- so its
            # WS-drop-falls-back-to-polling warning can't persist itself.
            # on_progress relays it here as a synthetic message; _progress
            # stays synchronous (matching on_progress's existing contract,
            # unchanged) and only *captures* it plus the moment it actually
            # happened, deferring the actual async add_job_log call to the
            # finally block below, after wait_for_completion resolves.
            pending_log_events: list[tuple[str, str, str, float]] = []

            def _progress(message: dict[str, Any], shot_index: int = shot_index) -> None:
                if message.get("type") == "progress":
                    data = message["data"]
                    logger.info(
                        "Job %s shot %d: step %s/%s", job.id, shot_index, data.get("value"), data.get("max")
                    )
                elif message.get("type") == "ws_dropped_fallback_polling":
                    pending_log_events.append(
                        (LogLevel.WARNING.value, LogStage.COMFYUI.value, message["detail"], time.time())
                    )

            try:
                history = await asyncio.wait_for(
                    comfyui_client.wait_for_completion(prompt_id, on_progress=_progress),
                    timeout=settings.generation_timeout_seconds,
                )
            except asyncio.TimeoutError as exc:
                error_msg = (
                    f"Shot {shot_index} (prompt {prompt_id}) did not finish within "
                    f"{settings.generation_timeout_seconds}s."
                )
                await store.upsert_shot(job.id, shot_index, ShotStatus.FAILED.value, error=error_msg, finished_at=time.time())
                await store.add_job_log(
                    job.id, LogLevel.ERROR.value, LogStage.SHOT.value, error_msg, shot_index=shot_index,
                )
                raise ComfyUIError(error_msg) from exc
            except Exception as exc:
                await store.upsert_shot(job.id, shot_index, ShotStatus.FAILED.value, error=str(exc), finished_at=time.time())
                await store.add_job_log(
                    job.id, LogLevel.ERROR.value, LogStage.SHOT.value, str(exc), shot_index=shot_index,
                )
                raise
            finally:
                for level, stage, message, event_timestamp in pending_log_events:
                    await store.add_job_log(
                        job.id, level, stage, message, shot_index=shot_index, timestamp=event_timestamp,
                    )

        shot_dir = settings.output_path / job.id / f"shot_{shot_index:03d}"
        saved_files = await _save_history_outputs(comfyui_client, history, shot_dir)
        await store.upsert_shot(job.id, shot_index, ShotStatus.DONE.value, files=saved_files, finished_at=time.time())
        logger.info("Job %s shot %d done: %d file(s) saved to %s", job.id, shot_index, len(saved_files), shot_dir)
        await store.add_job_log(
            job.id, LogLevel.INFO.value, LogStage.SHOT.value,
            f"Job {job.id} shot {shot_index} done: {len(saved_files)} file(s) saved to {shot_dir}",
            shot_index=shot_index,
        )
        results.append({"shot_index": shot_index, "prompt_id": prompt_id, "files": saved_files})

    return {"shots": results}


worker.register_handler("storyboard", _run_storyboard_job)


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


def _artifact_from_path(raw_path: str) -> ArtifactResponse:
    """Derives display metadata for one persisted output file.

    Deliberately not persisted -- computed fresh from the raw path stored in
    shots.files every time a job's status is read (see TODO.md's Job Output
    Artifacts milestone). The try/except here is real, not reflexive: this is
    a confirmed single-user localhost deployment with unmediated filesystem
    access to output/, so a file can genuinely disappear between being
    recorded and being read on a later poll -- letting stat() raise would
    turn one deleted file into a permanently-broken job status response.
    """
    path = Path(raw_path)
    content_type, _ = mimetypes.guess_type(path.name)
    try:
        size_bytes: Optional[int] = path.stat().st_size
    except OSError:
        size_bytes = None
    return ArtifactResponse(filename=path.name, size_bytes=size_bytes, content_type=content_type)


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


# SSE keepalive interval -- sent as a comment line (ignored by EventSource,
# just keeps the connection from looking idle to any intermediary) whenever
# no real event arrives within this window. Also used as the wait_for
# timeout that lets each stream periodically recheck request.is_disconnected().
_SSE_KEEPALIVE_SECONDS = 15.0


def _sse_frame(event_type: str, data: str, event_id: Optional[str] = None) -> str:
    lines = []
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.append(f"event: {event_type}")
    lines.append(f"data: {data}")
    return "\n".join(lines) + "\n\n"


async def _all_jobs_events_stream(request: Request, bus: EventBus):
    """A signal-only channel, deliberately NOT pushing per-job payloads like
    the single-job streams do. A status-filtered dashboard view can both
    gain AND lose jobs as they change (e.g. a `?status=failed` view must
    drop a job the instant it's retried elsewhere) -- the frontend's
    existing upsert-by-id helper (updateJob) can only add/replace, never
    remove, so pushing individual job snapshots here can't correctly express
    a job leaving the filtered view. Signaling "something changed" and
    letting the client re-run its own already-correct, already-filtered
    GET /api/jobs fetch (exactly what polling already does every 3s, just
    now triggered instantly instead of on a timer) sidesteps that gap
    entirely with no new merge logic.
    """
    queue = bus.subscribe(None)
    try:
        yield "retry: 3000\n\n"
        yield _sse_frame("job_updated", "{}")
        while True:
            if await request.is_disconnected():
                break
            try:
                await asyncio.wait_for(queue.get(), timeout=_SSE_KEEPALIVE_SECONDS)
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
                continue
            yield _sse_frame("job_updated", "{}")
    finally:
        bus.unsubscribe(None, queue)


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


async def _job_events_stream(request: Request, store: JobStore, job_id: str, bus: EventBus):
    """Pushes the full JobStatusResponse (same shape GET /api/jobs/{id}
    already returns) every time this job changes. Subscribes BEFORE the
    first fetch -- deliberate: fetching first and subscribing after would
    leave a gap where a change could fire and be silently missed, possibly
    hanging the connection on a stale snapshot forever if that change was
    the job's last one.
    """
    queue = bus.subscribe(job_id)
    try:
        yield "retry: 3000\n\n"
        while True:
            response = await _build_job_status_response(store, job_id)
            yield _sse_frame("job_updated", response.model_dump_json())
            if await request.is_disconnected():
                break
            try:
                await asyncio.wait_for(queue.get(), timeout=_SSE_KEEPALIVE_SECONDS)
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
    finally:
        bus.unsubscribe(job_id, queue)


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


async def _job_logs_events_stream(request: Request, store: JobStore, job_id: str, bus: EventBus):
    """Pushes only new log rows (id greater than what's already been sent) --
    unlike job_updated's full-snapshot push, log entries are deltas, so
    resending the whole growing array on every wakeup would be wasteful and
    misleading to a client that appends rather than replaces. Honors
    Last-Event-ID on reconnect (job_logs.id is already a durable,
    monotonically-increasing column, so this survives a backend restart mid-
    job for free) -- without it, every reconnect would resend the entire
    backlog, visibly duplicating lines in a client that appends.
    """
    queue = bus.subscribe(job_id)
    last_id = int(request.headers.get("last-event-id") or 0)
    try:
        yield "retry: 3000\n\n"
        while True:
            rows = await store.get_job_logs(job_id)
            for row in rows:
                if row["id"] <= last_id:
                    continue
                last_id = row["id"]
                entry = JobLogEntry(
                    timestamp=row["timestamp"], level=row["level"], stage=row["stage"],
                    message=row["message"], shot_index=row["shot_index"],
                )
                yield _sse_frame("log_added", entry.model_dump_json(), event_id=str(row["id"]))
            if await request.is_disconnected():
                break
            try:
                await asyncio.wait_for(queue.get(), timeout=_SSE_KEEPALIVE_SECONDS)
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
    finally:
        bus.unsubscribe(job_id, queue)


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
        if job_row["kind"] == "generate_script":
            # A single `await llm_client.generate_script(...)` call has no
            # per-chunk checkpoint to hook a cooperative check into -- only
            # a still-QUEUED generate_script job can be cancelled.
            raise HTTPException(
                status_code=409,
                detail="generate_script jobs cannot be cancelled once running (no per-chunk checkpoint) -- only while queued.",
            )
        await store.update_job_status(job_id, JobStatus.CANCELLING.value)
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
