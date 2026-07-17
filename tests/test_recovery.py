"""Tests for the resume/reconciliation logic added for the Job Manager +
Queue + Resume System milestone (TODO.md): backend/core/recovery.py's
job-level routing, and the three shot-level reconciliation branches inside
backend/api/routes/storyboard.py's _run_storyboard_job (DONE -> skip,
PENDING -> normal submit, SUBMITTED -> reconcile against ComfyUI's
/history, either reusing the existing result or resubmitting fresh).

Mocked throughout -- no live ComfyUI/backend services, matching
tests/test_comfyui_client.py's existing style.
"""

import asyncio
from unittest.mock import AsyncMock

from backend.api.routes import storyboard as storyboard_module
from backend.core.comfyui_client import ComfyUIClient, ComfyUIError
from backend.core.config import Settings
from backend.core.job_store import JobStore
from backend.core.recovery import _resubmit_row, _resume_comfyui_jobs, resume_incomplete_jobs
from backend.core.worker import Job, JobCancelled, JobStatus, ShotStatus, SingleSlotWorker


def _store(tmp_path) -> JobStore:
    return JobStore(Settings(db_path=str(tmp_path / "jobs.db")))


def _fake_history(filename: str) -> dict:
    return {"outputs": {"10": {"gifs": [{"filename": filename, "subfolder": "", "type": "output"}]}}}


# ---- backend/core/recovery.py: job-level routing ----


def test_resume_incomplete_jobs_does_nothing_when_none_pending(tmp_path):
    store = _store(tmp_path)
    worker = SingleSlotWorker(store=store)
    worker.resubmit = AsyncMock()
    comfyui_client = ComfyUIClient(Settings())

    asyncio.run(resume_incomplete_jobs(worker, store, comfyui_client))

    worker.resubmit.assert_not_called()


def test_resume_incomplete_jobs_resubmits_generate_script_jobs_without_touching_comfyui(tmp_path):
    """generate_script has no ComfyUI-style history to reconcile against --
    it must go straight back on the queue, never calling health_check()."""
    store = _store(tmp_path)
    worker = SingleSlotWorker(store=store)
    worker.resubmit = AsyncMock()
    comfyui_client = ComfyUIClient(Settings())
    comfyui_client.health_check = AsyncMock(side_effect=AssertionError("should not be called"))

    async def scenario():
        await store.create_job("job-1", "generate_script", "queued", {"prompt": "x"}, created_at=1.0)
        await resume_incomplete_jobs(worker, store, comfyui_client)

    asyncio.run(scenario())

    worker.resubmit.assert_awaited_once()
    resumed_job: Job = worker.resubmit.call_args.args[0]
    assert resumed_job.id == "job-1"
    assert resumed_job.status == JobStatus.QUEUED


def test_resubmit_row_transitions_running_to_resuming(tmp_path):
    store = _store(tmp_path)
    worker = SingleSlotWorker(store=store)
    worker.resubmit = AsyncMock()

    async def scenario():
        await store.create_job("job-1", "storyboard", "running", {"shots": []}, created_at=1.0)
        row = await store.get_job("job-1")
        await _resubmit_row(worker, store, row)
        return await store.get_job("job-1")

    updated_row = asyncio.run(scenario())

    assert updated_row["status"] == JobStatus.RESUMING.value
    resumed_job: Job = worker.resubmit.call_args.args[0]
    assert resumed_job.status == JobStatus.RESUMING


def test_resubmit_row_leaves_queued_jobs_queued(tmp_path):
    """A job that never started needs no RESUMING transition -- nothing was
    in flight to reconcile."""
    store = _store(tmp_path)
    worker = SingleSlotWorker(store=store)
    worker.resubmit = AsyncMock()

    async def scenario():
        await store.create_job("job-1", "storyboard", "queued", {"shots": []}, created_at=1.0)
        row = await store.get_job("job-1")
        await _resubmit_row(worker, store, row)
        return await store.get_job("job-1")

    updated_row = asyncio.run(scenario())

    assert updated_row["status"] == JobStatus.QUEUED.value
    resumed_job: Job = worker.resubmit.call_args.args[0]
    assert resumed_job.status == JobStatus.QUEUED


