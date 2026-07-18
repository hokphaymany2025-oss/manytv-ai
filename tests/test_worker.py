"""Tests for backend/core/worker.py's SingleSlotWorker -- previously only
exercised indirectly via tests/test_recovery.py. Covers the queue/failure
semantics TODO.md already flagged as untested, plus the cancellation state
machine added alongside job retry: a job cancelled while still QUEUED must
be skipped without ever invoking its handler, and a handler that
cooperatively raises JobCancelled must finalize the job as CANCELLED, not
FAILED.

Mocked throughout -- no live ComfyUI/backend services, matching the rest of
this test suite's style.
"""

import asyncio

import pytest

from backend.core.config import Settings
from backend.core.job_store import JobStore
from backend.core.worker import Job, JobCancelled, JobStatus, SingleSlotWorker


def _store(tmp_path) -> JobStore:
    return JobStore(Settings(db_path=str(tmp_path / "jobs.db")))


async def _run_one_iteration(worker: SingleSlotWorker) -> None:
    """Drives exactly one pass of the worker loop body without looping
    forever -- runs _run() as a background task and waits for the queue to
    drain rather than reimplementing the loop's internals in each test.
    """
    task = asyncio.create_task(worker._run())
    await worker._queue.join()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


# ---- start / stop / resubmit ----
# Previously untested directly -- every other test in this file drives the
# queue via _run_one_iteration rather than the real start()/stop() lifecycle
# methods, and resubmit() was only ever exercised with worker.resubmit
# monkeypatched out entirely (see tests/test_job_routes.py).


def test_start_is_idempotent_and_creates_a_background_task(tmp_path):
    store = _store(tmp_path)
    worker = SingleSlotWorker(store=store)

    async def scenario():
        worker.start()
        first_task = worker._task
        worker.start()  # second call must be a no-op, not a second task
        second_task = worker._task
        await worker.stop()
        return first_task, second_task

    first_task, second_task = asyncio.run(scenario())

    assert first_task is not None
    assert first_task is second_task


def test_stop_cancels_the_task_and_clears_it(tmp_path):
    store = _store(tmp_path)
    worker = SingleSlotWorker(store=store)

    async def scenario():
        worker.start()
        await worker.stop()
        return worker._task

    assert asyncio.run(scenario()) is None


def test_resubmit_enqueues_the_job_as_is(tmp_path):
    """Unlike submit(), resubmit() must not create a new job or touch the
    store -- the row already exists (backend/core/recovery.py reconstructs
    the Job from it); this only needs to land back on the queue."""
    store = _store(tmp_path)
    worker = SingleSlotWorker(store=store)
    job = Job(id="job-1", kind="test", payload={})

    async def scenario():
        await worker.resubmit(job)
        return await worker._queue.get()

    dequeued = asyncio.run(scenario())

    assert dequeued is job


def test_submit_creates_job_and_it_runs_to_done(tmp_path):
    store = _store(tmp_path)
    worker = SingleSlotWorker(store=store)

    async def handler(job: Job) -> dict:
        return {"ok": True}

    worker.register_handler("test", handler)

    async def scenario():
        job = await worker.submit("test", {})
        await _run_one_iteration(worker)
        return job.id

    job_id = asyncio.run(scenario())
    row = asyncio.run(store.get_job(job_id))

    assert row["status"] == JobStatus.DONE.value


def test_handler_exception_marks_job_failed_with_error(tmp_path):
    store = _store(tmp_path)
    worker = SingleSlotWorker(store=store)

    async def handler(job: Job) -> dict:
        raise ValueError("boom")

    worker.register_handler("test", handler)

    async def scenario():
        job = await worker.submit("test", {})
        await _run_one_iteration(worker)
        return job.id

    job_id = asyncio.run(scenario())
    row = asyncio.run(store.get_job(job_id))

    assert row["status"] == JobStatus.FAILED.value
    assert row["error"] == "boom"


def test_missing_handler_raises_and_marks_failed(tmp_path):
    store = _store(tmp_path)
    worker = SingleSlotWorker(store=store)

    async def scenario():
        job = await worker.submit("unregistered-kind", {})
        await _run_one_iteration(worker)
        return job.id

    job_id = asyncio.run(scenario())
    row = asyncio.run(store.get_job(job_id))

    assert row["status"] == JobStatus.FAILED.value
    assert "unregistered-kind" in row["error"]


def test_two_jobs_run_strictly_sequentially(tmp_path):
    store = _store(tmp_path)
    worker = SingleSlotWorker(store=store)
    order: list[str] = []

    async def handler(job: Job) -> dict:
        order.append(f"start-{job.payload['label']}")
        await asyncio.sleep(0.01)
        order.append(f"end-{job.payload['label']}")
        return {}

    worker.register_handler("test", handler)

    async def scenario():
        await worker.submit("test", {"label": "a"})
        await worker.submit("test", {"label": "b"})
        await _run_one_iteration(worker)

    asyncio.run(scenario())

    assert order == ["start-a", "end-a", "start-b", "end-b"]


