"""Startup reconciliation for jobs left incomplete by a previous process.

If the backend is killed mid-job (crash, --reload picking up a change, a
deliberate restart), ComfyUI keeps running independently -- the persisted
job/shot state in backend/core/job_store.py is the only record of what was
happening. This module runs once at startup, right after
SingleSlotWorker.start(), and re-enqueues anything left non-terminal so it
picks back up instead of vanishing silently.

Deliberately does not block FastAPI's own startup/readiness on ComfyUI
being reachable -- matches app.py's existing lifespan behavior (a down
ComfyUI today just logs a warning, doesn't fail startup) and the real
workflow, where starting ComfyUI is a separate manual step
(scripts/run_comfyui.ps1) done after the backend. Jobs needing ComfyUI
reconciliation are resumed from a background task that waits for it,
however long that takes; GET /health stays 200 the whole time regardless.
"""

import asyncio
import json
import logging
import time

from backend.core.comfyui_client import ComfyUIClient
from backend.core.job_store import JobStore
from backend.core.worker import Job, JobStatus, SingleSlotWorker

logger = logging.getLogger("manytv.recovery")

_COMFYUI_HEALTH_RETRY_SECONDS = 5.0


async def resume_incomplete_jobs(worker: SingleSlotWorker, store: JobStore, comfyui_client: ComfyUIClient) -> None:
    rows = await store.list_jobs(status_in=[
        JobStatus.QUEUED.value, JobStatus.RUNNING.value, JobStatus.CANCELLING.value,
    ])
    if not rows:
        return

    # A CANCELLING row here means a cancel was requested but the backend
    # crashed before the running handler's cooperative check ever noticed --
    # still a clear, stale request to stop the job, honored directly rather
    # than ever resubmitting it. Handled kind-agnostically and ahead of the
    # kind-based split below, even though in practice only a storyboard row
    # can currently reach CANCELLING (a running generate_script job can't be
    # cancelled at all -- see cancel_job in storyboard.py).
    cancelling_rows = [r for r in rows if r["status"] == JobStatus.CANCELLING.value]
    for row in cancelling_rows:
        await store.update_job_status(row["id"], JobStatus.CANCELLED.value, finished_at=time.time())
        logger.info("Job %s was CANCELLING when the backend last stopped; finalized as CANCELLED.", row["id"])

    live_rows = [r for r in rows if r["status"] != JobStatus.CANCELLING.value]
    if not live_rows:
        return

    logger.info("Found %d incomplete job(s) from a previous run; resuming.", len(live_rows))

    # generate_script has no ComfyUI-style history to reconcile against (an
    # LLM completion isn't resumable/idempotent the way a ComfyUI prompt_id
    # is) -- "resume" for that kind always means "re-run from scratch," and
    # it doesn't need ComfyUI at all, so it can go straight back on the
    # queue without waiting on anything.
    non_comfyui_rows = [r for r in live_rows if r["kind"] != "storyboard"]
    comfyui_rows = [r for r in live_rows if r["kind"] == "storyboard"]

    for row in non_comfyui_rows:
        await _resubmit_row(worker, store, row)

    if comfyui_rows:
        asyncio.create_task(
            _resume_comfyui_jobs(worker, store, comfyui_client, comfyui_rows),
            name="manytv-resume-comfyui-jobs",
        )


async def _resume_comfyui_jobs(
    worker: SingleSlotWorker,
    store: JobStore,
    comfyui_client: ComfyUIClient,
    rows: list[dict],
    poll_interval_seconds: float = _COMFYUI_HEALTH_RETRY_SECONDS,
) -> None:
    if not await comfyui_client.health_check():
        logger.warning(
            "ComfyUI not reachable yet -- %d storyboard job(s) will wait for it before resuming "
            "(checking every %.0fs; start it with scripts/run_comfyui.ps1).",
            len(rows), poll_interval_seconds,
        )
        while not await comfyui_client.health_check():
            await asyncio.sleep(poll_interval_seconds)
        logger.info("ComfyUI is reachable again; resuming %d storyboard job(s).", len(rows))

    for stale_row in rows:
        # `rows` was captured before the (possibly long, possibly
        # indefinite) health-check wait above -- a /cancel request could
        # have landed on this job any time during that wait, so re-fetch
        # immediately before acting on it rather than trusting the snapshot.
        fresh_row = await store.get_job(stale_row["id"])
        if fresh_row is None:
            continue
        if fresh_row["status"] in (JobStatus.CANCELLED.value, JobStatus.CANCELLING.value):
            if fresh_row["status"] == JobStatus.CANCELLING.value:
                await store.update_job_status(fresh_row["id"], JobStatus.CANCELLED.value, finished_at=time.time())
            logger.info("Job %s was cancelled while waiting for ComfyUI; not resuming.", fresh_row["id"])
            continue
        await _resubmit_row(worker, store, fresh_row)


async def _resubmit_row(worker: SingleSlotWorker, store: JobStore, row: dict) -> None:
    job = Job(
        id=row["id"],
        kind=row["kind"],
        payload=json.loads(row["payload"]),
        status=JobStatus(row["status"]),
        error=row["error"],
        created_at=row["created_at"],
        started_at=row["started_at"],
    )
    if job.status == JobStatus.RUNNING:
        job.status = JobStatus.RESUMING
        await store.update_job_status(job.id, JobStatus.RESUMING.value)
        logger.info("Job %s (%s) was mid-flight when the backend last stopped; marked RESUMING.", job.id, job.kind)
    await worker.resubmit(job)