def test_resume_comfyui_jobs_waits_indefinitely_for_comfyui_before_resubmitting(tmp_path):
    """Confirmed decision: a storyboard job resuming after restart waits
    for ComfyUI to come back rather than giving up on a timeout."""
    store = _store(tmp_path)
    worker = SingleSlotWorker(store=store)
    worker.resubmit = AsyncMock()
    comfyui_client = ComfyUIClient(Settings())

    health_check_results = [False, False, True]
    comfyui_client.health_check = AsyncMock(side_effect=lambda: health_check_results.pop(0))

    async def scenario():
        await store.create_job("job-1", "storyboard", "queued", {"shots": []}, created_at=1.0)
        row = await store.get_job("job-1")
        await _resume_comfyui_jobs(worker, store, comfyui_client, [row], poll_interval_seconds=0)

    asyncio.run(scenario())

    assert comfyui_client.health_check.await_count == 3
    worker.resubmit.assert_awaited_once()


def test_resume_incomplete_jobs_includes_cancelling_in_query(tmp_path):
    """A CANCELLING row at startup means a cancel was requested but the
    backend crashed before the running handler's cooperative check ever
    noticed -- still an unambiguous stop request, finalized directly rather
    than ever resubmitted."""
    store = _store(tmp_path)
    worker = SingleSlotWorker(store=store)
    worker.resubmit = AsyncMock()
    comfyui_client = ComfyUIClient(Settings())

    async def scenario():
        await store.create_job("job-1", "storyboard", "cancelling", {"shots": []}, created_at=1.0)
        await resume_incomplete_jobs(worker, store, comfyui_client)
        return await store.get_job("job-1")

    row = asyncio.run(scenario())

    worker.resubmit.assert_not_called()
    assert row["status"] == JobStatus.CANCELLED.value


def test_cancelling_generate_script_row_at_startup_is_also_finalized(tmp_path):
    """Defensive/kind-agnostic case -- a running generate_script job can't
    actually be cancelled via the API today, but the startup reconciliation
    handles CANCELLING for any kind rather than assuming storyboard-only."""
    store = _store(tmp_path)
    worker = SingleSlotWorker(store=store)
    worker.resubmit = AsyncMock()
    comfyui_client = ComfyUIClient(Settings())
    comfyui_client.health_check = AsyncMock(side_effect=AssertionError("should not be called"))

    async def scenario():
        await store.create_job("job-1", "generate_script", "cancelling", {"prompt": "x"}, created_at=1.0)
        await resume_incomplete_jobs(worker, store, comfyui_client)
        return await store.get_job("job-1")

    row = asyncio.run(scenario())

    worker.resubmit.assert_not_called()
    assert row["status"] == JobStatus.CANCELLED.value


def test_resume_comfyui_jobs_skips_resubmit_if_job_cancelled_during_comfyui_wait(tmp_path):
    """The key regression test for the recovery.py race fix: _resume_comfyui_jobs
    captures its rows once, then waits (possibly a long time) for ComfyUI's
    health check. If a /cancel request lands on the job during that wait, the
    stale captured row must not be used to blindly resubmit and clobber the
    cancellation back to RESUMING."""
    store = _store(tmp_path)
    worker = SingleSlotWorker(store=store)
    worker.resubmit = AsyncMock()
    comfyui_client = ComfyUIClient(Settings())

    health_check_results = [False, False, True]

    async def scenario():
        await store.create_job("job-1", "storyboard", "running", {"shots": []}, created_at=1.0)
        row = await store.get_job("job-1")

        async def fake_health_check():
            result = health_check_results.pop(0)
            if result:
                # Simulate a concurrent /cancel request landing while this
                # job was waiting for ComfyUI to come back.
                await store.update_job_status("job-1", JobStatus.CANCELLED.value, finished_at=99.0)
            return result

        comfyui_client.health_check = fake_health_check
        await _resume_comfyui_jobs(worker, store, comfyui_client, [row], poll_interval_seconds=0)
        return await store.get_job("job-1")

    final_row = asyncio.run(scenario())

    worker.resubmit.assert_not_called()
    assert final_row["status"] == JobStatus.CANCELLED.value


