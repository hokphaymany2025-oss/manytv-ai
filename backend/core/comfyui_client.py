"""Thin async client for a locally running ComfyUI instance.

ManyTV never loads a diffusion/video model itself. Every generation is a
prompt graph POSTed to ComfyUI's `/prompt` endpoint, tracked to completion
over its `/ws` WebSocket, then pulled back via `/history` + `/view`. This
keeps model loading, VRAM scheduling (--lowvram/--gpu-only) and node
execution entirely inside ComfyUI's own process, where it belongs.
"""

import asyncio
import json
import logging
from typing import Any, Callable, Optional

import httpx
import websockets

from backend.core.config import Settings

logger = logging.getLogger("manytv.comfyui")


class ComfyUIError(RuntimeError):
    """Raised when ComfyUI is unreachable or reports a failed execution."""


class ComfyUIInterrupted(ComfyUIError):
    """Raised when ComfyUI reports a prompt was interrupted (execution_interrupted),
    as opposed to a genuine execution_error. Distinguished from a plain
    ComfyUIError so callers can tell "someone called /interrupt (or the new
    /api/jobs/{id}/cancel) on this prompt" apart from "this workflow actually
    failed" -- ComfyUI's own /history entry doesn't preserve that distinction
    (see _raise_if_history_incomplete below), only the live WS message does.
    """


def _raise_if_history_incomplete(history: dict[str, Any], prompt_id: str) -> None:
    """A present /history entry can represent a real success OR a finished-
    but-unsuccessful outcome (a genuine error or an interrupt) -- ComfyUI's
    own history status doesn't distinguish the two (confirmed against the
    installed ComfyUI's execution.py: PromptQueue.task_done sets
    status_str='error'/completed=False identically for both cases). Only
    call sites that never see the live WS message stream directly need this
    check -- wait_for_completion's own WS loop already intercepts every real
    failure via execution_error/execution_interrupted before ever reaching a
    "done" return, so a present-but-incomplete history is unreachable there
    in practice. The two call sites that do need it: _poll_history_until_done
    (the WS-drop fallback, BUG-1) and storyboard_engine.py's SUBMITTED-shot
    resume-after-restart reconciliation, both of which only ever see /history
    and never the WS stream.
    """
    status = history.get("status")
    if status is not None and status.get("completed") is False:
        raise ComfyUIError(
            f"ComfyUI prompt {prompt_id} finished without completing successfully "
            f"(status: {status.get('status_str')}, messages: {status.get('messages')})."
        )


