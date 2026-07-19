"""Tests for Phase 4's real ComfyUI /interrupt support: immediate (not just
between-shot) cancellation of a mid-generation shot.

Covers three layers:
- backend/core/comfyui_client.py: cancel_prompt(), the new
  execution_interrupted WS handling, and _raise_if_history_incomplete (used
  by _poll_history_until_done and storyboard_engine.py's resume path).
- backend/core/storyboard_engine.py: request_shot_interrupt() and
  _run_storyboard_job's new ComfyUIInterrupted branch.
- backend/api/routes/storyboard.py: cancel_job calling request_shot_interrupt.

Mocked throughout -- no live ComfyUI, matching tests/test_comfyui_client.py
and tests/test_recovery.py's existing style.
"""

import asyncio
from unittest.mock import AsyncMock

import pytest
import websockets

from backend.api.routes import storyboard as storyboard_module
from backend.core import storyboard_engine as storyboard_engine_module
from backend.core.comfyui_client import ComfyUIClient, ComfyUIError, ComfyUIInterrupted
from backend.core.config import Settings
from backend.core.job_store import JobStore
from backend.core.worker import Job, JobCancelled, JobStatus, ShotStatus


def _store(tmp_path) -> JobStore:
    return JobStore(Settings(db_path=str(tmp_path / "jobs.db")))


def _client() -> ComfyUIClient:
    return ComfyUIClient(Settings())


def _fake_history(filename: str) -> dict:
    return {"outputs": {"10": {"gifs": [{"filename": filename, "subfolder": "", "type": "output"}]}}}


# ---- comfyui_client.py: cancel_prompt ----


def test_cancel_prompt_posts_to_the_right_url_and_returns_cancelled_flag(monkeypatch):
    client = _client()
    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"cancelled": True}

    class FakeHTTPXClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, **kwargs):
            captured["url"] = url
            return FakeResponse()

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: FakeHTTPXClient())

    result = asyncio.run(client.cancel_prompt("abc123"))

    assert result is True
    assert captured["url"].endswith("/api/jobs/abc123/cancel")


def test_cancel_prompt_returns_false_when_comfyui_says_not_cancelled(monkeypatch):
    client = _client()

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"cancelled": False}

    class FakeHTTPXClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, **kwargs):
            return FakeResponse()

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: FakeHTTPXClient())

    assert asyncio.run(client.cancel_prompt("already-done")) is False


def test_cancel_prompt_raises_comfyui_error_when_unreachable(monkeypatch):
    client = _client()

    class FakeHTTPXClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, **kwargs):
            import httpx
            raise httpx.ConnectError("refused")

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: FakeHTTPXClient())

    with pytest.raises(ComfyUIError, match="Could not reach ComfyUI"):
        asyncio.run(client.cancel_prompt("abc123"))


# ---- comfyui_client.py: wait_for_completion's execution_interrupted handling ----


def test_wait_for_completion_raises_comfyui_interrupted_on_execution_interrupted(monkeypatch):
    class FakeSocket:
        def __init__(self):
            self._sent = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def recv(self):
            if self._sent:
                raise AssertionError("recv() called again after execution_interrupted was raised")
            self._sent = True
            return '{"type": "execution_interrupted", "data": {"prompt_id": "abc123", "node_id": "5"}}'

    monkeypatch.setattr(websockets, "connect", lambda *a, **kw: FakeSocket())

    client = _client()
    with pytest.raises(ComfyUIInterrupted, match="interrupted"):
        asyncio.run(client.wait_for_completion("abc123"))


def test_comfyui_interrupted_is_a_comfyui_error_subclass():
    """Callers that only catch the generic ComfyUIError (e.g. the resume
    reconciliation path's try/except) must still catch this."""
    assert issubclass(ComfyUIInterrupted, ComfyUIError)


# ---- comfyui_client.py: _raise_if_history_incomplete / _poll_history_until_done ----


