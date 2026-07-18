"""Shot-splitting, workflow templating, and the storyboard job-execution engine.

Split out of backend/api/routes/storyboard.py (v1.2 Phase 3) -- this module
owns the actual work of turning a script into shots and driving ComfyUI
through them, with no FastAPI dependency, matching every other
backend/core/*.py module's style.

Shot state is persisted via backend/core/job_store.py at two points per
shot (submitted, then done/failed) -- not on every WebSocket progress tick,
see job_store.py's module docstring for why. _run_storyboard_job() always
reads its shot list from the store rather than always starting a blank list
at index 0, which is what makes it double as the resume path: a fresh job
just has no persisted shots yet (bootstrapped as PENDING here), while a job
resumed after a backend restart (see backend/core/recovery.py) has some
shots already DONE/SUBMITTED from before the restart. Same code, no
separate resume implementation to keep in sync.

Note for future readers of tests/test_recovery.py: its shot-level
reconciliation tests patch this module's `comfyui_client`, `get_settings`,
`get_job_store`, and `_load_workflow` by name (monkeypatch) -- a real
coupling `_run_storyboard_job` depends on via bare-name lookups in its own
module globals, not incidental to where this code happens to live.
"""

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any, Optional

from backend.core.comfyui_client import ComfyUIClient, ComfyUIError
from backend.core.config import Settings, get_settings
from backend.core.job_store import get_job_store
from backend.core.worker import Job, JobCancelled, JobStatus, LogLevel, LogStage, ShotStatus, worker
from backend.models.schemas import StoryboardShot

