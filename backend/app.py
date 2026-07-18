"""ManyTV backend entrypoint.

Run with (from the project root, backend venv active):
    uvicorn backend.app:app --reload --port 8000

This process orchestrates a locally running ComfyUI instance over HTTP +
WebSocket — it never loads a diffusion/video model itself. Start ComfyUI
first with scripts/run_comfyui.ps1 (tuned for an 8GB Intel Arc A750).
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.api.routes import generate_script, storyboard
from backend.core.comfyui_client import ComfyUIClient
from backend.core.config import get_settings
from backend.core.job_store import get_job_store
from backend.core.recovery import resume_incomplete_jobs
from backend.core.worker import worker

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("manytv.app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    worker.start()

    client = ComfyUIClient(settings)
    if await client.health_check():
        logger.info("Connected to ComfyUI at %s", settings.comfyui_http_url)
    else:
        logger.warning(
            "ComfyUI not reachable at %s yet -- start it with scripts/run_comfyui.ps1 "
            "before submitting a /api/storyboard job.",
            settings.comfyui_http_url,
        )

    await resume_incomplete_jobs(worker, get_job_store(), client)

    yield

    await worker.stop()


app = FastAPI(title="ManyTV", version="0.1.0", lifespan=lifespan)

# See BUG-3 in TODO.md: this used to be allow_origins=["*"] with no auth on
# any route, which let any page open in the user's browser hit this API.
# Fix for this single-machine, no-frontend-yet deployment: an empty
# allowlist by default (settings.cors_allowed_origins) plus binding uvicorn
# to 127.0.0.1 (see README "Running") -- nothing but this machine's own
# tooling can reach the API at all, so no separate auth layer is needed on
# top of that. Set CORS_ALLOWED_ORIGINS in .env once a real frontend exists.
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_allowed_origins_list,
    allow_methods=["GET", "POST"],
    # Last-Event-ID isn't a CORS-safelisted header -- EventSource sends it
    # automatically on reconnect (see the SSE routes in storyboard.py), and
    # without it listed here that reconnect gets CORS-preflight-rejected.
    # Breaks silently only on reconnect, never on first connect (which sends
    # no such header) -- e.g. the first time `uvicorn --reload` restarts
    # mid-job.
    allow_headers=["Content-Type", "Last-Event-ID"],
)

app.include_router(generate_script.router)
app.include_router(storyboard.router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
