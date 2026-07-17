"""Pydantic request/response models for the API layer."""

from typing import Any, Optional

from pydantic import BaseModel, Field


class ScriptGenerationRequest(BaseModel):
    prompt: str = Field(..., description="High-level idea/topic for the video script.")
    target_duration_seconds: int = Field(30, ge=5, le=600)
    tone: str = Field("neutral", description="e.g. 'dramatic', 'comedic', 'documentary'")
    project_id: Optional[str] = Field(
        default=None,
        description=(
            "Optional free-form id grouping this job with related jobs (e.g. a "
            "follow-up /api/storyboard call using the generated script). Not "
            "validated against any backing entity -- the caller mints it."
        ),
    )


class ScriptGenerationResponse(BaseModel):
    job_id: str
    status: str


class StoryboardShot(BaseModel):
    index: int
    description: str = ""
    prompt: str
    negative_prompt: str = ""


class StoryboardRequest(BaseModel):
    script: str = Field(..., description="Full script text; ignored if `shots` is provided.")
    workflow_name: str = Field(
        "default_t2v",
        pattern=r"^[A-Za-z0-9_-]+$",
        description=(
            "Filename (without extension) of a ComfyUI API-format workflow JSON in "
            "backend/workflows/. Letters, digits, underscore, hyphen only -- no path "
            "separators or extension, since this is joined directly onto a filesystem path."
        ),
    )
    shots: Optional[list[StoryboardShot]] = Field(
        default=None,
        description="Explicit shot list. If omitted, the script is split one shot per non-empty line.",
    )
    project_id: Optional[str] = Field(
        default=None,
        description="Optional free-form id grouping this job with related jobs (see ScriptGenerationRequest).",
    )


class StoryboardResponse(BaseModel):
    job_id: str
    status: str
    shot_count: int


class ShotStatusResponse(BaseModel):
    shot_index: int
    status: str
    prompt_id: Optional[str] = None
    files: Optional[list[str]] = None
    error: Optional[str] = None
    created_at: Optional[float] = None
    submitted_at: Optional[float] = None
    finished_at: Optional[float] = None


class JobStatusResponse(BaseModel):
    id: str
    kind: str
    status: str
    project_id: Optional[str] = None
    workflow_name: Optional[str] = None
    result: Optional[dict[str, Any]] = None
    error: Optional[str] = None
    shots: Optional[list[ShotStatusResponse]] = Field(
        default=None,
        description="Per-shot status for storyboard jobs, built from persisted shot state -- "
        "populated even while the job is still running, so partial progress is visible "
        "before the job reaches a terminal state. Always null for generate_script jobs.",
    )
    created_at: Optional[float] = None
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
