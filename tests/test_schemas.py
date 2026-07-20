"""Regression test for BUG-2: workflow_name must not allow path traversal.

_load_workflow() in backend/api/routes/storyboard.py joins workflow_name
directly onto a filesystem path (settings.workflow_path / f"{workflow_name}.json"),
so the Pydantic model is the boundary that has to reject anything but a bare
filename before it ever reaches disk I/O.
"""

import pytest
from pydantic import ValidationError

from backend.models.schemas import (
    ScriptGenerationRequest,
    StoryboardRequest,
    StoryboardShot,
)


def test_workflow_name_default_is_valid():
    request = StoryboardRequest(script="A lighthouse at dawn.")
    assert request.workflow_name == "default_t2v"


@pytest.mark.parametrize(
    "workflow_name",
    [
        "../../../../etc/passwd",
        "../secrets",
        "sub/dir",
        "sub\\dir",
        "name.json",
        "name with spaces",
        "",
    ],
)
def test_workflow_name_rejects_path_traversal_and_invalid_chars(workflow_name):
    with pytest.raises(ValidationError):
        StoryboardRequest(script="x", workflow_name=workflow_name)


@pytest.mark.parametrize("workflow_name", ["default_t2v", "my-workflow_2", "ABC123"])
def test_workflow_name_accepts_valid_names(workflow_name):
    request = StoryboardRequest(script="x", workflow_name=workflow_name)
    assert request.workflow_name == workflow_name


# --- Request size limits (no cap existed before) ---


def test_script_generation_prompt_rejects_over_length():
    with pytest.raises(ValidationError):
        ScriptGenerationRequest(prompt="x" * 4001)


def test_script_generation_prompt_accepts_at_length():
    request = ScriptGenerationRequest(prompt="x" * 4000)
    assert len(request.prompt) == 4000


def test_script_generation_tone_rejects_over_length():
    with pytest.raises(ValidationError):
        ScriptGenerationRequest(prompt="x", tone="x" * 101)


def test_storyboard_script_rejects_over_length():
    with pytest.raises(ValidationError):
        StoryboardRequest(script="x" * 20001)


def test_storyboard_script_accepts_at_length():
    request = StoryboardRequest(script="x" * 20000)
    assert len(request.script) == 20000


@pytest.mark.parametrize("field", ["description", "prompt", "negative_prompt"])
def test_storyboard_shot_text_fields_reject_over_length(field):
    kwargs = {"index": 0, "prompt": "x"}
    kwargs[field] = "x" * 4001
    with pytest.raises(ValidationError):
        StoryboardShot(**kwargs)


def test_storyboard_shot_text_fields_accept_at_length():
    shot = StoryboardShot(index=0, description="x" * 4000, prompt="x" * 4000, negative_prompt="x" * 4000)
    assert len(shot.prompt) == 4000
