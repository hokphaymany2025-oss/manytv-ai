"""Thin async client for a locally running ComfyUI instance.

ManyTV never loads a diffusion/video model itself. Every generation is a
prompt graph POSTed to ComfyUI's `/prompt` endpoint, tracked to completion
over its `/ws` WebSocket, then pulled back via `/history` + `/view`. This
keeps model loading, VRAM scheduling (--lowvram/--gpu-only) and node
execution entirely inside ComfyUI's own process, where it belongs.
"""

import json
import logging
from typing import Any, Callable, Optional

import httpx
import websockets

from backend.core.config import Settings

logger = logging.getLogger("manytv.comfyui")


class ComfyUIError(RuntimeError):
    """Raised when ComfyUI is unreachable or reports a failed execution."""


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

    async def wait_for_completion(
        self,
        prompt_id: str,
        on_progress: Optional[Callable[[dict[str, Any]], None]] = None,
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

                    # ComfyUI emits an "executing" event with node=None once
                    # the whole prompt graph has finished.
                    if message.get("type") == "executing" and data.get("node") is None:
                        break
        except (websockets.exceptions.WebSocketException, OSError) as exc:
            raise ComfyUIError(f"Lost connection to ComfyUI WebSocket at {ws_url}") from exc

        return await self.get_history(prompt_id)

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
