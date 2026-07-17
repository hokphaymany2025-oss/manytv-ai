# ManyTV — Project Analysis

**Analyzed:** 2026-07-17
**Scope:** Full repository at `D:\NewProjects\ManyTV` (backend source, scripts, workflow templates, runtime artifacts). No code was modified during this analysis.

---

## 1. What this project is

ManyTV is a small **FastAPI orchestration backend** for a self-hosted AI video pipeline:

1. `/api/generate-script` — turns a topic + tone into a shot-by-shot script via a local LLM (Ollama, OpenAI-compatible API).
2. `/api/storyboard` — splits a script into shots (or accepts an explicit shot list) and drives a **separately running ComfyUI instance** to generate one video clip per shot.

It deliberately does **not** load any ML model itself. Every generation happens inside a ComfyUI process reached over HTTP + WebSocket; ManyTV's job is queueing, prompt-graph templating, progress tracking, and downloading results. This separation is the central architectural decision and it's consistently honored across the codebase — there is no `torch` import anywhere except the optional/defensive path in `gpu_memory.py`.

The target hardware is a single 8GB Intel Arc A750 GPU shared between ComfyUI (video) and Ollama (script generation), which explains almost every non-obvious design choice in this codebase (see §3).

**Total source:** 726 lines of Python across 8 non-empty files, 1 PowerShell launch script, 1 workflow JSON, 3 markdown docs. This is a small, single-maintainer, pre-1.0 project (`version="0.1.0"` in `app.py`), not a large legacy system — "old" here means "has already been run and produced real output," not "large/decayed."

---

## 2. Architecture

```
ManyTV/
├── backend/
│   ├── app.py                  FastAPI app, lifespan (starts worker, checks ComfyUI), CORS, /health
│   ├── core/
│   │   ├── config.py           pydantic-settings Settings, loaded from .env
│   │   ├── comfyui_client.py   HTTP + WebSocket client for ComfyUI (/prompt, /ws, /history, /view)
│   │   ├── llm_client.py       OpenAI-compatible client for script generation (Ollama by default)
│   │   ├── worker.py           SingleSlotWorker: one FIFO queue, one asyncio task, max 1 concurrent job
│   │   └── gpu_memory.py       best-effort torch.xpu cache release after each job (no-op if torch absent)
│   ├── api/routes/
│   │   ├── generate_script.py  POST /api/generate-script (queues an LLM job)
│   │   └── storyboard.py       POST /api/storyboard, GET /api/jobs/{id}
│   ├── models/schemas.py       Pydantic request/response models
│   └── workflows/
│       ├── default_t2v.json    shipped SD1.5 + AnimateDiff v3 text-to-video ComfyUI graph
│       └── README.md           node-naming convention (`Positive`/`Negative` CLIPTextEncode titles)
├── scripts/run_comfyui.ps1     launches ComfyUI with Dynamic-VRAM-aware flags for the A750
├── output/<job_id>/shot_NNN/   downloaded generation results (one real run present, see §5)
├── requirements.txt            backend deps + optional Intel XPU torch stack
├── .env.example                documented, matches config.py defaults exactly
└── README.md                   thorough, accurate, written *from* real runs on this machine
```

### Request flow (storyboard)

1. Client `POST /api/storyboard` with a script or explicit shots + `workflow_name`.
2. `storyboard.py` splits the script one-shot-per-nonempty-line (`_naive_shot_split`) unless shots are given explicitly, validates shot count against `MAX_SHOTS_PER_JOB`, and hands the job to `worker.submit("storyboard", ...)`. Returns `job_id` immediately (HTTP 200, job status `queued`).
3. `SingleSlotWorker._run` pulls the job off its `asyncio.Queue` (exactly one task ever drains it) and calls the registered `"storyboard"` handler.
4. The handler loads the named workflow JSON, and for each shot: deep-copies the template, patches the `CLIPTextEncode` nodes titled `Positive`/`Negative`, `POST`s it to ComfyUI's `/prompt`, then blocks on the shared `/ws` WebSocket until ComfyUI reports the prompt's `executing` event with `node=None` (i.e. done). Downloads every output file via `/view` into `output/<job_id>/shot_NNN/`.
5. Client polls `GET /api/jobs/{id}` until `status` is `done` or `failed`.

