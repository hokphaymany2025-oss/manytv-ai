"""Server-Sent Events stream generators for job/log updates.

Split out of backend/api/routes/storyboard.py (v1.2 Phase 3).
"""

import asyncio
from typing import Optional

from fastapi import Request

from backend.api.job_responses import _build_job_status_response
from backend.core.events import EventBus
from backend.core.job_store import JobStore
from backend.models.schemas import JobLogEntry

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
