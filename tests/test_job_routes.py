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
from backend.core.events import EventBus
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


# ---- timestamps ----


def test_get_job_status_includes_job_level_timestamps(tmp_path, monkeypatch):
    store = _store(tmp_path)
    _patch_store_and_worker(monkeypatch, store)

    async def scenario():
        await store.create_job("job-1", "generate_script", "queued", {"prompt": "x"}, created_at=1.0)
        await store.update_job_status("job-1", "running", started_at=2.0)
        await store.update_job_status("job-1", "done", finished_at=3.0)
        return await storyboard_module.get_job_status("job-1")

    response = asyncio.run(scenario())

    assert response.created_at == 1.0
    assert response.started_at == 2.0
    assert response.finished_at == 3.0


def test_get_job_status_includes_shot_level_timestamps(tmp_path, monkeypatch):
    store = _store(tmp_path)
    _patch_store_and_worker(monkeypatch, store)

    async def scenario():
        await store.create_job("job-1", "storyboard", "running", {"shots": []}, created_at=1.0)
        await store.upsert_shot("job-1", 0, ShotStatus.SUBMITTED.value, prompt_id="p1", submitted_at=2.0)
        await store.upsert_shot("job-1", 0, ShotStatus.DONE.value, prompt_id="p1", files=["a.mp4"], finished_at=3.0)
        return await storyboard_module.get_job_status("job-1")

    response = asyncio.run(scenario())

    shot = response.shots[0]
    assert shot.created_at is not None
    assert shot.submitted_at == 2.0
    assert shot.finished_at == 3.0


# ---- logs ----


def test_get_job_logs_rejects_missing_job(tmp_path, monkeypatch):
    store = _store(tmp_path)
    _patch_store_and_worker(monkeypatch, store)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(storyboard_module.get_job_logs("does-not-exist"))

    assert exc_info.value.status_code == 404


def test_get_job_logs_returns_empty_list_for_job_with_no_logs_yet(tmp_path, monkeypatch):
    store = _store(tmp_path)
    _patch_store_and_worker(monkeypatch, store)

    async def scenario():
        await store.create_job("job-1", "generate_script", "queued", {"prompt": "x"}, created_at=1.0)
        return await storyboard_module.get_job_logs("job-1")

    assert asyncio.run(scenario()) == []


def test_get_job_logs_returns_entries_in_order_with_correct_shape(tmp_path, monkeypatch):
    store = _store(tmp_path)
    _patch_store_and_worker(monkeypatch, store)

    async def scenario():
        await store.create_job("job-1", "storyboard", "running", {"shots": []}, created_at=1.0)
        await store.add_job_log("job-1", "info", "job", "Starting job job-1.", timestamp=10.0)
        await store.add_job_log(
            "job-1", "info", "shot", "Job job-1: shot 0 queued as ComfyUI prompt p1",
            shot_index=0, timestamp=11.0,
        )
        return await storyboard_module.get_job_logs("job-1")

    entries = asyncio.run(scenario())

    assert [e.message for e in entries] == [
        "Starting job job-1.",
        "Job job-1: shot 0 queued as ComfyUI prompt p1",
    ]
    assert entries[0].shot_index is None
    assert entries[1].shot_index == 0
    assert entries[1].level == "info"
    assert entries[1].stage == "shot"
    assert entries[1].timestamp == 11.0


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
    assert response.media_type == "video/mp4"


def test_download_shot_file_missing_file_returns_404(tmp_path, monkeypatch):
    settings = Settings(output_dir=str(tmp_path / "output"))
    monkeypatch.setattr(storyboard_module, "get_settings", lambda: settings)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(storyboard_module.download_shot_file(job_id="job-1", shot_index=0, filename="video.mp4"))

    assert exc_info.value.status_code == 404


# ---- artifacts ----