### Request flow (script generation)

Same worker, different handler (`kind="generate_script"`). This is intentional, not incidental: Ollama loads its model 100% onto the *same* GPU as ComfyUI on this hardware (verified via `ollama ps`, per the README), so script and video jobs are serialized through one queue to avoid two processes contending for one 8GB VRAM budget. This is the single most important architectural fact about the project — it explains the worker design, the `--vram-headroom` flag, and the timeout values, and it's confirmed (not assumed) in the code comments and README.

### Concurrency model

- One `asyncio.Queue`, one consumer task (`worker.py:53-115`). Genuinely single-flight; no locking needed because there's no parallelism to guard against.
- Job state lives in a plain `dict[str, Job]` in process memory. No persistence layer.
- `release_gpu_memory()` runs in the worker's `finally` block after every job, but it's a documented no-op unless `torch` is installed in *this* venv (it isn't — confirmed, see §6) and unless it exposes `torch.xpu`. In the current install it does nothing but `gc.collect()`.

---

## 3. Design rationale worth preserving

These aren't obvious from the code alone without the comments/README, so they're recorded here in case the docs ever drift from the code:

- **Why one shared worker for two different job kinds** — not just "avoid two ComfyUI jobs at once," but "avoid Ollama and ComfyUI fighting over the same physical VRAM," confirmed via `ollama ps` during a real request (`backend/core/worker.py:9-14`, README "Why single-slot").
- **Why `ping_interval=None` on the ComfyUI WebSocket client** (`backend/core/comfyui_client.py:76`) — ComfyUI's own execution is synchronous and can block its event loop for 55s+ during a single generation step, long enough to miss the websockets library's default 20s client keepalive and kill the connection mid-job. See §5 for evidence that this mitigation is **incomplete**.
- **Why there's no `--normalvram` flag in `run_comfyui.ps1`** — checked against this project's actual cloned `comfy/cli_args.py` rather than generic ComfyUI docs; that flag doesn't exist in the installed version. A documented example of "verify against the real source, not memory/training data," which is a good practice signal for this codebase.
- **Why `<think>...</think>` is stripped defensively in `llm_client.py:41`** — observed empirically that Ollama's *native* `/api/chat` endpoint inlines Qwen3's reasoning into `content` (not the separate `reasoning` field the OpenAI-compat shim normally uses), and that `think:false` does not suppress it on the installed Ollama version (0.32.0). The regex strip is a belt-and-braces fix for a real, observed inconsistency, not speculative hardening.
- **Why `default_t2v.json` is named `_t2v` and not `_i2v`** — the workflow doesn't take an input image despite the project's earlier working name; renamed to match actual behavior (`backend/workflows/README.md:21-22`).

---

## 4. Dependency review

**`requirements.txt`** is small and current as of analysis date:

| Package | Pinned floor | Installed (`.venv`) | Notes |
|---|---|---|---|
| fastapi | >=0.115 | 0.139.2 | current |
| uvicorn[standard] | >=0.32 | 0.51.0 | current |
| pydantic | >=2.9 | 2.13.4 | current |
| pydantic-settings | >=2.6 | 2.14.2 | current |
| httpx | >=0.27 | 0.28.1 | current |
| websockets | >=13.0 | **16.1** | see §5 — behavior differs meaningfully from what the code comment assumes |
| python-dotenv | >=1.0 | 1.2.2 | current |
| python-multipart | >=0.0.9 | 0.0.32 | unused directly — no file-upload endpoints exist yet; pulled in for future multipart support or as a FastAPI extra |
| openai | >=1.50 | 2.45.0 | current, used only as an OpenAI-compatible HTTP client (talks to Ollama) |
| torch/torchvision/torchaudio (XPU) | >=2.5 | **not installed in this venv** | correctly optional per requirements.txt comments; confirmed absent — `gpu_memory.py` is running in no-op mode right now |

No version conflicts, no abandoned/unmaintained packages, no unpinned ceiling versions (floors only — reasonable for an actively-developed personal project, but means a future `pip install -r requirements.txt` on a fresh machine could pull breaking major versions with no warning; see TODO.md).

**No `pyproject.toml`, no lockfile** (no `requirements.lock`, no `pip-compile` output, no `poetry.lock`). Reproducibility depends entirely on floor-pinned `requirements.txt` plus whatever pip resolves at install time.

**No dev/test dependencies at all** — no pytest, no ruff/black/mypy, nothing. Confirmed by searching the whole tree; see §7.

---

## 5. Confirmed behavior — what's actually been run

This is not a theoretical read of the code; the repo contains direct evidence of at least one real end-to-end run, which changes the confidence level of several findings below from "looks plausible" to "confirmed."

- `server.log` (70 lines, timestamped 2026-07-17 06:55–06:57) shows a real backend process (port 8003) that:
  - Connected to a real ComfyUI instance at startup (`GET /system_stats` → 200).
  - Accepted a real `POST /api/storyboard` (job `fb4850f6-17c8-4798-881f-6321b45413b5`).
  - Successfully generated **shot 0** end-to-end: queued to ComfyUI, streamed 20 KSampler steps + 16 AnimateDiff steps over the WebSocket, downloaded `ManyTV_00002.mp4`, saved to `output/fb4850f6.../shot_000/`. **This file exists on disk right now** — the pipeline works.
  - Then **failed on shot 1** after it was queued (prompt `eea673c3-...`) with:
    ```
    websockets.exceptions.ConnectionClosedError: sent 1011 (internal error) keepalive ping timeout; no close frame received
    ```
    raised from `comfyui_client.py:70` (`await ws.recv()`), roughly **50 seconds** after shot 1 was queued, with no visible ComfyUI progress events logged for it before the disconnect.
- `.e2e_job_id` at the repo root contains exactly this job's ID — a leftover scratch file from that manual/e2e test, not part of the application.

**This is a confirmed, reproduced bug, not a hypothetical one** — see TODO.md BUG-1. The client already disables its own keepalive pings specifically to avoid this failure mode (`ping_interval=None`, with a comment explaining why), but the crash happened anyway. Close code 1011 with reason `"keepalive ping timeout"` is generated by the `websockets` library's own `keepalive()` coroutine (confirmed by reading `websockets/asyncio/connection.py:809-849` in the installed 16.1 package) — and that coroutine only runs `if self.ping_interval is not None`. Since the *client's* `ping_interval` is explicitly `None` here, the client did not send that failing ping. The far more likely source is **ComfyUI's own WebSocket server-side keepalive**, timing out because ComfyUI's process is synchronously blocked running the next shot's generation and can't service its own ping/pong in time — the exact failure mode the existing comment already describes, just from the other side of the connection than the fix addresses. The one-directional fix is real but incomplete.

**Consequence of the bug as currently handled:** the whole job (`fb4850f6...`) is marked `FAILED` in the in-memory job store even though shot 0 fully succeeded and its file is sitting on disk. There is no partial-success reporting — `GET /api/jobs/{id}` would show `error` populated and `result: null`, with no way to know from the API response that shot 0 actually finished. A caller has to know to go look in `output/<job_id>/` directly.

---

## 6. Code quality assessment

**Overall: notably high for a 0.1.0 project.** Specific positives:

- Consistent, modern type hints (`dict[str, Any]`, `Optional[...]`, PEP 604 unions in the installed dependency code) throughout.
- Every non-obvious decision has a comment explaining *why*, often citing a specific verified observation (`ollama ps`, a real log line, a specific ComfyUI source file) rather than a general claim — this is unusually disciplined for a small project and should be preserved as a pattern in future work.
- Correct handling of the classic Python late-binding closure bug: `_progress` in `storyboard.py:108` captures `shot_index: int = shot.index` as a default argument specifically to avoid all shots' progress callbacks referencing the same final loop variable.
- Custom exception types (`ComfyUIError`, `LLMError`) instead of letting raw `httpx`/`websockets`/`openai` exceptions leak into application logic.
- `.env.example` and `config.py`'s `Settings` defaults are kept in exact sync (checked field-by-field) — a common source of drift in real projects, not present here.
- No dead code, no commented-out code blocks, no `TODO`/`FIXME`/`XXX` markers anywhere in the source (verified via full-tree search) — either genuinely clean or markers were never used as a convention; either way nothing was left half-finished-and-flagged.

**Weaknesses / smells (none severe, all listed in TODO.md with context):**

- Two independent `ComfyUIClient(settings)` instances are constructed (one in `app.py`'s lifespan for the startup health check, one module-level in `storyboard.py`) — harmless since the class is stateless, but unnecessary duplication.
- `_apply_shot_to_workflow` deep-copies the workflow template via `json.loads(json.dumps(workflow))` per shot — fine at current scale (workflow JSON is a few KB), would be worth a real `copy.deepcopy` or a cheaper diff-based patch only if workflows grow much larger.
- No structured logging (plain `%s`-format `logging` calls) — adequate for a single-user local tool, would need revisiting before any multi-user or hosted deployment.
- No input size/rate limiting beyond `MAX_SHOTS_PER_JOB` — a single very long `prompt` or `script` string has no length cap.

---

## 7. Testing, CI, and documentation gaps

- **No automated tests exist anywhere in the repo** — no `tests/` directory, no `pytest`/`unittest` usage, no test dependency in `requirements.txt`. Confidence in correctness currently comes entirely from the one manual e2e run captured in `server.log`.
- **No CI configuration** — no `.github/workflows/`, no other CI config found.
- **No linting/formatting/type-checking configuration** — no `pyproject.toml`, `ruff.toml`, `.flake8`, `mypy.ini`, or `setup.cfg`.
- **Not a git repository yet** — there is a `.gitignore` (correctly excluding `.venv/`, `__pycache__/`, `.env`, and `output/*` while keeping `output/.gitkeep`), but no `.git/` directory exists. **None of this work is currently under version control.** This is the single most consequential gap found in this analysis, independent of anything about the code itself: there is no history, no ability to diff future changes, and no recovery path if a file is lost or a change needs reverting.
- **In-code documentation is strong**; **README.md is unusually good** for a project this size — accurate, dated implicitly by its own "confirmed on this machine" claims, and it already self-reports its two known gaps (in-memory job store, missing ComfyUI checkpoint/workflow at doc-writing time — now resolved, see §5). No module-level docstring is missing; every file that needs one has one.
- **Not documented anywhere:** the WebSocket disconnect failure mode in §5 (the README's "Why single-slot" section describes the mitigation but not that it's been observed to fail), and the path-traversal-shaped input in `workflow_name` (see TODO.md BUG-2).

---

## 8. Summary judgment

This is a small, deliberately scoped, honestly documented backend that **works** — not a prototype that merely compiles. One real end-to-end video was generated on real hardware. The architecture is appropriately simple for its actual constraint (one shared 8GB GPU, one user, one machine) and does not over-engineer for scale it doesn't need.

The gaps are exactly what you'd expect from "backend built and verified solo, not yet hardened or given a frontend": no persistence, no tests, no git history, one confirmed reliability bug in the exact place the author already knew was risky, and one unaddressed input-validation gap. None of these require an architecture change — they're incremental hardening work on a foundation that's already sound. See TODO.md for prioritized next steps.
