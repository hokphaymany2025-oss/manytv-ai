"""Direct unit tests for backend/core/llm_client.py's LLMClient.generate_script
-- previously untested entirely (this whole module had zero direct test
coverage). The underlying AsyncOpenAI client is swapped out at the
`_client.chat.completions.create` boundary with a hand-rolled fake, matching
this repo's existing convention (tests/test_comfyui_client.py monkeypatches
a single bound method on a real instance rather than mocking a whole
library) -- no live Ollama/OpenAI-compatible endpoint needed.
"""

import asyncio

import httpx
import pytest
from openai import APIConnectionError, APIStatusError

from backend.core.config import Settings
from backend.core.llm_client import LLMClient, LLMError


class _FakeMessage:
    def __init__(self, content):
        self.content = content


class _FakeChoice:
    def __init__(self, content):
        self.message = _FakeMessage(content)


class _FakeCompletion:
    def __init__(self, content):
        self.choices = [_FakeChoice(content)]


def _client() -> LLMClient:
    return LLMClient(Settings())


def test_generate_script_returns_stripped_completion_text(monkeypatch):
    client = _client()

    async def fake_create(**kwargs):
        return _FakeCompletion("  a lighthouse at dawn.\nwaves crash below.  ")

    monkeypatch.setattr(client._client.chat.completions, "create", fake_create)

    result = asyncio.run(client.generate_script("a lighthouse story", 30, "moody"))

    assert result == "a lighthouse at dawn.\nwaves crash below."


def test_generate_script_sends_prompt_duration_and_tone_in_the_user_message(monkeypatch):
    client = _client()
    captured = {}

    async def fake_create(**kwargs):
        captured.update(kwargs)
        return _FakeCompletion("a script")

    monkeypatch.setattr(client._client.chat.completions, "create", fake_create)

    asyncio.run(client.generate_script("a robot learns to dance", 45, "comedic"))

    user_message = captured["messages"][1]["content"]
    assert "Topic: a robot learns to dance" in user_message
    assert "Target duration: ~45 seconds" in user_message
    assert "Tone: comedic" in user_message


def test_generate_script_strips_think_blocks_from_the_response(monkeypatch):
    """Ollama's native endpoint was observed inlining Qwen3's reasoning as a
    literal <think>...</think> block in `content` -- see this module's own
    top-of-file comment for the full story. Stripped defensively regardless
    of which endpoint/version is talking."""
    client = _client()

    async def fake_create(**kwargs):
        return _FakeCompletion("<think>reasoning about the plot here</think>the actual script line")

    monkeypatch.setattr(client._client.chat.completions, "create", fake_create)

    result = asyncio.run(client.generate_script("x", 30, "neutral"))

    assert result == "the actual script line"
    assert "<think>" not in result


def test_generate_script_raises_llm_error_when_result_is_empty_after_stripping(monkeypatch):
    """A response that's nothing but a <think> block (or genuinely empty
    content) must not silently return an empty script."""
    client = _client()

    async def fake_create(**kwargs):
        return _FakeCompletion("<think>only reasoning, no actual script</think>")

    monkeypatch.setattr(client._client.chat.completions, "create", fake_create)

    with pytest.raises(LLMError, match="empty script"):
        asyncio.run(client.generate_script("x", 30, "neutral"))


def test_generate_script_raises_llm_error_on_connection_failure(monkeypatch):
    client = _client()
    request = httpx.Request("POST", "http://127.0.0.1:11434/v1/chat/completions")

    async def fake_create(**kwargs):
        raise APIConnectionError(request=request)

    monkeypatch.setattr(client._client.chat.completions, "create", fake_create)

    with pytest.raises(LLMError, match="Could not reach the LLM"):
        asyncio.run(client.generate_script("x", 30, "neutral"))


def test_generate_script_raises_llm_error_on_api_status_error(monkeypatch):
    client = _client()
    request = httpx.Request("POST", "http://127.0.0.1:11434/v1/chat/completions")
    response = httpx.Response(500, request=request)

    async def fake_create(**kwargs):
        raise APIStatusError("internal server error", response=response, body=None)

    monkeypatch.setattr(client._client.chat.completions, "create", fake_create)

    with pytest.raises(LLMError, match="returned an error \\(500\\)"):
        asyncio.run(client.generate_script("x", 30, "neutral"))