def test_artifact_from_path_reports_size_and_content_type_for_a_real_file(tmp_path):
    real_file = tmp_path / "video.mp4"
    real_file.write_bytes(b"fake video data, 22 bytes")

    artifact = storyboard_module._artifact_from_path(str(real_file))

    assert artifact.filename == "video.mp4"
    assert artifact.size_bytes == real_file.stat().st_size
    assert artifact.content_type == "video/mp4"


def test_artifact_from_path_unknown_extension_has_no_content_type(tmp_path):
    real_file = tmp_path / "data.unknownext"
    real_file.write_bytes(b"data")

    artifact = storyboard_module._artifact_from_path(str(real_file))

    assert artifact.content_type is None
    assert artifact.size_bytes == real_file.stat().st_size


def test_artifact_from_path_missing_file_has_null_size_not_an_exception(tmp_path):
    """The one deliberately-added defensive case: a file recorded in
    shots.files can genuinely disappear between being saved and being read
    on a later poll (confirmed single-user, unmediated filesystem access) --
    stat() failing here must not crash the whole job status response."""
    missing_path = str(tmp_path / "gone.mp4")

    artifact = storyboard_module._artifact_from_path(missing_path)

    assert artifact.filename == "gone.mp4"
    assert artifact.size_bytes is None
    assert artifact.content_type == "video/mp4"  # derived from the extension, no I/O needed


def test_get_job_status_reports_real_artifact_metadata_for_a_done_shot(tmp_path, monkeypatch):
    store = _store(tmp_path)
    _patch_store_and_worker(monkeypatch, store)

    real_file = tmp_path / "shot0.mp4"
    real_file.write_bytes(b"0123456789")

    async def scenario():
        await store.create_job("job-1", "storyboard", "done", {"shots": []}, created_at=1.0)
        await store.upsert_shot("job-1", 0, ShotStatus.DONE.value, files=[str(real_file)])
        return await storyboard_module.get_job_status("job-1")

    response = asyncio.run(scenario())

    artifact = response.shots[0].files[0]
    assert artifact.filename == "shot0.mp4"
    assert artifact.size_bytes == 10
    assert artifact.content_type == "video/mp4"


def test_get_job_status_handles_a_shot_file_missing_from_disk(tmp_path, monkeypatch):
    store = _store(tmp_path)
    _patch_store_and_worker(monkeypatch, store)

    async def scenario():
        await store.create_job("job-1", "storyboard", "done", {"shots": []}, created_at=1.0)
        await store.upsert_shot("job-1", 0, ShotStatus.DONE.value, files=[str(tmp_path / "gone.mp4")])
        return await storyboard_module.get_job_status("job-1")

    response = asyncio.run(scenario())

    assert response.shots[0].files[0].size_bytes is None


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


# ---- SSE events ----


class _FakeRequest:
    """Duck-typed stand-in for fastapi.Request -- the SSE generators only
    ever call `await request.is_disconnected()` and read `request.headers`,
    so a real ASGI scope is unnecessary for testing the generator directly
    (matching this file's existing convention of calling route/helper
    functions directly rather than driving a real TestClient)."""

    def __init__(self, headers: dict[str, str] | None = None):
        self.headers = headers or {}

    async def is_disconnected(self) -> bool:
        return False


def test_job_events_stream_yields_snapshot_then_update_on_publish(tmp_path, monkeypatch):
    store = _store(tmp_path)
    _patch_store_and_worker(monkeypatch, store)

    async def scenario():
        await store.create_job("job-1", "generate_script", "queued", {"prompt": "x"}, created_at=1.0)
        gen = storyboard_module._job_events_stream(_FakeRequest(), store, "job-1", store._events)
        await gen.__anext__()  # the leading "retry: 3000" comment line
        first = await gen.__anext__()

        await store.update_job_status("job-1", "running", started_at=2.0)
        second = await gen.__anext__()

        await gen.aclose()
        return first, second

    first, second = asyncio.run(scenario())

    assert "event: job_updated" in first
    assert '"status":"queued"' in first
    assert '"status":"running"' in second


