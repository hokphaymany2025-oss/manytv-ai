"""In-process pub/sub used to push job/shot/log changes to SSE subscribers.

Deliberately generic -- knows nothing about JobStore, JobStatus, or any
response schema, so there's no import-cycle risk with job_store.py/
storyboard.py. Single-process only (see backend/api/routes/storyboard.py's
SSE routes and TODO.md): this bus lives in one uvicorn process's memory, so
it only ever sees writes made by JobStore instances in that same process --
a deliberate, named assumption matching this project's actual deployment
(uvicorn --reload, no --workers N), not a general-purpose multi-process
message broker.

subscribe/unsubscribe/publish are plain synchronous methods with no lock:
every call happens on the event loop thread with no `await` in the method
body, so each one already completes atomically with respect to every other
coroutine. Callers publishing from JobStore must do so from the async
wrapper *after* the underlying asyncio.to_thread call has returned (i.e.
back on the loop thread) -- never from inside the threaded sync callable
itself, since asyncio.Queue is not safe to touch off the loop thread.
"""

import asyncio
from collections import defaultdict
from functools import lru_cache
from typing import NamedTuple, Optional


class JobEvent(NamedTuple):
    job_id: str
    event_type: str  # "job_created" | "job_updated" | "log_added"


class EventBus:
    def __init__(self, maxsize: int = 32):
        self._maxsize = maxsize
        self._subscribers: dict[Optional[str], set["asyncio.Queue[JobEvent]"]] = defaultdict(set)

    def subscribe(self, job_id: Optional[str]) -> "asyncio.Queue[JobEvent]":
        """`job_id=None` subscribes to every job (used by the dashboard's
        all-jobs stream); a concrete job_id subscribes to just that job."""
        queue: "asyncio.Queue[JobEvent]" = asyncio.Queue(maxsize=self._maxsize)
        self._subscribers[job_id].add(queue)
        return queue

    def unsubscribe(self, job_id: Optional[str], queue: "asyncio.Queue[JobEvent]") -> None:
        self._subscribers[job_id].discard(queue)

    def publish(self, job_id: str, event_type: str) -> None:
        """Fans out to both this job's own subscribers and the all-jobs
        (job_id=None) subscribers -- the latter need job_id on the event
        itself (unlike a job-scoped subscriber, which already knows it) to
        know which job to re-fetch. Bounded queues with a drop-oldest policy:
        these are idempotent "something changed, go re-fetch" hints, not a
        guaranteed-delivery delta log, so losing an intermediate one is
        harmless as long as the next one still arrives.
        """
        event = JobEvent(job_id=job_id, event_type=event_type)
        for queue in (*self._subscribers.get(job_id, ()), *self._subscribers.get(None, ())):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(event)


@lru_cache
def get_event_bus() -> EventBus:
    return EventBus()
