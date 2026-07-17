"""POST /api/storyboard

Splits a script into shots (or accepts an explicit shot list), then queues
one image-to-video ComfyUI job per shot through the single-slot worker so
only one generation ever runs at a time on the 8GB A750. Generated files are
downloaded from ComfyUI and saved under OUTPUT_DIR/<job_id>/shot_NNN/.
"""

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

from backend.core.comfyui_client import ComfyUIClient, ComfyUIError
from backend.core.config import Settings, get_settings
from backend.core.worker import Job, worker
from backend.models.schemas import (
    JobStatusResponse,
    StoryboardRequest,
    StoryboardResponse,
    StoryboardShot,
)

logger = logging.getLogger("manytv.api.storyboard")
router = APIRouter(prefix="/api", tags=["storyboard"])

settings = get_settings()
comfyui_client = ComfyUIClient(settings)


def _naive_shot_split(script: str) -> list[StoryboardShot]:
    lines = [line.strip() for line in script.splitlines() if line.strip()]
    return [StoryboardShot(index=i, description=line, prompt=line) for i, line in enumerate(lines)]


def _load_workflow(workflow_name: str, settings: Settings) -> dict[str, Any]:
    path = settings.workflow_path / f"{workflow_name}.json"
    if not path.exists():
        raise ComfyUIError(
            f"Workflow '{workflow_name}' not found at {path}. Export one from the ComfyUI UI "
            "(Dev Mode enabled > 'Save (API Format)') and drop the JSON there — "
            "see backend/workflows/README.md."
        )
    return json.loads(path.read_text(encoding="utf-8"))


def _apply_shot_to_workflow(workflow: dict[str, Any], shot: StoryboardShot) -> dict[str, Any]:
    """Fills CLIPTextEncode nodes titled "Positive"/"Negative" with the shot's prompt text.

    Adjust the title matching here if your exported workflow names its nodes differently.
    """
    patched = json.loads(json.dumps(workflow))  # deep copy
    for node in patched.values():
        if node.get("class_type") != "CLIPTextEncode":
            continue
        title = node.get("_meta", {}).get("title", "").strip().lower()
        if title == "positive":
            node["inputs"]["text"] = shot.prompt
        elif title == "negative" and shot.negative_prompt:
            # Only override if the shot actually specifies one -- otherwise
            # keep whatever default negative prompt the workflow JSON ships
            # with, rather than clobbering it with an empty string.
            node["inputs"]["text"] = shot.negative_prompt
    return patched


async def _save_history_outputs(client: ComfyUIClient, history: dict[str, Any], dest_dir: Path) -> list[str]:
    """Downloads every file ComfyUI produced for this prompt (images, gifs, videos, ...)."""
    saved: list[str] = []
    for node_output in history.get("outputs", {}).values():
        for entries in node_output.values():
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict) or "filename" not in entry:
                    continue
                data = await client.fetch_output_bytes(
                    entry["filename"], entry.get("subfolder", ""), entry.get("type", "output")
                )
                dest_dir.mkdir(parents=True, exist_ok=True)
                out_path = dest_dir / entry["filename"]
                out_path.write_bytes(data)
                saved.append(str(out_path))
    return saved


async def _run_storyboard_job(job: Job) -> dict[str, Any]:
    settings = get_settings()
    payload = job.payload
    shots = [StoryboardShot(**s) for s in payload["shots"]]
    workflow_template = _load_workflow(payload["workflow_name"], settings)

    if not await comfyui_client.health_check():
        raise ComfyUIError(
            f"ComfyUI is not reachable at {settings.comfyui_http_url}. "
            "Start it first with scripts/run_comfyui.ps1."
        )

    results = []
    for shot in shots:
        workflow = _apply_shot_to_workflow(workflow_template, shot)
        prompt_id = await comfyui_client.queue_prompt(workflow)
        logger.info("Job %s: shot %d queued as ComfyUI prompt %s", job.id, shot.index, prompt_id)

        def _progress(message: dict[str, Any], shot_index: int = shot.index) -> None:
            if message.get("type") == "progress":
                data = message["data"]
                logger.info("Job %s shot %d: step %s/%s", job.id, shot_index, data.get("value"), data.get("max"))

        try:
            history = await asyncio.wait_for(
                comfyui_client.wait_for_completion(prompt_id, on_progress=_progress),
                timeout=settings.generation_timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            raise ComfyUIError(
                f"Shot {shot.index} (prompt {prompt_id}) did not finish within "
                f"{settings.generation_timeout_seconds}s."
            ) from exc

        shot_dir = settings.output_path / job.id / f"shot_{shot.index:03d}"
        saved_files = await _save_history_outputs(comfyui_client, history, shot_dir)
        logger.info("Job %s shot %d done: %d file(s) saved to %s", job.id, shot.index, len(saved_files), shot_dir)
        results.append({"shot_index": shot.index, "prompt_id": prompt_id, "files": saved_files})

    return {"shots": results}


worker.register_handler("storyboard", _run_storyboard_job)


@router.post("/storyboard", response_model=StoryboardResponse)
async def create_storyboard(request: StoryboardRequest) -> StoryboardResponse:
    settings = get_settings()
    shots = request.shots or _naive_shot_split(request.script)
    if not shots:
        raise HTTPException(status_code=400, detail="Script produced no shots to storyboard.")
    if len(shots) > settings.max_shots_per_job:
        raise HTTPException(
            status_code=400,
            detail=f"{len(shots)} shots exceeds MAX_SHOTS_PER_JOB ({settings.max_shots_per_job}).",
        )

    job = await worker.submit(
        "storyboard",
        {"shots": [s.model_dump() for s in shots], "workflow_name": request.workflow_name},
    )
    return StoryboardResponse(job_id=job.id, status=job.status.value, shot_count=len(shots))


@router.get("/jobs/{job_id}", response_model=JobStatusResponse)
async def get_job_status(job_id: str) -> JobStatusResponse:
    job = worker.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    return JobStatusResponse(id=job.id, kind=job.kind, status=job.status.value, result=job.result, error=job.error)