def test_poll_history_until_done_raises_when_history_is_present_but_incomplete(monkeypatch):
    """A dropped-WS (BUG-1) fallback poll must not silently treat an
    interrupted/failed prompt's history as a success -- ComfyUI's own
    history status doesn't distinguish 'interrupted' from 'errored', so any
    completed=False entry must surface as a real failure, not a retry."""
    client = _client()

    async def fake_get_history(prompt_id):
        return {"outputs": {}, "status": {"status_str": "error", "completed": False, "messages": ["interrupted"]}}

    monkeypatch.setattr(client, "get_history", fake_get_history)

    with pytest.raises(ComfyUIError, match="without completing successfully"):
        asyncio.run(client._poll_history_until_done("abc123", poll_interval_seconds=0))


def test_poll_history_until_done_succeeds_when_history_has_no_status_field(monkeypatch):
    """Backward-compatible default: a history entry with no status key at
    all (e.g. hand-built test fixtures elsewhere in this suite) is treated
    as complete, not rejected -- only an explicit completed: False rejects."""
    client = _client()

    async def fake_get_history(prompt_id):
        return {"outputs": {"1": {"filename": "shot.mp4"}}}

    monkeypatch.setattr(client, "get_history", fake_get_history)

    result = asyncio.run(client._poll_history_until_done("abc123", poll_interval_seconds=0))
    assert result["outputs"]["1"]["filename"] == "shot.mp4"


def test_poll_history_until_done_succeeds_when_status_completed_is_true(monkeypatch):
    client = _client()

    async def fake_get_history(prompt_id):
        return {"outputs": {"1": {"filename": "shot.mp4"}}, "status": {"status_str": "success", "completed": True}}

    monkeypatch.setattr(client, "get_history", fake_get_history)

    result = asyncio.run(client._poll_history_until_done("abc123", poll_interval_seconds=0))
    assert result["outputs"]["1"]["filename"] == "shot.mp4"


# ---- storyboard_engine.py: request_shot_interrupt ----


def _prepare_storyboard_env(monkeypatch, tmp_path, store: JobStore):
    settings = Settings(output_dir=str(tmp_path / "output"), db_path=str(tmp_path / "jobs.db"))
    monkeypatch.setattr(storyboard_engine_module, "get_settings", lambda: settings)
    monkeypatch.setattr(storyboard_engine_module, "get_job_store", lambda: store)
    monkeypatch.setattr(storyboard_engine_module, "_load_workflow", lambda name, settings: {})
    monkeypatch.setattr(storyboard_engine_module.comfyui_client, "health_check", AsyncMock(return_value=True))
    monkeypatch.setattr(storyboard_engine_module.comfyui_client, "fetch_output_bytes", AsyncMock(return_value=b"data"))


def _make_job(shots: list[dict]) -> Job:
    return Job(id="job-1", kind="storyboard", payload={"shots": shots, "workflow_name": "default_t2v"})


def test_request_shot_interrupt_calls_cancel_prompt_for_the_submitted_shot(tmp_path, monkeypatch):
    store = _store(tmp_path)
    monkeypatch.setattr(storyboard_engine_module, "get_job_store", lambda: store)
    monkeypatch.setattr(storyboard_engine_module.comfyui_client, "cancel_prompt", AsyncMock(return_value=True))

    async def scenario():
        await store.create_job("job-1", "storyboard", "running", {}, created_at=1.0)
        await store.upsert_shot("job-1", 0, ShotStatus.SUBMITTED.value, prompt_id="prompt-abc", submitted_at=1.0)
        await storyboard_engine_module.request_shot_interrupt("job-1")

    asyncio.run(scenario())

    storyboard_engine_module.comfyui_client.cancel_prompt.assert_awaited_once_with("prompt-abc")


def test_request_shot_interrupt_is_a_noop_when_no_shot_is_submitted(tmp_path, monkeypatch):
    store = _store(tmp_path)
    monkeypatch.setattr(storyboard_engine_module, "get_job_store", lambda: store)
    monkeypatch.setattr(
        storyboard_engine_module.comfyui_client, "cancel_prompt",
        AsyncMock(side_effect=AssertionError("must not call ComfyUI when nothing is submitted")),
    )

    async def scenario():
        await store.create_job("job-1", "storyboard", "running", {}, created_at=1.0)
        await store.upsert_shot("job-1", 0, ShotStatus.PENDING.value)
        await store.upsert_shot("job-1", 1, ShotStatus.DONE.value, files=["a.mp4"])
        await storyboard_engine_module.request_shot_interrupt("job-1")

    asyncio.run(scenario())  # must not raise