def test_resume_comfyui_jobs_finalizes_cancelling_row_found_at_refetch_time(tmp_path):
    """Same shape as above, but the row is found CANCELLING (not yet
    CANCELLED) at re-fetch time -- must still finalize to CANCELLED rather
    than resubmitting."""
    store = _store(tmp_path)
    worker = SingleSlotWorker(store=store)
    worker.resubmit = AsyncMock()
    comfyui_client = ComfyUIClient(Settings())
    comfyui_client.health_check = AsyncMock(return_value=True)

    async def scenario():
        await store.create_job("job-1", "storyboard", "running", {"shots": []}, created_at=1.0)
        row = await store.get_job("job-1")
        await store.update_job_status("job-1", JobStatus.CANCELLING.value)
        await _resume_comfyui_jobs(worker, store, comfyui_client, [row], poll_interval_seconds=0)
        return await store.get_job("job-1")

    final_row = asyncio.run(scenario())

    worker.resubmit.assert_not_called()
    assert final_row["status"] == JobStatus.CANCELLED.value


# ---- backend/api/routes/storyboard.py: shot-level reconciliation ----


def _prepare_storyboard_env(monkeypatch, tmp_path, store: JobStore):
    """Points _run_storyboard_job's settings/store/workflow-loading at test
    doubles without touching the real output/ dir or backend/workflows/."""
    settings = Settings(output_dir=str(tmp_path / "output"), db_path=str(tmp_path / "jobs.db"))
    monkeypatch.setattr(storyboard_module, "get_settings", lambda: settings)
    monkeypatch.setattr(storyboard_module, "get_job_store", lambda: store)
    monkeypatch.setattr(storyboard_module, "_load_workflow", lambda name, settings: {})
    monkeypatch.setattr(storyboard_module.comfyui_client, "health_check", AsyncMock(return_value=True))
    monkeypatch.setattr(storyboard_module.comfyui_client, "fetch_output_bytes", AsyncMock(return_value=b"data"))


def _make_job(shots: list[dict]) -> Job:
    return Job(id="job-1", kind="storyboard", payload={"shots": shots, "workflow_name": "default_t2v"})


def test_done_shot_is_skipped_not_reverified_against_comfyui(tmp_path, monkeypatch):
    store = _store(tmp_path)
    _prepare_storyboard_env(monkeypatch, tmp_path, store)

    async def scenario():
        await store.upsert_shot("job-1", 0, ShotStatus.DONE.value, prompt_id="old-prompt", files=["a.mp4"])
        monkeypatch.setattr(
            storyboard_module.comfyui_client, "queue_prompt",
            AsyncMock(side_effect=AssertionError("must not resubmit a DONE shot")),
        )
        monkeypatch.setattr(
            storyboard_module.comfyui_client, "get_history",
            AsyncMock(side_effect=AssertionError("must not re-check history for a DONE shot")),
        )
        job = _make_job([{"index": 0, "description": "", "prompt": "p", "negative_prompt": ""}])
        return await storyboard_module._run_storyboard_job(job)

    result = asyncio.run(scenario())

    assert result["shots"] == [{"shot_index": 0, "prompt_id": "old-prompt", "files": ["a.mp4"]}]


