"""Tests for backend/api/routes/generate_script.py -- previously untested
entirely (no test file existed for this module despite it being one of the
two core API endpoints). Calls the job handler and route function directly
with monkeypatched store/LLM client/worker, matching this repo's existing
convention (tests/test_job_routes.py) rather than a TestClient-based harness.
"""

import asyncio
from unittest.mock import AsyncMock

from backend.api.routes import generate_script as generate_script_module
from backend.core.config import Settings
from backend.core.job_store import JobStore
from backend.core.worker import Job, JobStatus
from backend.models.schemas import ScriptGenerationRequest


def _store(tmp_path) -> JobStore:
    return JobStore(Settings(db_path=str(tmp_path / "jobs.db")))


def test_run_generate_script_job_persists_a_log_entry_and_returns_the_script(tmp_path, monkeypatch):
    store = _store(tmp_path)
    monkeypatch.setattr(generate_script_module, "get_job_store", lambda: store)
    monkeypatch.setattr(
        generate_script_module._llm_client, "generate_script", AsyncMock(return_value="a lighthouse at dawn.")
    )

    async def scenario():
        await store.create_job("job-1", "generate_script", "running", {}, created_at=1.0)
        job = Job(
            id="job-1", kind="generate_script",
            payload={"prompt": "a lighthouse story", "target_duration_seconds": 30, "tone": "moody"},
        )
        result = await generate_script_module._run_generate_script_job(job)
        logs = await store.get_job_logs("job-1")
        return result, logs

    result, logs = asyncio.run(scenario())

    assert result == {"script": "a lighthouse at dawn."}
    generate_script_module._llm_client.generate_script.assert_awaited_once_with(
        "a lighthouse story", 30, "moody",
    )
    assert any("generated script" in entry["message"] for entry in logs)


def test_generate_script_route_submits_a_job_and_returns_its_id(monkeypatch):
    fake_job = Job(
        id="job-1", kind="generate_script",
        payload={"prompt": "x", "target_duration_seconds": 30, "tone": "neutral"},
        status=JobStatus.QUEUED,
    )
    monkeypatch.setattr(generate_script_module.worker, "submit", AsyncMock(return_value=fake_job))

    request = ScriptGenerationRequest(prompt="a robot learns to dance", target_duration_seconds=45, tone="comedic")

    response = asyncio.run(generate_script_module.generate_script(request))

    assert response.job_id == "job-1"
    assert response.status == "queued"
    generate_script_module.worker.submit.assert_awaited_once_with(
        "generate_script",
        {"prompt": "a robot learns to dance", "target_duration_seconds": 45, "tone": "comedic"},
        project_id=None,
    )


def test_generate_script_route_passes_project_id_through(monkeypatch):
    fake_job = Job(id="job-1", kind="generate_script", payload={}, status=JobStatus.QUEUED)
    monkeypatch.setattr(generate_script_module.worker, "submit", AsyncMock(return_value=fake_job))

    request = ScriptGenerationRequest(prompt="x", project_id="lighthouse-series")

    asyncio.run(generate_script_module.generate_script(request))

    _, kwargs = generate_script_module.worker.submit.call_args
    assert kwargs["project_id"] == "lighthouse-series"
