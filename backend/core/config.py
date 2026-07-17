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


@lru_cache
def get_settings() -> Settings:
    return Settings()
