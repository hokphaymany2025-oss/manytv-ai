"""Pydantic request/response models for the API layer."""

from typing import Any, Optional

from pydantic import BaseModel, Field


class ScriptGenerationRequest(BaseModel):
    prompt: str = Field(..., description="High-level idea/topic for the video script.")
    target_duration_seconds: int = Field(30, ge=5, le=600)
    tone: str = Field("neutral", description="e.g. 'dramatic', 'comedic', 'documentary'")


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
        description="Filename (without extension) of a ComfyUI API-format workflow JSON in backend/workflows/",
    )
    shots: Optional[list[StoryboardShot]] = Field(
        default=None,
        description="Explicit shot list. If omitted, the script is split one shot per non-empty line.",
    )


class StoryboardResponse(BaseModel):
    job_id: str
    status: str
    shot_count: int


class JobStatusResponse(BaseModel):
    id: str
    kind: str
    status: str
    result: Optional[dict[str, Any]] = None
    error: Optional[str] = None
