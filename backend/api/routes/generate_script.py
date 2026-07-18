"""POST /api/generate-script

Queues a script-generation job on the same single-slot worker as
/api/storyboard, rather than blocking the request for the 30s-3min a local
"thinking" model can take. This isn't just about avoiding an HTTP timeout:
on this machine Ollama loads qwen3:4b 100% onto the Arc A750 GPU (confirmed
via `ollama ps` during a real request), sharing the same 8GB VRAM budget
ComfyUI generation uses. Serializing script and video jobs through one
worker prevents the two processes from contending for VRAM concurrently --
the same anti-OOM rationale that caps video generation at one job at a
time.
"""

import logging
from typing import Any

from fastapi import APIRouter

from backend.core.config import get_settings
from backend.core.job_store import get_job_store
from backend.core.llm_client import LLMClient
from backend.core.worker import Job, LogLevel, LogStage, worker
from backend.models.schemas import ScriptGenerationRequest, ScriptGenerationResponse

logger = logging.getLogger("manytv.api.script")
router = APIRouter(prefix="/api", tags=["script"])

_llm_client = LLMClient(get_settings())


async def _run_generate_script_job(job: Job) -> dict[str, Any]:
    payload = job.payload
    text = await _llm_client.generate_script(
        payload["prompt"], payload["target_duration_seconds"], payload["tone"]
    )
    logger.info("Job %s: generated script (%d chars).", job.id, len(text))
    await get_job_store().add_job_log(
        job.id, LogLevel.INFO.value, LogStage.JOB.value,
        f"Job {job.id}: generated script ({len(text)} chars).",
    )
    return {"script": text}


worker.register_handler("generate_script", _run_generate_script_job)


@router.post("/generate-script", response_model=ScriptGenerationResponse)
async def generate_script(request: ScriptGenerationRequest) -> ScriptGenerationResponse:
    job = await worker.submit(
        "generate_script",
        {
            "prompt": request.prompt,
            "target_duration_seconds": request.target_duration_seconds,
            "tone": request.tone,
        },
        project_id=request.project_id,
    )
    return ScriptGenerationResponse(job_id=job.id, status=job.status.value)