def test_pending_shot_goes_through_normal_submit_and_wait(tmp_path, monkeypatch):
    store = _store(tmp_path)
    _prepare_storyboard_env(monkeypatch, tmp_path, store)
    monkeypatch.setattr(
        storyboard_module.comfyui_client, "queue_prompt", AsyncMock(return_value="new-prompt")
    )
    monkeypatch.setattr(
        storyboard_module.comfyui_client, "wait_for_completion",
        AsyncMock(return_value=_fake_history("shot0.mp4")),
    )

    async def scenario():
        job = _make_job([{"index": 0, "description": "", "prompt": "p", "negative_prompt": ""}])
        result = await storyboard_module._run_storyboard_job(job)
        shots = await store.get_shots("job-1")
        return result, shots

    result, shots = asyncio.run(scenario())

    assert result["shots"][0]["prompt_id"] == "new-prompt"
    assert shots[0]["status"] == ShotStatus.DONE.value
    assert shots[0]["prompt_id"] == "new-prompt"
    storyboard_module.comfyui_client.queue_prompt.assert_awaited_once()


def test_submitted_shot_with_history_found_is_reused_not_resubmitted(tmp_path, monkeypatch):
    """Core resume guarantee: if ComfyUI already has a finished result for a
    prompt_id recorded before the backend last stopped, reuse it -- this
    ComfyUI workflow isn't guaranteed deterministic, so resubmitting could
    silently produce a different video than one already relied on."""
    store = _store(tmp_path)
    _prepare_storyboard_env(monkeypatch, tmp_path, store)
    monkeypatch.setattr(
        storyboard_module.comfyui_client, "queue_prompt",
        AsyncMock(side_effect=AssertionError("must not resubmit when history is found")),
    )
    monkeypatch.setattr(
        storyboard_module.comfyui_client, "get_history",
        AsyncMock(return_value=_fake_history("shot0.mp4")),
    )

    async def scenario():
        await store.upsert_shot("job-1", 0, ShotStatus.SUBMITTED.value, prompt_id="old-prompt", submitted_at=1.0)
        job = _make_job([{"index": 0, "description": "", "prompt": "p", "negative_prompt": ""}])
        result = await storyboard_module._run_storyboard_job(job)
        shots = await store.get_shots("job-1")
        return result, shots

    result, shots = asyncio.run(scenario())

    assert result["shots"][0]["prompt_id"] == "old-prompt"
    assert shots[0]["status"] == ShotStatus.DONE.value
    assert shots[0]["prompt_id"] == "old-prompt"


def test_submitted_shot_with_no_history_is_resubmitted_fresh(tmp_path, monkeypatch):
    """If ComfyUI has no memory of the prompt at all (get_history raises
    ComfyUIError, not just "unreachable"), it's safe to assume it's genuinely
    gone and resubmit."""
    store = _store(tmp_path)
    _prepare_storyboard_env(monkeypatch, tmp_path, store)
    monkeypatch.setattr(
        storyboard_module.comfyui_client, "get_history",
        AsyncMock(side_effect=ComfyUIError("no history entry")),
    )
    monkeypatch.setattr(
        storyboard_module.comfyui_client, "queue_prompt", AsyncMock(return_value="resubmitted-prompt")
    )
    monkeypatch.setattr(
        storyboard_module.comfyui_client, "wait_for_completion",
        AsyncMock(return_value=_fake_history("shot0.mp4")),
    )

    async def scenario():
        await store.upsert_shot("job-1", 0, ShotStatus.SUBMITTED.value, prompt_id="old-prompt", submitted_at=1.0)
        job = _make_job([{"index": 0, "description": "", "prompt": "p", "negative_prompt": ""}])
        result = await storyboard_module._run_storyboard_job(job)
        shots = await store.get_shots("job-1")
        return result, shots

    result, shots = asyncio.run(scenario())

    assert result["shots"][0]["prompt_id"] == "resubmitted-prompt"
    assert shots[0]["status"] == ShotStatus.DONE.value
    assert shots[0]["prompt_id"] == "resubmitted-prompt"
    storyboard_module.comfyui_client.queue_prompt.assert_awaited_once()


