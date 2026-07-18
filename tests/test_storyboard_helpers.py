"""Direct unit tests for the pure(-ish) helper functions in
backend/core/storyboard_engine.py: _naive_shot_split (script -> shot list),
_load_workflow (workflow name -> parsed JSON), _apply_shot_to_workflow
(shot -> patched ComfyUI workflow JSON), and _save_history_outputs (ComfyUI
history -> downloaded files). All were previously only exercised indirectly
through _run_storyboard_job integration-style tests (see TODO.md item 13)
-- these test them in isolation, matching this repo's existing "test logic,
not just through the route" convention (e.g. JobTimeline's
buildTimelineEvents on the frontend).

_naive_shot_split/_apply_shot_to_workflow are plain synchronous functions --
no asyncio.run() or monkeypatching needed for those. _save_history_outputs
is async (a fake ComfyUIClient stand-in is used instead of a live one).
"""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from backend.core.comfyui_client import ComfyUIError
from backend.core.config import Settings
from backend.core.storyboard_engine import (
    _apply_shot_to_workflow,
    _load_workflow,
    _naive_shot_split,
    _save_history_outputs,
)
from backend.models.schemas import StoryboardShot


# ---- _naive_shot_split ----


def test_naive_shot_split_empty_script_returns_no_shots():
    assert _naive_shot_split("") == []


def test_naive_shot_split_whitespace_only_script_returns_no_shots():
    assert _naive_shot_split("   \n\t\n  \n") == []


def test_naive_shot_split_skips_blank_lines_between_content():
    script = "first shot\n\n   \nsecond shot\n\t\nthird shot"

    shots = _naive_shot_split(script)

    assert [s.description for s in shots] == ["first shot", "second shot", "third shot"]


def test_naive_shot_split_assigns_sequential_zero_based_indices_with_no_gaps():
    """Indices must track position in the filtered (blank-line-free) list,
    not the original line number -- a blank line between two content lines
    must not leave a gap in the assigned indices."""
    script = "a\n\nb\n\n\nc"

    shots = _naive_shot_split(script)

    assert [s.index for s in shots] == [0, 1, 2]


def test_naive_shot_split_strips_surrounding_whitespace_from_each_line():
    script = "  leading and trailing spaces  \n\tone tab-indented line\t\n"

    shots = _naive_shot_split(script)

    assert [s.description for s in shots] == ["leading and trailing spaces", "one tab-indented line"]


def test_naive_shot_split_sets_description_equal_to_prompt_and_empty_negative_prompt():
    shots = _naive_shot_split("a lone tree on a hill")

    assert len(shots) == 1
    shot = shots[0]
    assert shot.description == "a lone tree on a hill"
    assert shot.prompt == "a lone tree on a hill"
    assert shot.negative_prompt == ""


# ---- _load_workflow ----
# Previously only exercised indirectly, always with _load_workflow itself
# monkeypatched out to a stub in tests/test_recovery.py's shot-reconciliation
# tests -- the real function body (a real filesystem read) had no direct
# coverage anywhere until now.


def test_load_workflow_reads_and_parses_a_real_workflow_file(tmp_path):
    workflow_dir = tmp_path / "workflows"
    workflow_dir.mkdir()
    (workflow_dir / "my_workflow.json").write_text(json.dumps({"1": {"class_type": "KSampler"}}))
    settings = Settings(workflow_dir=str(workflow_dir))

    workflow = _load_workflow("my_workflow", settings)

    assert workflow == {"1": {"class_type": "KSampler"}}


def test_load_workflow_raises_comfyui_error_when_file_is_missing(tmp_path):
    settings = Settings(workflow_dir=str(tmp_path / "workflows"))

    with pytest.raises(ComfyUIError, match="not found"):
        _load_workflow("does-not-exist", settings)


# ---- _apply_shot_to_workflow ----


def _workflow(positive_text="old positive", negative_text="old negative", positive_title="Positive", negative_title="Negative"):
    return {
        "1": {"class_type": "CLIPTextEncode", "inputs": {"text": positive_text}, "_meta": {"title": positive_title}},
        "2": {"class_type": "CLIPTextEncode", "inputs": {"text": negative_text}, "_meta": {"title": negative_title}},
    }


def _shot(prompt="new positive prompt", negative_prompt="") -> StoryboardShot:
    return StoryboardShot(index=0, description=prompt, prompt=prompt, negative_prompt=negative_prompt)


def test_apply_shot_to_workflow_sets_positive_node_text_to_shot_prompt():
    workflow = _workflow()
    shot = _shot(prompt="a red bicycle leaning against a wall")

    patched = _apply_shot_to_workflow(workflow, shot)

    assert patched["1"]["inputs"]["text"] == "a red bicycle leaning against a wall"


