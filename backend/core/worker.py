"""Single-slot background worker.

ComfyUI's own queue would happily accept concurrent prompts, but two
simultaneous video-generation jobs will not fit in an 8GB Arc A750, so
concurrency is capped at exactly one job here: a single asyncio task pulls
from a FIFO queue, runs one job to completion (success or failure), releases
GPU memory, then moves to the next.

Both job kinds registered on this worker -- "storyboard" (ComfyUI) and
"generate_script" (Ollama) -- share this one slot. That's not just about
serializing ComfyUI jobs against each other: Ollama loads its model onto
the same discrete GPU as ComfyUI on this hardware (confirmed via
`ollama ps`), so an LLM call and a video generation running concurrently
would compete for the same 8GB VRAM budget.

Job/shot state is persisted via backend/core/job_store.py at every
transition below, so it survives a backend restart -- see
backend/core/recovery.py for how a previous process's incomplete jobs get
picked back up at startup. This module stays kind-agnostic and knows
nothing about ComfyUI, shots, or workflows; persisted state is the source
of truth for *querying* a job (see GET /api/jobs/{id} in
backend/api/routes/storyboard.py), so this class only tracks what it needs
to actually run the queue, not a separate in-memory job index.
"""

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Optional

from backend.core.gpu_memory import release_gpu_memory
from backend.core.job_store import JobStore, get_job_store

logger = logging.getLogger("manytv.worker")


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    # Set the instant a job that was RUNNING when the backend last stopped is
    # picked back up at startup, before it's confirmed safely re-queued --
    # see backend/core/recovery.py. Distinct from RUNNING/QUEUED so a poller
    # gets an honest "known, being worked on, possibly waiting on ComfyUI"
    # signal rather than either of those slightly misleading states.
    RESUMING = "resuming"
    # A cancel request has been recorded against a RUNNING/RESUMING job but
    # not yet honored -- the job's own handler notices this cooperatively
    # (see _run_storyboard_job's per-shot check) and raises JobCancelled,
    # which finalizes it to CANCELLED below. Purely a signal; nothing reads
    # it as "the job is stopped" except the transition itself.
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    DONE = "done"
    FAILED = "failed"


class ShotStatus(str, Enum):
    PENDING = "pending"      # not yet queue_prompt()'d
    SUBMITTED = "submitted"  # queue_prompt() succeeded, prompt_id recorded,
                              # outcome not yet confirmed -- the in-flight state
    DONE = "done"            # history retrieved, files on disk
    FAILED = "failed"        # execution_error, timeout, or download failure