def test_submitted_shot_with_comfyui_unreachable_propagates_without_resubmitting(tmp_path, monkeypatch):
    """A non-ComfyUIError from get_history (e.g. a real connection error)
    must not be treated the same as "no history" -- resubmitting blindly
    while ComfyUI's actual state is unknown is exactly what this design
    avoids."""
    store = _store(tmp_path)
    _prepare_storyboard_env(monkeypatch, tmp_path, store)
    monkeypatch.setattr(
        storyboard_module.comfyui_client, "get_history",
        AsyncMock(side_effect=ConnectionError("comfyui unreachable")),
    )
    monkeypatch.setattr(
        storyboard_module.comfyui_client, "queue_prompt",
        AsyncMock(side_effect=AssertionError("must not resubmit on an unrelated error")),
    )

    async def scenario():
        await store.upsert_shot("job-1", 0, ShotStatus.SUBMITTED.value, prompt_id="old-prompt", submitted_at=1.0)
        job = _make_job([{"index": 0, "description": "", "prompt": "p", "negative_prompt": ""}])
        await storyboard_module._run_storyboard_job(job)

    try:
        asyncio.run(scenario())
        raised = False
    except ConnectionError:
        raised = True

    assert raised


def test_shot_loop_raises_job_cancelled_when_status_is_cancelling(tmp_path, monkeypatch):
    """The cooperative cancellation check fires before any ComfyUI call is
    made for the next shot -- a job with shot 0 already DONE and shot 1
    PENDING, but the job's own row already CANCELLING, must raise
    JobCancelled without ever touching ComfyUI."""
    store = _store(tmp_path)
    _prepare_storyboard_env(monkeypatch, tmp_path, store)
    monkeypatch.setattr(
        storyboard_module.comfyui_client, "queue_prompt",
        AsyncMock(side_effect=AssertionError("must not submit anything once cancelling")),
    )

    async def scenario():
        await store.create_job("job-1", "storyboard", "cancelling", {"shots": []}, created_at=1.0)
        await store.upsert_shot("job-1", 0, ShotStatus.DONE.value, files=["a.mp4"])
        await store.upsert_shot("job-1", 1, ShotStatus.PENDING.value)
        job = _make_job([
            {"index": 0, "description": "", "prompt": "p", "negative_prompt": ""},
            {"index": 1, "description": "", "prompt": "p", "negative_prompt": ""},
        ])
        await storyboard_module._run_storyboard_job(job)

    try:
        asyncio.run(scenario())
        raised = False
    except JobCancelled:
        raised = True

    assert raised


def test_shot_loop_checks_cancellation_between_every_shot(tmp_path, monkeypatch):
    """Two PENDING shots; the job gets flipped to CANCELLING (simulating a
    concurrent /cancel request) right after shot 0 completes. Shot 1 must
    never be submitted."""
    store = _store(tmp_path)
    _prepare_storyboard_env(monkeypatch, tmp_path, store)
    monkeypatch.setattr(storyboard_module.comfyui_client, "queue_prompt", AsyncMock(return_value="prompt-0"))

    async def fake_wait_for_completion(prompt_id, on_progress=None):
        await store.update_job_status("job-1", JobStatus.CANCELLING.value)
        return _fake_history("shot0.mp4")

    monkeypatch.setattr(storyboard_module.comfyui_client, "wait_for_completion", fake_wait_for_completion)

    async def scenario():
        await store.create_job("job-1", "storyboard", "running", {"shots": []}, created_at=1.0)
        job = _make_job([
            {"index": 0, "description": "", "prompt": "p", "negative_prompt": ""},
            {"index": 1, "description": "", "prompt": "p", "negative_prompt": ""},
        ])
        await storyboard_module._run_storyboard_job(job)

    try:
        asyncio.run(scenario())
        raised = False
    except JobCancelled:
        raised = True

    assert raised
    storyboard_module.comfyui_client.queue_prompt.assert_awaited_once()
