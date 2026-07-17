"""Tests for the job-lifecycle routes in backend/api/routes/storyboard.py:
GET /api/jobs, POST /api/jobs/{id}/retry, POST /api/jobs/{id}/cancel, and
GET /api/jobs/{id}/files/{shot_index}/{filename}.

Calls the route functions directly with monkeypatched store/worker, matching
this repo's existing convention (tests/test_recovery.py) rather than
introducing a TestClient-based harness for just this module.
"""

import asyncio
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from backend.api.routes import storyboard as storyboard_module
from backend.core.config import Settings
from backend.core.job_store import JobStore
from backend.core.worker import Job, JobStatus, ShotStatus


def _store(tmp_path) -> JobStore:
    return JobStore(Settings(db_path=str(tmp_path / "jobs.db")))


def _patch_store_and_worker(monkeypatch, store: JobStore):
    monkeypatch.setattr(storyboard_module, "get_job_store", lambda: store)
    monkeypatch.setattr(storyboard_module.worker, "resubmit", AsyncMock())


# ---- retry ----


def test_retry_rejects_job_not_found(tmp_path, monkeypatch):
    store = _store(tmp_path)
    _patch_store_and_worker(monkeypatch, store)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(storyboard_module.retry_job("does-not-exist"))

    assert exc_info.value.status_code == 404


def test_retry_rejects_non_terminal_status(tmp_path, monkeypatch):
    store = _store(tmp_path)
    _patch_store_and_worker(monkeypatch, store)

    async def scenario():
        await store.create_job("job-1", "storyboard", "running", {"shots": []}, created_at=1.0)
        await storyboard_module.retry_job("job-1")

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(scenario())

    assert exc_info.value.status_code == 409


def test_retry_rejects_cancelled_job_with_no_started_at(tmp_path, monkeypatch):
    """A job cancelled while still QUEUED never started (started_at is
    NULL) and never created shot rows -- nothing to resume, and retrying it
    would enqueue a second entry for the same job id."""
    store = _store(tmp_path)
    _patch_store_and_worker(monkeypatch, store)

    async def scenario():
        await store.create_job("job-1", "storyboard", "cancelled", {"shots": []}, created_at=1.0)
        await storyboard_module.retry_job("job-1")

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(scenario())

    assert exc_info.value.status_code == 409


def test_retry_resets_failed_job_and_resubmits(tmp_path, monkeypatch):
    store = _store(tmp_path)
    _patch_store_and_worker(monkeypatch, store)

    async def scenario():
        await store.create_job(
            "job-1", "storyboard", "queued", {"shots": [], "workflow_name": "default_t2v"}, created_at=1.0,
        )
        await store.update_job_status("job-1", "running", started_at=2.0)
        await store.update_job_status("job-1", "failed", finished_at=3.0, error="boom")
        response = await storyboard_module.retry_job("job-1")
        return response, await store.get_job("job-1")

    response, row = asyncio.run(scenario())

    assert response.status == JobStatus.QUEUED.value
    assert row["error"] is None
    assert row["finished_at"] is None
    storyboard_module.worker.resubmit.assert_awaited_once()
    resubmitted: Job = storyboard_module.worker.resubmit.call_args.args[0]
    assert resubmitted.id == "job-1"
    assert resubmitted.payload == {"shots": [], "workflow_name": "default_t2v"}


def test_retry_resets_only_failed_shots_leaves_submitted_and_done_untouched(tmp_path, monkeypatch):
    store = _store(tmp_path)
    _patch_store_and_worker(monkeypatch, store)

    async def scenario():
        await store.create_job("job-1", "storyboard", "queued", {"shots": []}, created_at=1.0)
        await store.update_job_status("job-1", "running", started_at=2.0)
        await store.upsert_shot("job-1", 0, ShotStatus.DONE.value, files=["a.mp4"])
        await store.upsert_shot("job-1", 1, ShotStatus.SUBMITTED.value, prompt_id="p1", submitted_at=5.0)
        await store.upsert_shot("job-1", 2, ShotStatus.FAILED.value, error="timed out", finished_at=6.0)
        await store.update_job_status("job-1", "failed", finished_at=7.0, error="shot 2 failed")
        await storyboard_module.retry_job("job-1")
        return await store.get_shots("job-1")

    shots = asyncio.run(scenario())
    by_index = {s["shot_index"]: s for s in shots}

    assert by_index[0]["status"] == ShotStatus.DONE.value
    assert by_index[1]["status"] == ShotStatus.SUBMITTED.value
    assert by_index[1]["prompt_id"] == "p1"
    assert by_index[2]["status"] == ShotStatus.PENDING.value
    assert by_index[2]["error"] is None