def test_request_shot_interrupt_swallows_a_comfyui_unreachable_error(tmp_path, monkeypatch):
    """Best-effort: if ComfyUI can't be reached to interrupt immediately,
    the existing between-shot cooperative check remains the fallback --
    this must not raise and break the /cancel route."""
    store = _store(tmp_path)
    monkeypatch.setattr(storyboard_engine_module, "get_job_store", lambda: store)
    monkeypatch.setattr(
        storyboard_engine_module.comfyui_client, "cancel_prompt",
        AsyncMock(side_effect=ComfyUIError("unreachable")),
    )

    async def scenario():
        await store.create_job("job-1", "storyboard", "running", {}, created_at=1.0)
        await store.upsert_shot("job-1", 0, ShotStatus.SUBMITTED.value, prompt_id="prompt-abc", submitted_at=1.0)
        await storyboard_engine_module.request_shot_interrupt("job-1")

    asyncio.run(scenario())  # must not raise


# ---- storyboard_engine.py: _run_storyboard_job's ComfyUIInterrupted handling ----


def test_interrupted_shot_ends_job_cancelled_when_job_is_cancelling(tmp_path, monkeypatch):
    """Simulates the real race this feature targets: the shot is submitted
    while the job is still RUNNING (so the shot loop's top-of-iteration
    check doesn't preempt it), then a /cancel request lands *while*
    wait_for_completion is in flight -- flipping the job to CANCELLING and
    (via request_shot_interrupt, not re-tested here) causing ComfyUI to
    report the prompt as interrupted."""
    store = _store(tmp_path)
    _prepare_storyboard_env(monkeypatch, tmp_path, store)
    monkeypatch.setattr(storyboard_engine_module.comfyui_client, "queue_prompt", AsyncMock(return_value="new-prompt"))

    async def _interrupted_mid_wait(*args, **kwargs):
        await store.update_job_status("job-1", "cancelling")
        raise ComfyUIInterrupted("interrupted")

    monkeypatch.setattr(storyboard_engine_module.comfyui_client, "wait_for_completion", _interrupted_mid_wait)

    async def scenario():
        await store.create_job("job-1", "storyboard", "running", {}, created_at=1.0)
        job = _make_job([{"index": 0, "description": "", "prompt": "p", "negative_prompt": ""}])
        await storyboard_engine_module._run_storyboard_job(job)

    with pytest.raises(JobCancelled):
        asyncio.run(scenario())

    shots = asyncio.run(store.get_shots("job-1"))
    assert shots[0]["status"] == ShotStatus.FAILED.value
    assert "interrupted" in shots[0]["error"]


def test_interrupted_shot_ends_job_failed_when_job_is_not_cancelling(tmp_path, monkeypatch):
    """An interrupt with no matching CANCELLING request (e.g. someone hit
    ComfyUI's own stop button) is a real, unrequested failure from ManyTV's
    perspective -- not silently treated as a successful cancellation."""
    store = _store(tmp_path)
    _prepare_storyboard_env(monkeypatch, tmp_path, store)
    monkeypatch.setattr(storyboard_engine_module.comfyui_client, "queue_prompt", AsyncMock(return_value="new-prompt"))
    monkeypatch.setattr(
        storyboard_engine_module.comfyui_client, "wait_for_completion",
        AsyncMock(side_effect=ComfyUIInterrupted("interrupted")),
    )

    async def scenario():
        await store.create_job("job-1", "storyboard", "running", {}, created_at=1.0)
        job = _make_job([{"index": 0, "description": "", "prompt": "p", "negative_prompt": ""}])
        await storyboard_engine_module._run_storyboard_job(job)

    with pytest.raises(ComfyUIInterrupted):
        asyncio.run(scenario())

    shots = asyncio.run(store.get_shots("job-1"))
    assert shots[0]["status"] == ShotStatus.FAILED.value