logger = logging.getLogger("manytv.api.storyboard")

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
    store = get_job_store()
    payload = job.payload
    shots_by_index = {s["index"]: StoryboardShot(**s) for s in payload["shots"]}
    workflow_template = _load_workflow(payload["workflow_name"], settings)

    if not await comfyui_client.health_check():
        raise ComfyUIError(
            f"ComfyUI is not reachable at {settings.comfyui_http_url}. "
            "Start it first with scripts/run_comfyui.ps1."
        )

    shot_rows = await store.get_shots(job.id)
    if not shot_rows:
        # Fresh job -- nothing persisted yet, so this *is* the initial state.
        for index in sorted(shots_by_index):
            await store.upsert_shot(job.id, index, ShotStatus.PENDING.value)
        shot_rows = await store.get_shots(job.id)

    results = []
    for shot_row in shot_rows:
        # Checked at the top of every iteration -- including ones about to
        # be DONE-skipped below -- so a /cancel request is noticed between
        # any two shots, not just before the next one that does real work.
        # Only takes effect between shots: a shot already SUBMITTED to
        # ComfyUI keeps running to completion (bounded by
        # GENERATION_TIMEOUT_SECONDS) since there's no ComfyUI /interrupt
        # call here -- see TODO.md for why that's a deliberate scope limit.
        current_job_row = await store.get_job(job.id)
        if current_job_row is not None and current_job_row["status"] == JobStatus.CANCELLING.value:
            raise JobCancelled(f"Job {job.id} cancelled during shot processing.")

        shot_index = shot_row["shot_index"]
        shot = shots_by_index[shot_index]

        if shot_row["status"] == ShotStatus.DONE.value:
            # Already completed in a prior process -- trust it rather than
            # re-checking ComfyUI, since a DONE row is only ever written
            # after its files are confirmed on disk (see job_store.py).
            files = json.loads(shot_row["files"]) if shot_row["files"] else []
            results.append({"shot_index": shot_index, "prompt_id": shot_row["prompt_id"], "files": files})
            continue

        prompt_id: Optional[str] = None
        history: Optional[dict[str, Any]] = None

        if shot_row["status"] == ShotStatus.SUBMITTED.value and shot_row["prompt_id"]:
            # Resumed mid-flight: this shot was queued to ComfyUI before the
            # backend last stopped, but its outcome was never confirmed.
            # ComfyUI keeps executing independently of this backend's
            # lifetime (see BUG-1), so check whether it actually finished
            # before assuming it's lost.
            try:
                history = await comfyui_client.get_history(shot_row["prompt_id"])
                prompt_id = shot_row["prompt_id"]
                logger.info(
                    "Job %s shot %d: found existing ComfyUI history for prompt %s, reusing it "
                    "instead of resubmitting.", job.id, shot_index, prompt_id,
                )
                await store.add_job_log(
                    job.id, LogLevel.INFO.value, LogStage.RESUME.value,
                    f"Job {job.id} shot {shot_index}: found existing ComfyUI history for prompt "
                    f"{prompt_id}, reusing it instead of resubmitting.",
                    shot_index=shot_index,
                )
            except ComfyUIError:
                # ComfyUI has no memory of this prompt (it was likely
                # restarted independently of the backend) -- safe to
                # resubmit fresh below. Anything other than ComfyUIError
                # here (e.g. ComfyUI simply unreachable right now) is left
                # to propagate: resubmitting without knowing the original
                # prompt's fate could silently produce a different result
                # than one the caller may already be relying on.
                logger.info(
                    "Job %s shot %d: prompt %s has no ComfyUI history; resubmitting.",
                    job.id, shot_index, shot_row["prompt_id"],
                )
                await store.add_job_log(
                    job.id, LogLevel.INFO.value, LogStage.RESUME.value,
                    f"Job {job.id} shot {shot_index}: prompt {shot_row['prompt_id']} has no ComfyUI "
                    "history; resubmitting.",
                    shot_index=shot_index,
                )

        if history is None:
            workflow = _apply_shot_to_workflow(workflow_template, shot)
            prompt_id = await comfyui_client.queue_prompt(workflow)
            await store.upsert_shot(
                job.id, shot_index, ShotStatus.SUBMITTED.value,
                prompt_id=prompt_id, submitted_at=time.time(),
            )
            logger.info("Job %s: shot %d queued as ComfyUI prompt %s", job.id, shot_index, prompt_id)
            await store.add_job_log(
                job.id, LogLevel.INFO.value, LogStage.SHOT.value,
                f"Job {job.id}: shot {shot_index} queued as ComfyUI prompt {prompt_id}",
                shot_index=shot_index,
            )

            # comfyui_client.py stays job-agnostic (it only ever sees a bare
            # prompt_id, never a job_id/shot_index or JobStore) -- so its
            # WS-drop-falls-back-to-polling warning can't persist itself.
            # on_progress relays it here as a synthetic message; _progress
            # stays synchronous (matching on_progress's existing contract,
            # unchanged) and only *captures* it plus the moment it actually
            # happened, deferring the actual async add_job_log call to the
            # finally block below, after wait_for_completion resolves.
            pending_log_events: list[tuple[str, str, str, float]] = []

            def _progress(message: dict[str, Any], shot_index: int = shot_index) -> None:
                if message.get("type") == "progress":
                    data = message["data"]
                    logger.info(
                        "Job %s shot %d: step %s/%s", job.id, shot_index, data.get("value"), data.get("max")
                    )
                elif message.get("type") == "ws_dropped_fallback_polling":
                    pending_log_events.append(
                        (LogLevel.WARNING.value, LogStage.COMFYUI.value, message["detail"], time.time())
                    )

            try:
                history = await asyncio.wait_for(
                    comfyui_client.wait_for_completion(prompt_id, on_progress=_progress),
                    timeout=settings.generation_timeout_seconds,
                )
            except asyncio.TimeoutError as exc:
                error_msg = (
                    f"Shot {shot_index} (prompt {prompt_id}) did not finish within "
                    f"{settings.generation_timeout_seconds}s."
                )
                await store.upsert_shot(job.id, shot_index, ShotStatus.FAILED.value, error=error_msg, finished_at=time.time())
                await store.add_job_log(
                    job.id, LogLevel.ERROR.value, LogStage.SHOT.value, error_msg, shot_index=shot_index,
                )
                raise ComfyUIError(error_msg) from exc
            except Exception as exc:
                await store.upsert_shot(job.id, shot_index, ShotStatus.FAILED.value, error=str(exc), finished_at=time.time())
                await store.add_job_log(
                    job.id, LogLevel.ERROR.value, LogStage.SHOT.value, str(exc), shot_index=shot_index,
                )
                raise
            finally:
                for level, stage, message, event_timestamp in pending_log_events:
                    await store.add_job_log(
                        job.id, level, stage, message, shot_index=shot_index, timestamp=event_timestamp,
                    )

        shot_dir = settings.output_path / job.id / f"shot_{shot_index:03d}"
        saved_files = await _save_history_outputs(comfyui_client, history, shot_dir)
        await store.upsert_shot(job.id, shot_index, ShotStatus.DONE.value, files=saved_files, finished_at=time.time())
        logger.info("Job %s shot %d done: %d file(s) saved to %s", job.id, shot_index, len(saved_files), shot_dir)
        await store.add_job_log(
            job.id, LogLevel.INFO.value, LogStage.SHOT.value,
            f"Job {job.id} shot {shot_index} done: {len(saved_files)} file(s) saved to {shot_dir}",
            shot_index=shot_index,
        )
        results.append({"shot_index": shot_index, "prompt_id": prompt_id, "files": saved_files})

    return {"shots": results}


worker.register_handler("storyboard", _run_storyboard_job)