def test_retry_accepts_cancelled_job_with_partial_shot_progress(tmp_path, monkeypatch):
    store = _store(tmp_path)
    _patch_store_and_worker(monkeypatch, store)

    async def scenario():
        await store.create_job("job-1", "storyboard", "queued", {"shots": []}, created_at=1.0)
        await store.update_job_status("job-1", "running", started_at=2.0)
        await store.upsert_shot("job-1", 0, ShotStatus.DONE.value, files=["a.mp4"])
        await store.update_job_status("job-1", "cancelled", finished_at=5.0)
        response = await storyboard_module.retry_job("job-1")
        return response

    response = asyncio.run(scenario())

    assert response.status == JobStatus.QUEUED.value
    storyboard_module.worker.resubmit.assert_awaited_once()


# ---- cancel ----


def test_cancel_not_found(tmp_path, monkeypatch):
    store = _store(tmp_path)
    _patch_store_and_worker(monkeypatch, store)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(storyboard_module.cancel_job("does-not-exist"))

    assert exc_info.value.status_code == 404


def test_cancel_queued_job_sets_cancelled_directly_no_cancelling_intermediate(tmp_path, monkeypatch):
    store = _store(tmp_path)
    _patch_store_and_worker(monkeypatch, store)

    async def scenario():
        await store.create_job("job-1", "storyboard", "queued", {"shots": []}, created_at=1.0)
        response = await storyboard_module.cancel_job("job-1")
        return response, await store.get_job("job-1")

    response, row = asyncio.run(scenario())

    assert response.status == JobStatus.CANCELLED.value
    assert row["status"] == JobStatus.CANCELLED.value


def test_cancel_running_storyboard_job_sets_cancelling(tmp_path, monkeypatch):
    store = _store(tmp_path)
    _patch_store_and_worker(monkeypatch, store)

    async def scenario():
        await store.create_job("job-1", "storyboard", "running", {"shots": []}, created_at=1.0)
        response = await storyboard_module.cancel_job("job-1")
        return response

    response = asyncio.run(scenario())

    assert response.status == JobStatus.CANCELLING.value


def test_cancel_running_generate_script_job_is_rejected(tmp_path, monkeypatch):
    store = _store(tmp_path)
    _patch_store_and_worker(monkeypatch, store)

    async def scenario():
        await store.create_job("job-1", "generate_script", "running", {"prompt": "x"}, created_at=1.0)
        await storyboard_module.cancel_job("job-1")

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(scenario())

    assert exc_info.value.status_code == 409


def test_cancel_queued_generate_script_job_succeeds(tmp_path, monkeypatch):
    store = _store(tmp_path)
    _patch_store_and_worker(monkeypatch, store)

    async def scenario():
        await store.create_job("job-1", "generate_script", "queued", {"prompt": "x"}, created_at=1.0)
        return await storyboard_module.cancel_job("job-1")

    response = asyncio.run(scenario())

    assert response.status == JobStatus.CANCELLED.value


def test_cancel_already_cancelling_or_cancelled_job_is_idempotent_noop(tmp_path, monkeypatch):
    store = _store(tmp_path)
    _patch_store_and_worker(monkeypatch, store)

    async def scenario():
        await store.create_job("job-1", "storyboard", "cancelled", {"shots": []}, created_at=1.0)
        return await storyboard_module.cancel_job("job-1")

    response = asyncio.run(scenario())

    assert response.status == JobStatus.CANCELLED.value


def test_cancel_terminal_job_is_rejected(tmp_path, monkeypatch):
    store = _store(tmp_path)
    _patch_store_and_worker(monkeypatch, store)

    async def scenario():
        await store.create_job("job-1", "storyboard", "done", {"shots": []}, created_at=1.0)
        await storyboard_module.cancel_job("job-1")

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(scenario())

    assert exc_info.value.status_code == 409