class ComfyUIClient:
    def __init__(self, settings: Settings):
        self._settings = settings
        self._client_id = settings.comfyui_client_id

    async def health_check(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(f"{self._settings.comfyui_http_url}/system_stats")
                return resp.status_code == 200
        except httpx.HTTPError:
            return False

    async def queue_prompt(self, workflow: dict[str, Any]) -> str:
        payload = {"prompt": workflow, "client_id": self._client_id}
        async with httpx.AsyncClient(timeout=30) as client:
            try:
                resp = await client.post(f"{self._settings.comfyui_http_url}/prompt", json=payload)
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise ComfyUIError(f"ComfyUI rejected the prompt: {exc.response.text}") from exc
            except httpx.HTTPError as exc:
                raise ComfyUIError(f"Could not reach ComfyUI at {self._settings.comfyui_http_url}") from exc

            data = resp.json()
            prompt_id = data.get("prompt_id")
            if not prompt_id:
                raise ComfyUIError(f"ComfyUI did not return a prompt_id: {data}")
            return prompt_id

    async def cancel_prompt(self, prompt_id: str) -> bool:
        """POST /api/jobs/{prompt_id}/cancel -- atomically interrupts this
        prompt if it's currently executing, or dequeues it if still pending
        in ComfyUI's own queue. Idempotent: returns False (not an error) if
        the prompt already finished or is unknown to ComfyUI.

        Verified against the actual installed ComfyUI's server.py +
        comfy_execution/jobs.py, not assumed from general docs -- this is a
        newer, purpose-built cancel-by-id endpoint, deliberately used instead
        of the legacy global POST /interrupt: that endpoint's own prompt_id
        check isn't atomic with the interrupt itself (a plain scan of the
        currently-running list, then an unconditional global
        nodes.interrupt_processing() call), so it can race a prompt that
        finishes in between. This endpoint's interrupt path
        (PromptQueue.interrupt_if_running) is signalled under the same mutex
        that moves a job from running to done, closing that race.
        """
        async with httpx.AsyncClient(timeout=10) as client:
            try:
                resp = await client.post(f"{self._settings.comfyui_http_url}/api/jobs/{prompt_id}/cancel")
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise ComfyUIError(f"ComfyUI rejected cancel for prompt {prompt_id}: {exc.response.text}") from exc
            except httpx.HTTPError as exc:
                raise ComfyUIError(f"Could not reach ComfyUI at {self._settings.comfyui_http_url}") from exc
            return resp.json().get("cancelled", False)

    async def wait_for_completion(
        self,
        prompt_id: str,
        on_progress: Optional[Callable[[dict[str, Any]], None]] = None,
        history_poll_interval_seconds: float = 3.0,
    ) -> dict[str, Any]:
        """Blocks (async) until ComfyUI finishes executing `prompt_id`.

        Streams over the shared client WebSocket rather than polling
        `/history`, so progress callbacks fire in near real time.
        """
        ws_url = self._settings.comfyui_ws_url
        try:
            # ComfyUI's execution is synchronous/blocking on its event loop,
            # so a long generation (observed: 55s+ for a single AnimateDiff
            # shot) can starve it long enough to miss the websockets
            # library's default 20s ping/pong keepalive -- killing the
            # connection mid-generation even though ComfyUI is still working
            # fine. Disabling client-initiated pings avoids that false
            # positive; a truly dead connection still surfaces via recv()
            # raising, and GENERATION_TIMEOUT_SECONDS bounds the overall wait.
            async with websockets.connect(ws_url, max_size=None, ping_interval=None) as ws:
                while True:
                    raw = await ws.recv()
                    if isinstance(raw, (bytes, bytearray)):
                        continue  # binary preview/latent frames, not needed here

                    message = json.loads(raw)
                    data = message.get("data", {})

                    if data.get("prompt_id") and data.get("prompt_id") != prompt_id:
                        continue

                    if on_progress:
                        on_progress(message)

                    if message.get("type") == "execution_error":
                        raise ComfyUIError(f"ComfyUI execution error for {prompt_id}: {data}")

                    if message.get("type") == "execution_interrupted":
                        raise ComfyUIInterrupted(f"ComfyUI prompt {prompt_id} was interrupted: {data}")

                    # ComfyUI emits an "executing" event with node=None once
                    # the whole prompt graph has finished.
                    if message.get("type") == "executing" and data.get("node") is None:
                        return await self.get_history(prompt_id)
        except (websockets.exceptions.WebSocketException, OSError) as exc:
            # Confirmed via a real run (see TODO.md BUG-1): this fires even
            # though the client disables its own keepalive pings above --
            # most likely ComfyUI's own WebSocket server failing its side of
            # the keepalive while its event loop is blocked mid-generation.
            # Either way, the prompt itself keeps executing inside ComfyUI
            # regardless of whether this monitoring connection stays up (the
            # generation was never tied to the socket), so a dropped
            # connection isn't proof the job failed -- fall back to polling
            # `/history` for the real outcome instead of failing the whole
            # storyboard job over a monitoring-channel hiccup. Bounded by the
            # caller's GENERATION_TIMEOUT_SECONDS wrap (asyncio.wait_for in
            # storyboard.py), not by anything in this method.
            logger.warning(
                "WebSocket to ComfyUI dropped while waiting for prompt %s (%s); "
                "falling back to polling /history -- the job may still be running.",
                prompt_id,
                exc,
            )
            if on_progress:
                on_progress({"type": "ws_dropped_fallback_polling", "prompt_id": prompt_id, "detail": str(exc)})
            return await self._poll_history_until_done(prompt_id, history_poll_interval_seconds)

    async def _poll_history_until_done(self, prompt_id: str, poll_interval_seconds: float) -> dict[str, Any]:
        """Polls `/history/{prompt_id}` until ComfyUI records a finished entry.

        ComfyUI only adds a prompt to `/history` once it has finished
        executing (success or error) -- get_history() raises ComfyUIError
        while it's still absent, which this treats as "not done yet" and
        retries. A genuinely dead ComfyUI surfaces as an httpx error instead
        (not a ComfyUIError) and propagates immediately rather than retrying
        for the full timeout window.
        """
        while True:
            try:
                history = await self.get_history(prompt_id)
            except ComfyUIError:
                await asyncio.sleep(poll_interval_seconds)
                continue
            _raise_if_history_incomplete(history, prompt_id)
            return history

    async def get_history(self, prompt_id: str) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(f"{self._settings.comfyui_http_url}/history/{prompt_id}")
            resp.raise_for_status()
            history = resp.json()
            if prompt_id not in history:
                raise ComfyUIError(f"ComfyUI has no history entry for prompt {prompt_id}")
            return history[prompt_id]

    async def fetch_output_bytes(self, filename: str, subfolder: str, folder_type: str) -> bytes:
        params = {"filename": filename, "subfolder": subfolder, "type": folder_type}
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.get(f"{self._settings.comfyui_http_url}/view", params=params)
            resp.raise_for_status()
            return resp.content
