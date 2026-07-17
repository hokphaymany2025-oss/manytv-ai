"""Regression tests for BUG-1: a dropped ComfyUI WebSocket must not
immediately fail a job that may still be running server-side.

ComfyUI keeps executing a queued prompt regardless of whether this client's
monitoring WebSocket stays connected (confirmed in server.log -- see
TODO.md BUG-1), so wait_for_completion() falls back to polling /history
instead of treating any WebSocket drop as fatal to the whole job.

Uses asyncio.run() directly in plain `def test_...` functions rather than
an async test plugin, since none is installed and none is needed for tests
this small.
"""

import asyncio

import pytest
import websockets

from backend.core.comfyui_client import ComfyUIClient, ComfyUIError
from backend.core.config import Settings


def _client() -> ComfyUIClient:
    return ComfyUIClient(Settings())


def test_poll_history_until_done_retries_until_present(monkeypatch):
    client = _client()
    calls = []

    async def fake_get_history(prompt_id):
        calls.append(prompt_id)
        if len(calls) < 3:
            raise ComfyUIError("not in history yet")
        return {"outputs": {"1": {"filename": "shot.mp4"}}}

    async def no_op_sleep(_seconds):
        return None

    monkeypatch.setattr(client, "get_history", fake_get_history)
    monkeypatch.setattr(asyncio, "sleep", no_op_sleep)

    result = asyncio.run(client._poll_history_until_done("abc123", poll_interval_seconds=0))

    assert len(calls) == 3
    assert result["outputs"]["1"]["filename"] == "shot.mp4"


def test_poll_history_until_done_propagates_non_comfyui_errors(monkeypatch):
    """A genuinely dead ComfyUI (e.g. connection refused) must fail fast,
    not retry silently for the whole GENERATION_TIMEOUT_SECONDS window."""
    client = _client()

    async def fake_get_history(prompt_id):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(client, "get_history", fake_get_history)

    with pytest.raises(RuntimeError):
        asyncio.run(client._poll_history_until_done("abc123", poll_interval_seconds=0))


def test_wait_for_completion_falls_back_to_polling_on_dropped_socket(monkeypatch):
    client = _client()

    class DroppedSocket:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def recv(self):
            raise OSError("connection dropped mid-generation")

    def fake_connect(*args, **kwargs):
        return DroppedSocket()

    monkeypatch.setattr(websockets, "connect", fake_connect)

    polled_ids = []

    async def fake_poll(prompt_id, poll_interval_seconds):
        polled_ids.append(prompt_id)
        return {"outputs": {"1": {"filename": "shot.mp4"}}}

    monkeypatch.setattr(client, "_poll_history_until_done", fake_poll)

    result = asyncio.run(client.wait_for_completion("abc123"))

    assert polled_ids == ["abc123"]
    assert result["outputs"]["1"]["filename"] == "shot.mp4"


def test_wait_for_completion_still_raises_on_execution_error(monkeypatch):
    """A real ComfyUI-reported failure (not a dropped connection) must
    still fail the job -- only connection drops get the polling fallback."""
    client = _client()

    class FakeSocket:
        def __init__(self):
            self._sent = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def recv(self):
            if self._sent:
                raise AssertionError("recv() called again after execution_error was raised")
            self._sent = True
            return '{"type": "execution_error", "data": {"prompt_id": "abc123", "exception_message": "boom"}}'

    def fake_connect(*args, **kwargs):
        return FakeSocket()

    monkeypatch.setattr(websockets, "connect", fake_connect)

    with pytest.raises(ComfyUIError, match="execution error"):
        asyncio.run(client.wait_for_completion("abc123"))