def test_retry_and_cancel_response_shape_matches_get_job_status(tmp_path, monkeypatch):
    store = _store(tmp_path)
    _patch_store_and_worker(monkeypatch, store)

    async def scenario():
        await store.create_job("job-1", "storyboard", "queued", {"shots": []}, created_at=1.0)
        via_get = await storyboard_module.get_job_status("job-1")
        via_cancel = await storyboard_module.cancel_job("job-1")
        return via_get, via_cancel

    via_get, via_cancel = asyncio.run(scenario())

    assert type(via_get) is type(via_cancel)
    assert via_get.id == via_cancel.id == "job-1"


# ---- list_jobs_route ----


def test_list_jobs_returns_all_jobs_newest_first(tmp_path, monkeypatch):
    store = _store(tmp_path)
    _patch_store_and_worker(monkeypatch, store)

    async def scenario():
        await store.create_job("job-1", "storyboard", "done", {"shots": []}, created_at=1.0)
        await store.create_job("job-2", "generate_script", "failed", {"prompt": "x"}, created_at=2.0)
        await store.create_job("job-3", "storyboard", "queued", {"shots": []}, created_at=3.0)
        return await storyboard_module.list_jobs_route(status=None)

    responses = asyncio.run(scenario())

    assert [r.id for r in responses] == ["job-3", "job-2", "job-1"]


def test_list_jobs_filters_by_comma_separated_status(tmp_path, monkeypatch):
    store = _store(tmp_path)
    _patch_store_and_worker(monkeypatch, store)

    async def scenario():
        await store.create_job("job-1", "storyboard", "done", {"shots": []}, created_at=1.0)
        await store.create_job("job-2", "generate_script", "failed", {"prompt": "x"}, created_at=2.0)
        await store.create_job("job-3", "storyboard", "queued", {"shots": []}, created_at=3.0)
        return await storyboard_module.list_jobs_route(status="failed,queued")

    responses = asyncio.run(scenario())

    assert {r.id for r in responses} == {"job-2", "job-3"}


def test_list_jobs_returns_empty_list_when_no_jobs(tmp_path, monkeypatch):
    store = _store(tmp_path)
    _patch_store_and_worker(monkeypatch, store)

    responses = asyncio.run(storyboard_module.list_jobs_route(status=None))

    assert responses == []


def test_list_jobs_includes_shots_for_storyboard_jobs(tmp_path, monkeypatch):
    store = _store(tmp_path)
    _patch_store_and_worker(monkeypatch, store)

    async def scenario():
        await store.create_job("job-1", "storyboard", "done", {"shots": []}, created_at=1.0)
        await store.upsert_shot("job-1", 0, ShotStatus.DONE.value, files=["a.mp4"])
        return await storyboard_module.list_jobs_route(status=None)

    responses = asyncio.run(scenario())

    assert responses[0].shots is not None
    assert responses[0].shots[0].status == ShotStatus.DONE.value


# ---- download_shot_file ----


def test_download_shot_file_returns_existing_file(tmp_path, monkeypatch):
    settings = Settings(output_dir=str(tmp_path / "output"))
    monkeypatch.setattr(storyboard_module, "get_settings", lambda: settings)

    shot_dir = settings.output_path / "job-1" / "shot_000"
    shot_dir.mkdir(parents=True)
    (shot_dir / "video.mp4").write_bytes(b"fake video data")

    response = asyncio.run(storyboard_module.download_shot_file(job_id="job-1", shot_index=0, filename="video.mp4"))

    assert str(response.path) == str(shot_dir / "video.mp4")


def test_download_shot_file_missing_file_returns_404(tmp_path, monkeypatch):
    settings = Settings(output_dir=str(tmp_path / "output"))
    monkeypatch.setattr(storyboard_module, "get_settings", lambda: settings)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(storyboard_module.download_shot_file(job_id="job-1", shot_index=0, filename="video.mp4"))

    assert exc_info.value.status_code == 404


def test_download_shot_file_rejects_invalid_filename_pattern_over_http():
    """The job_id/filename Path(..., pattern=...) constraints (same
    technique as BUG-2's workflow_name fix) are enforced by FastAPI's
    request-handling layer, not the function body -- calling the route
    function directly bypasses them entirely, so this one case needs a real
    HTTP request through the app to exercise it."""
    from fastapi.testclient import TestClient

    from backend.app import app

    client = TestClient(app)
    resp = client.get("/api/jobs/job-1/files/0/not a valid filename.mp4")

    assert resp.status_code == 422
