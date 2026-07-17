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
"""

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Optional

from backend.core.gpu_memory import release_gpu_memory

logger = logging.getLogger("manytv.worker")


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


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

    def __init__(self) -> None:
        self._queue: asyncio.Queue[Job] = asyncio.Queue()
        self._jobs: dict[str, Job] = {}
        self._handlers: dict[str, JobHandler] = {}
        self._task: Optional[asyncio.Task] = None

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

    async def submit(self, kind: str, payload: dict[str, Any]) -> Job:
        job = Job(id=str(uuid.uuid4()), kind=kind, payload=payload)
        self._jobs[job.id] = job
        await self._queue.put(job)
        logger.info("Queued job %s (%s); %d job(s) ahead of it.", job.id, kind, self._queue.qsize() - 1)
        return job

    def get_job(self, job_id: str) -> Optional[Job]:
        return self._jobs.get(job_id)

    async def _run(self) -> None:
        while True:
            job = await self._queue.get()
            job.status = JobStatus.RUNNING
            job.started_at = time.time()
            logger.info("Starting job %s (%s).", job.id, job.kind)
            try:
                handler = self._handlers.get(job.kind)
                if handler is None:
                    raise RuntimeError(f"No handler registered for job kind '{job.kind}'")
                job.result = await handler(job)
                job.status = JobStatus.DONE
                elapsed = time.time() - job.started_at
                logger.info("Job %s completed in %.1fs.", job.id, elapsed)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                job.status = JobStatus.FAILED
                job.error = str(exc)
                logger.exception("Job %s failed.", job.id)
            finally:
                job.finished_at = time.time()
                release_gpu_memory()
                self._queue.task_done()


worker = SingleSlotWorker()
