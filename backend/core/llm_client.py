"""OpenAI-compatible chat client for script generation.

Defaults to a local Ollama instance's OpenAI-compat shim
(http://127.0.0.1:11434/v1) using the official `openai` SDK -- Ollama
accepts any non-empty string as the API key. Repointing LLM_BASE_URL /
LLM_API_KEY / LLM_MODEL in .env at a hosted OpenAI-compatible provider
instead requires no code changes.

On this machine, Ollama loads qwen3:4b 100% onto the Arc A750 GPU
(confirmed via `ollama ps` mid-request) -- the same 8GB VRAM budget
ComfyUI's video generation uses. See backend/api/routes/generate_script.py
for why script generation is routed through the same single-slot worker as
video jobs rather than running independently.
"""

import logging
import re

from openai import APIConnectionError, APIStatusError, AsyncOpenAI

from backend.core.config import Settings

logger = logging.getLogger("manytv.llm")

SYSTEM_PROMPT = (
    "You are a video scriptwriter. Write a shot-by-shot script for a short "
    "video. Output ONLY the script: one shot's visual description per line, "
    "plain text, no numbering, no markdown formatting, no headers or "
    "commentary. Each line will be used verbatim as a text-to-video "
    "generation prompt, so keep every line concrete and visual -- describe "
    "what is on screen, not narration or dialogue."
)

# Reasoning models (e.g. Qwen3's default "thinking" mode) usually return
# their chain-of-thought in a separate `reasoning` field via Ollama's
# OpenAI-compat shim, keeping `content` clean -- but tested against Ollama
# 0.32.0, the native /api/chat endpoint inlines it as literal
# <think>...</think> tags in `content` instead, and the `think: false`
# request flag did not suppress it for qwen3 on that version. Stripped
# defensively so a leak either way can't end up in the script.
_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


class LLMError(RuntimeError):
    """Raised when the LLM endpoint is unreachable or returns an error."""


class LLMClient:
    def __init__(self, settings: Settings):
        self._settings = settings
        self._client = AsyncOpenAI(base_url=settings.llm_base_url, api_key=settings.llm_api_key)

    async def generate_script(self, prompt: str, target_duration_seconds: int, tone: str) -> str:
        user_message = (
            f"Topic: {prompt}\n"
            f"Target duration: ~{target_duration_seconds} seconds\n"
            f"Tone: {tone}\n\n"
            "Write the shot list now."
        )
        try:
            completion = await self._client.chat.completions.create(
                model=self._settings.llm_model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_message},
                ],
                timeout=self._settings.llm_timeout_seconds,
            )
        except APIConnectionError as exc:
            raise LLMError(
                f"Could not reach the LLM at {self._settings.llm_base_url}. "
                "If using Ollama, make sure it's running and the model is "
                f"pulled (`ollama pull {self._settings.llm_model}`)."
            ) from exc
        except APIStatusError as exc:
            raise LLMError(f"LLM endpoint returned an error ({exc.status_code}): {exc.message}") from exc

        raw_text = completion.choices[0].message.content or ""
        text = _THINK_BLOCK_RE.sub("", raw_text).strip()
        if not text:
            raise LLMError("LLM returned an empty script.")
        return text
