"""Central configuration, loaded from environment variables / .env.

Only ComfyUI connection details and pipeline safety limits live here — no
model paths or GPU settings, since the FastAPI process never loads a model
itself (see backend/core/comfyui_client.py).
"""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    comfyui_host: str = "127.0.0.1"
    comfyui_port: int = 8188
    comfyui_client_id: str = "manytv-backend"

    output_dir: str = "output"
    workflow_dir: str = "backend/workflows"
    # SQLite file backing persistent job/shot state (backend/core/job_store.py).
    # Lives under output_dir by default -- that's already this project's one
    # convention for generated/runtime state that isn't source.
    db_path: str = "output/jobs.db"

    generation_timeout_seconds: int = 600
    max_shots_per_job: int = 50

    # Age (in days) past which a terminal (done/failed/cancelled) job's DB
    # rows and output/<job_id>/ files are pruned at backend startup (see
    # backend/core/retention.py). 0 (default) disables pruning entirely --
    # this is a destructive, irreversible operation, so it's opt-in rather
    # than silently active. queued/running/resuming/cancelling jobs are
    # never eligible regardless of age.
    job_retention_days: int = 0

    # Comma-separated list of origins allowed to make cross-origin browser
    # requests (CORS). Empty by default: this deployment is a single local
    # machine with no frontend yet, so there's no legitimate cross-origin
    # caller to allow, and an empty allowlist means any other page open in
    # the user's browser (or a wildcard) can't get this API to honor its
    # preflight -- see BUG-3 in TODO.md for why that mattered in practice.
    # Once a real frontend exists, set this to its origin(s), e.g.
    # "http://127.0.0.1:5173,http://localhost:5173" for a local Vite dev server.
    cors_allowed_origins: str = ""

    # OpenAI-compatible chat endpoint for /api/generate-script. Defaults to
    # a local Ollama instance's OpenAI-compat shim; point this at any other
    # OpenAI-compatible provider by changing base_url/api_key/model, no code
    # changes needed.
    llm_base_url: str = "http://127.0.0.1:11434/v1"
    llm_api_key: str = "ollama"
    llm_model: str = "qwen3:4b"
    # Reasoning models can spend a couple of minutes "thinking" before the
    # actual answer -- 120s was empirically too tight for qwen3:4b and
    # caused the openai SDK to silently retry a still-in-flight request.
    llm_timeout_seconds: int = 300

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @property
    def comfyui_http_url(self) -> str:
        return f"http://{self.comfyui_host}:{self.comfyui_port}"

    @property
    def comfyui_ws_url(self) -> str:
        return f"ws://{self.comfyui_host}:{self.comfyui_port}/ws?clientId={self.comfyui_client_id}"

    @property
    def output_path(self) -> Path:
        return Path(self.output_dir)

    @property
    def workflow_path(self) -> Path:
        return Path(self.workflow_dir)

    @property
    def db_file_path(self) -> Path:
        return Path(self.db_path)

    @property
    def cors_allowed_origins_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_allowed_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