def test_apply_shot_to_workflow_title_matching_is_case_insensitive():
    for title_variant in ["POSITIVE", "Positive", "  Positive  ", "pOsItIvE"]:
        workflow = _workflow(positive_title=title_variant)
        shot = _shot(prompt="matched regardless of case")

        patched = _apply_shot_to_workflow(workflow, shot)

        assert patched["1"]["inputs"]["text"] == "matched regardless of case"


def test_apply_shot_to_workflow_negative_prompt_overrides_when_provided():
    workflow = _workflow(negative_text="old negative")
    shot = _shot(negative_prompt="blurry, low quality")

    patched = _apply_shot_to_workflow(workflow, shot)

    assert patched["2"]["inputs"]["text"] == "blurry, low quality"


def test_apply_shot_to_workflow_leaves_negative_node_untouched_when_shot_has_none():
    """Documented behavior (see the function's own comment): an empty
    shot.negative_prompt must not clobber whatever default negative prompt
    the workflow JSON already ships with."""
    workflow = _workflow(negative_text="the workflow's own default negative prompt")
    shot = _shot(negative_prompt="")

    patched = _apply_shot_to_workflow(workflow, shot)

    assert patched["2"]["inputs"]["text"] == "the workflow's own default negative prompt"


def test_apply_shot_to_workflow_ignores_non_cliptextencode_nodes():
    workflow = {
        "1": {"class_type": "KSampler", "inputs": {"text": "should never be touched"}, "_meta": {"title": "Positive"}},
    }
    shot = _shot(prompt="irrelevant")

    patched = _apply_shot_to_workflow(workflow, shot)

    assert patched["1"]["inputs"]["text"] == "should never be touched"


def test_apply_shot_to_workflow_ignores_cliptextencode_with_unrecognized_title():
    workflow = {
        "1": {"class_type": "CLIPTextEncode", "inputs": {"text": "untouched"}, "_meta": {"title": "Unrelated"}},
    }
    shot = _shot(prompt="irrelevant")

    patched = _apply_shot_to_workflow(workflow, shot)

    assert patched["1"]["inputs"]["text"] == "untouched"


def test_apply_shot_to_workflow_ignores_cliptextencode_node_missing_meta_entirely():
    workflow = {"1": {"class_type": "CLIPTextEncode", "inputs": {"text": "untouched"}}}
    shot = _shot(prompt="irrelevant")

    patched = _apply_shot_to_workflow(workflow, shot)

    assert patched["1"]["inputs"]["text"] == "untouched"


def test_apply_shot_to_workflow_does_not_mutate_the_original_workflow():
    workflow = _workflow(positive_text="original positive", negative_text="original negative")
    shot = _shot(prompt="a brand new prompt", negative_prompt="a brand new negative")

    patched = _apply_shot_to_workflow(workflow, shot)

    assert patched is not workflow
    assert patched["1"] is not workflow["1"]
    assert workflow["1"]["inputs"]["text"] == "original positive"
    assert workflow["2"]["inputs"]["text"] == "original negative"
    # ...while the returned copy did get patched, confirming this isn't just
    # a no-op copy that happens to look unchanged.
    assert patched["1"]["inputs"]["text"] == "a brand new prompt"
    assert patched["2"]["inputs"]["text"] == "a brand new negative"


# ---- _save_history_outputs ----
# The "happy path" (a well-formed history with real dict entries) is already
# exercised indirectly via tests/test_recovery.py's shot-completion tests --
# these cover the two skip branches specifically, which weren't reachable
# from any existing test.


def test_save_history_outputs_skips_non_list_node_output_values(tmp_path):
    """A node_output value that isn't a list (ComfyUI's history shape in
    practice, but not guaranteed by any schema this client controls) must be
    skipped rather than raising -- and fetch_output_bytes must never be
    called for it."""
    client = AsyncMock()
    client.fetch_output_bytes.side_effect = AssertionError("must not be called for a non-list value")
    history = {"outputs": {"10": {"not_a_list": "unexpected string value"}}}

    saved = asyncio.run(_save_history_outputs(client, history, tmp_path))

    assert saved == []


def test_save_history_outputs_skips_entries_missing_filename(tmp_path):
    client = AsyncMock()
    client.fetch_output_bytes.side_effect = AssertionError("must not be called for a malformed entry")
    history = {"outputs": {"10": {"gifs": ["not a dict", {"subfolder": "", "type": "output"}]}}}

    saved = asyncio.run(_save_history_outputs(client, history, tmp_path))

    assert saved == []


def test_save_history_outputs_downloads_and_saves_valid_entries(tmp_path):
    client = AsyncMock()
    client.fetch_output_bytes.return_value = b"fake video bytes"
    history = {"outputs": {"10": {"gifs": [{"filename": "shot0.mp4", "subfolder": "", "type": "output"}]}}}

    saved = asyncio.run(_save_history_outputs(client, history, tmp_path))

    assert saved == [str(tmp_path / "shot0.mp4")]
    assert (tmp_path / "shot0.mp4").read_bytes() == b"fake video bytes"