def test_submitted_shot_with_incomplete_history_is_resubmitted_not_reused(tmp_path, monkeypatch):
    """Resume-after-restart reconciliation: a SUBMITTED shot whose ComfyUI
    history exists but shows completed: False (interrupted or genuinely
    failed before the crash) must be resubmitted fresh, exactly like 'no
    history found' -- never silently reused as if it were a valid result."""
    store = _store(tmp_path)
    _prepare_storyboard_env(monkeypatch, tmp_path, store)
    monkeypatch.setattr(
        storyboard_engine_module.comfyui_client, "get_history",
        AsyncMock(return_value={"outputs": {}, "status": {"status_str": "error", "completed": False, "messages": []}}),
    )
    monkeypatch.setattr(storyboard_engine_module.comfyui_client, "queue_prompt", AsyncMock(return_value="fresh-prompt"))
    monkeypatch.setattr(
        storyboard_engine_module.comfyui_client, "wait_for_completion",
        AsyncMock(return_value=_fake_history("shot0.mp4")),
    )

    async def scenario():
        await store.upsert_shot("job-1", 0, ShotStatus.SUBMITTED.value, prompt_id="old-prompt", submitted_at=1.0)
        job = _make_job([{"index": 0, "description": "", "prompt": "p", "negative_prompt": ""}])
        result = await storyboard_engine_module._run_storyboard_job(job)
        shots = await store.get_shots("job-1")
        return result, shots

    result, shots = asyncio.run(scenario())

    assert result["shots"][0]["prompt_id"] == "fresh-prompt"
    assert shots[0]["prompt_id"] == "fresh-prompt"
    storyboard_engine_module.comfyui_client.queue_prompt.assert_awaited_once()


# ---- backend/api/routes/storyboard.py: cancel_job calling request_shot_interrupt ----


def _patch_store_and_worker(monkeypatch, store: JobStore):
    monkeypatch.setattr(storyboard_module, "get_job_store", lambda: store)
    monkeypatch.setattr(storyboard_module.worker, "resubmit", AsyncMock())
    # cancel_job calls request_shot_interrupt(), which lives in
    # storyboard_engine.py and reaches for its own get_job_store() name --
    # a separate module-level binding that storyboard_module's own patch
    # above does not affect (see TODO.md's v1.2 Phase 3 note on this exact
    # kind of name-rebinding gap). Must be patched too, or this would
    # silently touch the real global JobStore during a routes-level test.
    monkeypatch.setattr(storyboard_engine_module, "get_job_store", lambda: store)


def test_cancel_running_storyboard_job_with_submitted_shot_interrupts_it(tmp_path, monkeypatch):
    store = _store(tmp_path)
    _patch_store_and_worker(monkeypatch, store)
    monkeypatch.setattr(storyboard_engine_module.comfyui_client, "cancel_prompt", AsyncMock(return_value=True))

    async def scenario():
        await store.create_job("job-1", "storyboard", "running", {"shots": []}, created_at=1.0)
        await store.upsert_shot("job-1", 0, ShotStatus.SUBMITTED.value, prompt_id="prompt-abc", submitted_at=1.0)
        return await storyboard_module.cancel_job("job-1")

    response = asyncio.run(scenario())

    assert response.status == JobStatus.CANCELLING.value
    storyboard_engine_module.comfyui_client.cancel_prompt.assert_awaited_once_with("prompt-abc")


def test_cancel_running_storyboard_job_with_no_submitted_shot_does_not_call_comfyui(tmp_path, monkeypatch):
    store = _store(tmp_path)
    _patch_store_and_worker(monkeypatch, store)
    monkeypatch.setattr(
        storyboard_engine_module.comfyui_client, "cancel_prompt",
        AsyncMock(side_effect=AssertionError("must not call ComfyUI when nothing is submitted")),
    )

    async def scenario():
        await store.create_job("job-1", "storyboard", "running", {"shots": []}, created_at=1.0)
        return await storyboard_module.cancel_job("job-1")

    response = asyncio.run(scenario())

    assert response.status == JobStatus.CANCELLING.value