class LogLevel(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class LogStage(str, Enum):
    JOB = "job"          # job-level lifecycle: started, done, failed, cancelled, retried
    SHOT = "shot"        # per-shot: queued, done, failed
    RESUME = "resume"    # startup reconciliation after a backend restart
    COMFYUI = "comfyui"  # ComfyUI-connection-level events (currently: the WS-drop fallback)


class JobCancelled(Exception):
    """Raised from inside a job handler's cooperative cancellation check
    (see _run_storyboard_job) once it observes the job's persisted status is
    CANCELLING. Caught in SingleSlotWorker._run() ahead of the generic
    except Exception below, so a cancelled job finalizes as CANCELLED rather
    than being misreported as FAILED.
    """


@dataclass
class Job:
    id: str
    kind: str
    payload: dict[str, Any]
    status: JobStatus = JobStatus.QUEUED
    result: Optional[dict[str, Any]] = None
    error: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None


JobHandler = Callable[[Job], Awaitable[dict[str, Any]]]


class SingleSlotWorker:
    """FIFO queue with exactly one concurrent worker task."""

    def __init__(self, store: Optional[JobStore] = None) -> None:
        self._queue: asyncio.Queue[Job] = asyncio.Queue()
        self._handlers: dict[str, JobHandler] = {}
        self._task: Optional[asyncio.Task] = None
        self._store = store or get_job_store()
        # Tracks whichever job/task _run() is currently awaiting, so
        # cancel_current_job() below can reach it directly -- the only
        # mechanism available for a job kind with no cooperative checkpoint
        # of its own inside its handler (generate_script's single blocking
        # LLM call; storyboard has its own per-shot check plus ComfyUI's own
        # /interrupt, see storyboard_engine.py).
        self._current_job: Optional[Job] = None
        self._current_job_task: Optional[asyncio.Task] = None
        # Set by cancel_current_job() just before it cancels the inner task,
        # so _run()'s except block can tell "this was our own targeted
        # per-job cancel" apart from "the outer _run() task itself is being
        # cancelled (worker.stop())". task.cancelled() alone can't
        # distinguish these: asyncio.Task.cancel() automatically propagates
        # to whatever a task is currently suspended on (its _fut_waiter) --
        # since _run() awaits the inner task directly, cancelling the OUTER
        # task also cancels the inner one as a side effect, making both
        # scenarios look identical from task.cancelled() alone. Confirmed
        # the hard way: an earlier version of this relying on
        # task.cancelled() made worker.stop() hang forever, since it
        # mistook its own shutdown for a job-level cancel and looped back to
        # an empty queue instead of re-raising.
        self._cancel_requested_for: Optional[str] = None

    def register_handler(self, kind: str, handler: JobHandler) -> None:
        self._handlers[kind] = handler

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="manytv-worker-loop")
            logger.info("Worker loop started (max concurrency = 1).")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def submit(
        self,
        kind: str,
        payload: dict[str, Any],
        project_id: Optional[str] = None,
        workflow_name: Optional[str] = None,
    ) -> Job:
        job = Job(id=str(uuid.uuid4()), kind=kind, payload=payload)
        await self._store.create_job(
            job.id, kind, job.status.value, payload,
            project_id=project_id, workflow_name=workflow_name, created_at=job.created_at,
        )
        await self._queue.put(job)
        logger.info("Queued job %s (%s); %d job(s) ahead of it.", job.id, kind, self._queue.qsize() - 1)
        return job

    async def resubmit(self, job: Job) -> None:
        """Re-enqueues a Job reconstructed from a persisted row.

        Used by backend/core/recovery.py at startup -- unlike submit(), the
        DB row and job id already exist, so this only enqueues.
        """
        await self._queue.put(job)
        logger.info("Resumed job %s (%s); %d job(s) ahead of it.", job.id, job.kind, self._queue.qsize() - 1)

    async def _run(self) -> None:
        while True:
            job = await self._queue.get()

            # A job can sit in this queue for a while (behind other work, or
            # -- for a RESUMING job -- across the indefinite ComfyUI-health
            # wait in recovery.py) with a cancel request recorded against it
            # in the meantime. Check the persisted row before touching
            # anything: without this, the unconditional `status = RUNNING`
            # write a few lines below would silently clobber a CANCELLING
            # signal, and the handler would run to completion anyway.
            row = await self._store.get_job(job.id)
            if row is not None and row["status"] in (JobStatus.CANCELLED.value, JobStatus.CANCELLING.value):
                if row["status"] == JobStatus.CANCELLING.value:
                    await self._store.update_job_status(
                        job.id, JobStatus.CANCELLED.value, finished_at=time.time(),
                    )
                logger.info("Job %s was cancelled before it started running; skipping.", job.id)
                await self._store.add_job_log(
                    job.id, LogLevel.INFO.value, LogStage.JOB.value,
                    f"Job {job.id} was cancelled before it started running; skipping.",
                )
                self._queue.task_done()
                continue

            job.status = JobStatus.RUNNING
            job.started_at = time.time()
            logger.info("Starting job %s (%s).", job.id, job.kind)
            await self._store.update_job_status(job.id, JobStatus.RUNNING.value, started_at=job.started_at)
            await self._store.add_job_log(
                job.id, LogLevel.INFO.value, LogStage.JOB.value, f"Starting job {job.id} ({job.kind})."
            )
            self._current_job = job
            task: Optional[asyncio.Task] = None
            try:
                handler = self._handlers.get(job.kind)
                if handler is None:
                    raise RuntimeError(f"No handler registered for job kind '{job.kind}'")
                # Run the handler as its own Task (not a bare await) so
                # cancel_current_job() can target *just this job's*
                # execution -- needed for a kind like generate_script, whose
                # handler is one uninterruptible blocking LLM call with no
                # cooperative checkpoint of its own to notice a CANCELLING
                # signal (unlike storyboard, which checks between shots).
                task = asyncio.ensure_future(handler(job))
                self._current_job_task = task
                job.result = await task
                job.status = JobStatus.DONE
                elapsed = time.time() - job.started_at
                logger.info("Job %s completed in %.1fs.", job.id, elapsed)
                await self._store.add_job_log(
                    job.id, LogLevel.INFO.value, LogStage.JOB.value,
                    f"Job {job.id} completed in {elapsed:.1f}s.",
                )
            except asyncio.CancelledError:
                if self._cancel_requested_for == job.id:
                    # Our own targeted cancel_current_job() call for this
                    # exact job -- not this loop's own outer task
                    # (self._task, cancelled by stop()). Finalize like
                    # JobCancelled below and keep the loop running.
                    job.status = JobStatus.CANCELLED
                    logger.info("Job %s cancelled.", job.id)
                    await self._store.add_job_log(
                        job.id, LogLevel.INFO.value, LogStage.JOB.value, f"Job {job.id} cancelled."
                    )
                else:
                    # This loop's own task is being cancelled (worker.stop()).
                    # asyncio.Task.cancel() already propagates to whatever a
                    # task is currently suspended on (its _fut_waiter, here
                    # the inner `task`), so the inner task is very likely
                    # already cancelled too by this point -- calling
                    # task.cancel() again is a harmless no-op if so, and a
                    # real safety net if for some reason it wasn't.
                    if task is not None:
                        task.cancel()
                    raise
            except JobCancelled:
                job.status = JobStatus.CANCELLED
                logger.info("Job %s cancelled.", job.id)
                await self._store.add_job_log(
                    job.id, LogLevel.INFO.value, LogStage.JOB.value, f"Job {job.id} cancelled."
                )
            except Exception as exc:
                job.status = JobStatus.FAILED
                job.error = str(exc)
                logger.exception("Job %s failed.", job.id)
                await self._store.add_job_log(
                    job.id, LogLevel.ERROR.value, LogStage.JOB.value, f"Job {job.id} failed: {job.error}"
                )
            finally:
                self._current_job = None
                self._current_job_task = None
                self._cancel_requested_for = None
                job.finished_at = time.time()
                await self._store.update_job_status(
                    job.id, job.status.value, finished_at=job.finished_at,
                    result=job.result, error=job.error,
                )
                release_gpu_memory()
                self._queue.task_done()

    def cancel_current_job(self, job_id: str) -> bool:
        """Best-effort, immediate cancellation for whichever job is actually
        executing right now, if its id matches -- the mechanism for a job
        kind with no cooperative checkpoint of its own inside its handler
        (today: generate_script). Cancels the asyncio Task wrapping the
        handler call directly, rather than relying on the handler to notice
        anything itself.

        Returns True only if job_id was genuinely the job executing right
        now and its task was cancelled; False otherwise (already finished,
        a different job is running, or nothing is running at all) -- the
        caller should treat False as "nothing to cancel this way", not as a
        silent success.
        """
        if (
            self._current_job is not None
            and self._current_job.id == job_id
            and self._current_job_task is not None
            and not self._current_job_task.done()
        ):
            self._cancel_requested_for = job_id
            self._current_job_task.cancel()
            return True
        return False


worker = SingleSlotWorker()
