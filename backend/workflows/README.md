# Workflow templates

Drop ComfyUI workflow JSON files here, exported in **API format** (Settings
> Dev Mode > Save (API Format) in the ComfyUI UI), or hand-authored against
node schemas confirmed via `http://127.0.0.1:8188/object_info/<class_type>`.

## `default_t2v.json` (shipped, verified working on this machine)

Text-to-video: SD1.5 (`v1-5-pruned-emaonly-fp16.safetensors`) + AnimateDiff
v3 motion module (`v3_sd15_mm.ckpt`) via
[ComfyUI-AnimateDiff-Evolved](https://github.com/Kosinkadink/ComfyUI-AnimateDiff-Evolved),
16 frames @ 512x512 @ 8fps (~2s clip), encoded to mp4 via
[ComfyUI-VideoHelperSuite](https://github.com/Kosinkadink/ComfyUI-VideoHelperSuite)'s
`VHS_VideoCombine`. Confirmed end-to-end on this machine: a real request
through `/api/storyboard` produced a valid, non-empty `.mp4`.

Requires both custom node packs cloned into `ComfyUI/custom_nodes/`, the
checkpoint in `ComfyUI/models/checkpoints/`, and the motion module in
`ComfyUI/models/animatediff_models/` (see main `README.md`). No input image
needed — despite the project's earlier `default_i2v` working name, this
pipeline is text-to-video, not image-to-video; renamed to `default_t2v` to
match what it actually does.

Named this way because it's a reasonable, low-risk default for 8GB VRAM
(SD1.5 + AnimateDiff needs no CUDA-only kernels, unlike several newer DiT
video models) — not because it's the best possible quality. Swap in a
different model/workflow freely; just keep the node-naming convention below
or adjust `_apply_shot_to_workflow`.

## Node naming convention

`backend/api/routes/storyboard.py` fills in prompt text by looking for
`CLIPTextEncode` nodes whose title (set via right-click > Title in the UI,
or `_meta.title` in the JSON) is exactly `Positive` or `Negative`. An empty
`negative_prompt` on a shot does **not** overwrite the workflow's own
negative-prompt default — only a non-empty one does. Rename your prompt
nodes to match, or adjust `_apply_shot_to_workflow` if your workflow uses a
different text-encoding node.

A request's `workflow_name` field (default `"default_t2v"`) selects which
file here gets used — e.g. `"default_t2v"` loads `default_t2v.json`.