def test_job_events_stream_unsubscribes_on_close(tmp_path, monkeypatch):
    store = _store(tmp_path)
    _patch_store_and_worker(monkeypatch, store)

    async def scenario():
        await store.create_job("job-1", "generate_script", "queued", {"prompt": "x"}, created_at=1.0)
        gen = storyboard_module._job_events_stream(_FakeRequest(), store, "job-1", store._events)
        await gen.__anext__()
        await gen.aclose()
        return store._events._subscribers.get("job-1", set())

    remaining = asyncio.run(scenario())

    assert remaining == set()


def test_job_events_route_rejects_missing_job(tmp_path, monkeypatch):
    store = _store(tmp_path)
    _patch_store_and_worker(monkeypatch, store)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(storyboard_module.job_events(_FakeRequest(), "does-not-exist"))

    assert exc_info.value.status_code == 404


def test_job_logs_events_stream_sends_only_new_rows(tmp_path, monkeypatch):
    store = _store(tmp_path)
    _patch_store_and_worker(monkeypatch, store)

    async def scenario():
        await store.create_job("job-1", "storyboard", "running", {"shots": []}, created_at=1.0)
        await store.add_job_log("job-1", "info", "job", "Starting job job-1.", timestamp=1.0)
        gen = storyboard_module._job_logs_events_stream(_FakeRequest(), store, "job-1", store._events)
        await gen.__anext__()  # the leading "retry: 3000" comment line
        first = await gen.__anext__()

        await store.add_job_log("job-1", "info", "job", "Job job-1 completed in 1.0s.", timestamp=2.0)
        second = await gen.__anext__()

        await gen.aclose()
        return first, second

    first, second = asyncio.run(scenario())

    assert "Starting job job-1." in first
    assert "Job job-1 completed in 1.0s." in second
    assert "Starting job job-1." not in second  # the delta, not a resend of everything so far


def test_job_logs_events_stream_honors_last_event_id_on_reconnect(tmp_path, monkeypatch):
    store = _store(tmp_path)
    _patch_store_and_worker(monkeypatch, store)

    async def scenario():
        await store.create_job("job-1", "storyboard", "running", {"shots": []}, created_at=1.0)
        await store.add_job_log("job-1", "info", "job", "first", timestamp=1.0)
        await store.add_job_log("job-1", "info", "job", "second", timestamp=2.0)
        logs = await store.get_job_logs("job-1")
        first_log_id = logs[0]["id"]

        # Simulate a reconnect that already saw the first log line.
        gen = storyboard_module._job_logs_events_stream(
            _FakeRequest(headers={"last-event-id": str(first_log_id)}), store, "job-1", store._events,
        )
        await gen.__anext__()  # the leading "retry: 3000" comment line
        only_new = await gen.__anext__()
        await gen.aclose()
        return only_new

    only_new = asyncio.run(scenario())

    assert "second" in only_new
    assert "first" not in only_new


def test_job_logs_events_route_rejects_missing_job(tmp_path, monkeypatch):
    store = _store(tmp_path)
    _patch_store_and_worker(monkeypatch, store)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(storyboard_module.job_logs_events(_FakeRequest(), "does-not-exist"))

    assert exc_info.value.status_code == 404


def test_all_jobs_events_stream_sends_initial_signal_then_one_per_change():
    bus = EventBus()

    async def scenario():
        gen = storyboard_module._all_jobs_events_stream(_FakeRequest(), bus)
        await gen.__anext__()  # the leading "retry: 3000" comment line
        initial = await gen.__anext__()

        bus.publish("job-1", "job_created")
        second = await gen.__anext__()

        await gen.aclose()
        return initial, second

    initial, second = asyncio.run(scenario())

    assert "event: job_updated" in initial
    assert "event: job_updated" in second


def test_all_jobs_events_stream_unsubscribes_on_close():
    bus = EventBus()

    async def scenario():
        gen = storyboard_module._all_jobs_events_stream(_FakeRequest(), bus)
        await gen.__anext__()
        await gen.aclose()
        return bus._subscribers.get(None, set())

    remaining = asyncio.run(scenario())

    assert remaining == set()