def test_dequeue_skips_job_already_cancelled_in_store(tmp_path):
    store = _store(tmp_path)
    worker = SingleSlotWorker(store=store)
    handler_called = False

    async def handler(job: Job) -> dict:
        nonlocal handler_called
        handler_called = True
        return {}

    worker.register_handler("test", handler)

    async def scenario():
        job = await worker.submit("test", {})
        await store.update_job_status(job.id, JobStatus.CANCELLED.value)
        await _run_one_iteration(worker)
        return job.id

    job_id = asyncio.run(scenario())
    row = asyncio.run(store.get_job(job_id))

    assert handler_called is False
    assert row["status"] == JobStatus.CANCELLED.value


def test_dequeue_finalizes_cancelling_job_to_cancelled_without_running_handler(tmp_path):
    store = _store(tmp_path)
    worker = SingleSlotWorker(store=store)
    handler_called = False

    async def handler(job: Job) -> dict:
        nonlocal handler_called
        handler_called = True
        return {}

    worker.register_handler("test", handler)

    async def scenario():
        job = await worker.submit("test", {})
        await store.update_job_status(job.id, JobStatus.CANCELLING.value)
        await _run_one_iteration(worker)
        return job.id

    job_id = asyncio.run(scenario())
    row = asyncio.run(store.get_job(job_id))

    assert handler_called is False
    assert row["status"] == JobStatus.CANCELLED.value


def test_job_cancelled_exception_marks_job_cancelled_not_failed(tmp_path):
    store = _store(tmp_path)
    worker = SingleSlotWorker(store=store)

    async def handler(job: Job) -> dict:
        raise JobCancelled("cancelled mid-flight")

    worker.register_handler("test", handler)

    async def scenario():
        job = await worker.submit("test", {})
        await _run_one_iteration(worker)
        return job.id

    job_id = asyncio.run(scenario())
    row = asyncio.run(store.get_job(job_id))

    assert row["status"] == JobStatus.CANCELLED.value
    assert row["error"] is None


def test_asyncio_cancelled_error_still_propagates(tmp_path):
    store = _store(tmp_path)
    worker = SingleSlotWorker(store=store)

    async def handler(job: Job) -> dict:
        raise asyncio.CancelledError()

    worker.register_handler("test", handler)

    async def scenario():
        await worker.submit("test", {})
        task = asyncio.create_task(worker._run())
        for _ in range(100):
            if task.done():
                break
            await asyncio.sleep(0.05)
        assert task.done()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())


def test_successful_job_writes_started_and_completed_log_entries(tmp_path):
    store = _store(tmp_path)
    worker = SingleSlotWorker(store=store)

    async def handler(job: Job) -> dict:
        return {"ok": True}

    worker.register_handler("test", handler)

    async def scenario():
        job = await worker.submit("test", {})
        await _run_one_iteration(worker)
        return job.id

    job_id = asyncio.run(scenario())
    logs = asyncio.run(store.get_job_logs(job_id))

    assert [entry["stage"] for entry in logs] == ["job", "job"]
    assert "Starting job" in logs[0]["message"]
    assert "completed" in logs[1]["message"]


def test_failed_job_writes_error_level_log_entry(tmp_path):
    store = _store(tmp_path)
    worker = SingleSlotWorker(store=store)

    async def handler(job: Job) -> dict:
        raise ValueError("boom")

    worker.register_handler("test", handler)

    async def scenario():
        job = await worker.submit("test", {})
        await _run_one_iteration(worker)
        return job.id

    job_id = asyncio.run(scenario())
    logs = asyncio.run(store.get_job_logs(job_id))

    failure_entries = [e for e in logs if e["level"] == "error"]
    assert len(failure_entries) == 1
    assert "boom" in failure_entries[0]["message"]


def test_job_cancelled_exception_writes_info_level_log_entry(tmp_path):
    store = _store(tmp_path)
    worker = SingleSlotWorker(store=store)

    async def handler(job: Job) -> dict:
        raise JobCancelled("cancelled mid-flight")

    worker.register_handler("test", handler)

    async def scenario():
        job = await worker.submit("test", {})
        await _run_one_iteration(worker)
        return job.id

    job_id = asyncio.run(scenario())
    logs = asyncio.run(store.get_job_logs(job_id))

    assert any("cancelled" in e["message"].lower() and e["level"] == "info" for e in logs)


def test_dequeue_skips_job_already_cancelled_writes_log_entry(tmp_path):
    store = _store(tmp_path)
    worker = SingleSlotWorker(store=store)

    async def handler(job: Job) -> dict:
        return {}

    worker.register_handler("test", handler)

    async def scenario():
        job = await worker.submit("test", {})
        await store.update_job_status(job.id, JobStatus.CANCELLED.value)
        await _run_one_iteration(worker)
        return job.id

    job_id = asyncio.run(scenario())
    logs = asyncio.run(store.get_job_logs(job_id))

    assert len(logs) == 1
    assert "skipping" in logs[0]["message"]


def test_cancelled_job_still_releases_gpu_memory_and_task_done(tmp_path, monkeypatch):
    store = _store(tmp_path)
    worker = SingleSlotWorker(store=store)
    release_called = False

    def fake_release() -> None:
        nonlocal release_called
        release_called = True

    monkeypatch.setattr("backend.core.worker.release_gpu_memory", fake_release)

    async def handler(job: Job) -> dict:
        raise JobCancelled("cancelled mid-flight")

    worker.register_handler("test", handler)

    async def scenario():
        await worker.submit("test", {})
        await _run_one_iteration(worker)

    asyncio.run(scenario())

    assert release_called is True
    assert worker._queue.empty()
