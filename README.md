# ManyTV

Self-hosted AI video generation workspace backend. A FastAPI orchestration
layer that drives a locally running [ComfyUI](https://github.com/comfyanonymous/ComfyUI)
instance over HTTP + WebSocket — it never loads a diffusion/video model
itself. That separation matters on an 8GB Intel Arc A750: model loading,
VRAM scheduling, and node execution all stay inside ComfyUI's own process,
which manages VRAM automatically via a **Dynamic VRAM** system that's on
by default in the version this project targets (verified against the
actual cloned `comfy/cli_args.py`, not assumed from general docs) — plus
fp8 loaders and a `--vram-headroom` knob for headroom shared with other
apps. See `scripts/run_comfyui.ps1` for the full breakdown of what each
flag actually does in this version.

Script generation shares that same VRAM budget: Ollama loads its model
100% onto the Arc A750 too (confirmed via `ollama ps` during a real
request), so both job types are serialized through one worker queue — see
"Why single-slot" below.

## Architecture

```
ManyTV/
├── backend/
│   ├── app.py                  # FastAPI entrypoint
│   ├── core/
│   │   ├── config.py           # env-driven Settings
│   │   ├── comfyui_client.py   # HTTP + WebSocket client for ComfyUI
│   │   ├── worker.py           # single-slot job queue (max 1 concurrent job)
│   │   └── gpu_memory.py       # best-effort backend-process VRAM cleanup
│   ├── api/routes/
│   │   ├── generate_script.py  # POST /api/generate-script
│   │   └── storyboard.py       # POST /api/storyboard, GET /api/jobs/{id}
│   ├── models/schemas.py       # Pydantic request/response models
│   └── workflows/              # ComfyUI workflow JSON (API format) goes here
├── scripts/run_comfyui.ps1     # launches ComfyUI with the right VRAM flags
├── output/                     # downloaded generation results, per job/shot
├── requirements.txt
└── .env.example
```

## Why single-slot, and why the two anti-OOM layers live where they do

- **Worker concurrency (`backend/core/worker.py`)**: exactly one job runs at
  a time, enforced by a single asyncio task draining a FIFO queue. This is
  the primary OOM guard — two simultaneous video pipelines will not fit in
  8GB regardless of any other setting. Both `storyboard` (ComfyUI) and
  `generate_script` (Ollama) jobs share this one queue, since both land on
  the same physical GPU's VRAM — a script-generation call running while a
  video job is mid-generation would compete for the same 8GB.
- **VRAM release (`backend/core/gpu_memory.py`)**: runs after every job.
  Since the backend process doesn't hold model tensors, this is mostly a
  no-op safety net (see comments in that file) — the real VRAM budget is
  managed by ComfyUI itself via its Dynamic VRAM system (on by default) and
  fp8 loaders, configured in `scripts/run_comfyui.ps1` and your workflow
  JSON.
- **`--vram-headroom` (`scripts/run_comfyui.ps1 -VramHeadroomGB`)**: tells
  ComfyUI's Dynamic VRAM to keep that much VRAM free "even counting VRAM
  from other apps" (its own wording). Relevant specifically because Ollama
  shares this GPU (see above) and keeps its model resident for a few
  minutes after each call by default — the worker prevents jobs from
  *running* concurrently, but doesn't control when Ollama unloads, so this
  is cheap insurance against that overlap window.
- **`MAX_SHOTS_PER_JOB`** (default 50, in `.env`): caps how large a single
  storyboard request can be, so one API call can't queue an unbounded
  sequence of generations.
- **`GENERATION_TIMEOUT_SECONDS`** (default 600, in `.env`): a stalled
  ComfyUI job fails loudly instead of hanging the worker forever.

## Setup

### 1. Backend (this repo)

```powershell
cd D:\NewProjects\ManyTV
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```

`.env` defaults work out of the box for a local ComfyUI on port 8188 — you
only need to edit it if you change ports, move output/workflow locations,
or want different safety limits. See inline comments in `.env.example`.

### 2. ComfyUI (separate clone, separate venv)

ComfyUI is not vendored into this project — clone and set it up on its own.
**Already done and verified on this machine** at `D:\NewProjects\ComfyUI`;
steps below are for reference / reinstall elsewhere.

```powershell
git clone https://github.com/comfyanonymous/ComfyUI D:\NewProjects\ComfyUI
cd D:\NewProjects\ComfyUI
python -m venv .venv
.venv\Scripts\activate
```

**Intel Arc (XPU) driver + torch install**, inside that ComfyUI venv:

1. Install Intel's **Arc & Iris Xe Graphics** Windows driver from Intel's
   official driver download page if you haven't already — required for
   the GPU to be visible to the XPU backend at all.
2. Install the Intel XPU build of PyTorch:
   ```powershell
   pip install torch torchvision torchaudio --extra-index-url https://download.pytorch.org/whl/xpu
   ```
   Verified on this machine: resolved to `torch==2.13.0+xpu`,
   `torchvision==0.28.0+xpu`, `torchaudio==2.11.0+xpu` (pulls the full Intel
   oneAPI/SYCL/MKL runtime automatically — no separate IPEX or oneAPI
   installer needed). Exact versions will drift over time; if in doubt,
   check [pytorch.org](https://pytorch.org)'s "Get Started" selector
   (Windows / Pip / XPU).
3. Install ComfyUI's own dependencies (leaves the XPU torch build alone —
   `requirements.txt` pins torch unversioned, so pip won't touch an
   already-satisfying install):
   ```powershell
   pip install -r requirements.txt
   ```
4. Sanity-check the GPU is visible:
   ```powershell
   python -c "import torch; print(torch.xpu.is_available(), torch.xpu.get_device_name(0))"
   ```
   Confirmed output on this machine: `True Intel(R) Arc(TM) A750 Graphics`.
   A real `main.py` boot (see "Running" below) additionally confirmed
   `Total VRAM 7936 MB` and `Device: xpu:0 Intel(R) Arc(TM) A750 Graphics`
   in ComfyUI's own startup log, and `/system_stats` reports the device
   correctly to ManyTV's health check.

### 3. Add a workflow

Export at least one ComfyUI workflow in **API format** (Settings > Dev Mode
> Save (API Format)) into `backend/workflows/default_i2v.json`. See
`backend/workflows/README.md` for the prompt-node naming convention the
storyboard route expects. Build the workflow around an fp8 checkpoint/UNet
loader to stay comfortably under 8GB — the `-Fp8` flag on
`scripts/run_comfyui.ps1` only helps if the workflow's loader nodes support
fp8 weights.

## Running

Two separate processes, in order:

```powershell
# Terminal 1 — ComfyUI
D:\NewProjects\ComfyUI\.venv\Scripts\Activate.ps1  # or use its own shell
D:\NewProjects\ManyTV\scripts\run_comfyui.ps1 -ComfyUIPath D:\NewProjects\ComfyUI -VramHeadroomGB 2

# Terminal 2 — ManyTV backend
D:\NewProjects\ManyTV\.venv\Scripts\Activate.ps1
uvicorn backend.app:app --reload --port 8000
```

Then, e.g.:

```powershell
# 1. Generate a script (queues a job -- poll for the result, don't expect it inline)
curl -X POST http://127.0.0.1:8000/api/generate-script `
  -H "Content-Type: application/json" `
  -d '{\"prompt\": \"a lighthouse keeper finds a message in a bottle\", \"tone\": \"mysterious\"}'
curl http://127.0.0.1:8000/api/jobs/<job_id>   # result.script once status is "done"

# 2. Storyboard + generate video from a script
curl -X POST http://127.0.0.1:8000/api/storyboard `
  -H "Content-Type: application/json" `
  -d '{\"script\": \"A lighthouse at dawn.\nWaves crash below.\", \"workflow_name\": \"default_i2v\"}'
curl http://127.0.0.1:8000/api/jobs/<job_id>
```

## Endpoints

| Method | Path                  | Notes |
|--------|-----------------------|-------|
| POST   | `/api/generate-script`| Queues a script-generation job on the single-slot worker (`backend/core/llm_client.py`, defaults to local Ollama), returns a `job_id` — see "LLM setup" below |
| POST   | `/api/storyboard`     | Splits script into shots (or accepts explicit `shots`), queues one ComfyUI job per shot via the single-slot worker, returns a `job_id` |
| GET    | `/api/jobs/{job_id}`  | Poll job status/result/error — result is `{"script": "..."}` for generate-script jobs, `{"shots": [...]}` for storyboard jobs |
| GET    | `/health`             | Liveness check |

## LLM setup (for `/api/generate-script`)

Defaults to a local [Ollama](https://ollama.com) instance via its
OpenAI-compatible `/v1` shim — the `openai` SDK talks to it unmodified,
just pointed at `http://127.0.0.1:11434/v1` with a dummy API key.

```powershell
ollama pull qwen3:4b   # or any model you prefer; update LLM_MODEL in .env to match
ollama list             # confirm it's there
```

Ollama runs as a background service once installed, so nothing else needs
starting. To use a hosted OpenAI-compatible provider instead, just change
`LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` in `.env` — no code changes.

**Latency note, verified against this machine's Ollama 0.32.0**: `qwen3:4b`
"thinks" before answering by default, taking anywhere from ~30s to ~3min per
`/api/generate-script` call (confirmed with real requests, not estimated) —
and the standard `think: false` request flag does **not** suppress this for
qwen3 on that Ollama version, on either the native or OpenAI-compat
endpoint. `LLM_TIMEOUT_SECONDS` is set to 300 to comfortably cover this.
`llm_client.py` also strips any `<think>...</think>` block from the
response defensively — Ollama's OpenAI-compat shim normally keeps reasoning
in a separate `reasoning` field (not `content`), but the native endpoint
was observed inlining it into `content` instead, so the client doesn't rely
on either behavior.

Because `/api/generate-script` is queued through the worker (see "Why
single-slot" above) rather than blocking the HTTP request, this latency no
longer risks a proxy/browser timeout — but it does occupy the one shared
GPU slot for that long, delaying any storyboard job queued behind it. If
that trade-off doesn't work for your workflow, pull a smaller non-reasoning
instruct model and point `LLM_MODEL` at it.

## Known gaps / next steps

- Job state is in-memory only (`SingleSlotWorker._jobs`); restarting the
  backend loses history of past jobs. Fine for single-user local use;
  swap in SQLite if you need persistence across restarts.
- ComfyUI itself is installed and verified (Arc A750 detected, server
  boots, `/system_stats` reachable from ManyTV) but has **no model
  checkpoint and no workflow JSON yet** — `/api/storyboard` will still fail
  with a "workflow not found" error until you pick a video model, download
  its checkpoint into ComfyUI's `models/` folder, build a workflow around
  it in the ComfyUI UI, and export it as `backend/workflows/default_i2v.json`
  (see `backend/workflows/README.md`). Model choice wasn't made for you —
  it's a real tradeoff (quality vs. speed vs. license vs. VRAM headroom on
  a shared 8GB card) worth deciding deliberately rather than defaulting.
- No frontend yet — this is the orchestration backend only.
