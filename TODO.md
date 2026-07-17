# ManyTV — TODO

Generated from a full-repo analysis on 2026-07-17. See `PROJECT_ANALYSIS.md` for the full reasoning behind each item. Priority key: **P0** = broken/production-blocking, **P1** = should fix soon, **P2** = worth doing, **P3** = nice to have.

---

## Completed [x]

- [x] FastAPI backend skeleton with lifespan-managed startup/shutdown (`backend/app.py`)
- [x] Env-driven settings via `pydantic-settings`, `.env.example` kept in sync with `Settings` defaults (`backend/core/config.py`)
- [x] ComfyUI HTTP + WebSocket client — queue prompt, stream progress, fetch history, download outputs (`backend/core/comfyui_client.py`)
- [x] Single-slot FIFO job worker shared by both job kinds, to avoid two processes contending for one 8GB VRAM budget (`backend/core/worker.py`)
- [x] `/api/generate-script` — LLM-backed script generation via Ollama's OpenAI-compat shim, including defensive `<think>` block stripping for Qwen3 (`backend/core/llm_client.py`, `backend/api/routes/generate_script.py`)
- [x] `/api/storyboard` — script → shots → per-shot ComfyUI generation → downloaded output files (`backend/api/routes/storyboard.py`)
- [x] `GET /api/jobs/{id}` polling endpoint, `GET /health` liveness check
- [x] Naive shot-splitting (one shot per non-empty script line) with an escape hatch for explicit shot lists
- [x] Workflow templating by `CLIPTextEncode` node title (`Positive`/`Negative`), documented in `backend/workflows/README.md`
- [x] Shipped, verified-working default workflow: SD1.5 + AnimateDiff v3 text-to-video (`backend/workflows/default_t2v.json`)
- [x] `scripts/run_comfyui.ps1` — launches ComfyUI with Dynamic-VRAM-correct flags, checked against the actual installed `cli_args.py` rather than assumed
- [x] Anti-OOM safeguards: single-slot concurrency, `MAX_SHOTS_PER_JOB`, `GENERATION_TIMEOUT_SECONDS`, `--vram-headroom`
- [x] **Real end-to-end run verified on target hardware** (Intel Arc A750): shot 0 of job `fb4850f6-17c8-4798-881f-6321b45413b5` generated a valid `.mp4` — see `output/fb4850f6-17c8-4798-881f-6321b45413b5/shot_000/ManyTV_00002.mp4` and `server.log`
- [x] README.md — accurate, verified-on-this-machine setup and architecture documentation
- [x] Repo is now under version control (`git init` + baseline commit, 2026-07-17)
- [x] **BUG-2 fixed** (2026-07-17): `StoryboardRequest.workflow_name` now constrained to `^[A-Za-z0-9_-]+$` via Pydantic `Field(pattern=...)` in `backend/models/schemas.py`, closing the path-traversal input. See "Bugs" below for detail, `SESSION_STATE.md` Session Log for the full record.
- [x] Test tooling bootstrapped: `pytest` added to `requirements.txt`, first test module `tests/test_schemas.py` (11 cases covering the BUG-2 fix), all passing.
- [x] **BUG-1 mitigated** (2026-07-17): `ComfyUIClient.wait_for_completion` now falls back to polling `/history` instead of failing the job outright when the monitoring WebSocket drops. Unit-tested with mocks (`tests/test_comfyui_client.py`, 4 new cases); **not yet validated against a real ComfyUI disconnect** since ComfyUI wasn't running this session — see "Current tasks" below.

## Current tasks [ ]

- [ ] **Validate the BUG-1 fix against a real ComfyUI instance.** The polling fallback is implemented and passes mocked unit tests, but the original failure was only ever observed live (`server.log`, job `fb4850f6...`). Start ComfyUI, run a real multi-shot `/api/storyboard` job, and confirm a shot that previously would have killed the job now completes via the `/history` fallback (watch for the new `"WebSocket to ComfyUI dropped ... falling back to polling"` log line). Until this runs, treat BUG-1 as *mitigated*, not *closed*.

---

## Bugs

### ~~BUG-1 (P0) — ComfyUI WebSocket connection dies mid-storyboard-job with "keepalive ping timeout", losing the whole job~~ — MITIGATED 2026-07-17, pending live validation

**Confirmed in `server.log`, not hypothetical.** Job `fb4850f6-17c8-4798-881f-6321b45413b5`: shot 0 succeeded fully; shot 1 was queued and then, ~50s later with no progress events logged, the client raised:
```
websockets.exceptions.ConnectionClosedError: sent 1011 (internal error) keepalive ping timeout; no close frame received
```
from `backend/core/comfyui_client.py:70` (inside `wait_for_completion`). The existing mitigation (`ping_interval=None` on the client's own `websockets.connect()`, `comfyui_client.py:76`) only disables the **client's** keepalive pings. Reading the installed `websockets==16.1` source confirms `keepalive()` only runs `if self.ping_interval is not None` — so this exact error can't be coming from the client. It's most likely **ComfyUI's own WebSocket server** failing its side of the keepalive because ComfyUI's process is synchronously blocked mid-generation and can't service its own ping/pong in time — the same failure mode the code comment already anticipated, just from the direction the fix doesn't cover.

- **Impact:** any multi-shot storyboard job can lose all remaining shots (and be marked fully `FAILED`) the moment ComfyUI is busy long enough to miss its own keepalive — which is likely to recur on longer/heavier generations, not a one-off fluke.
- **Fix applied:** rather than chase ComfyUI's own server-side ping/timeout flags (unconfirmed whether they even exist, and wouldn't cover every disconnect cause), `wait_for_completion` in `backend/core/comfyui_client.py` now catches the WebSocket drop and falls back to `_poll_history_until_done`, which polls `/history/{prompt_id}` every 3s until ComfyUI records a finished entry. This works because the prompt keeps executing inside ComfyUI regardless of whether the monitoring socket stays connected — the generation was never tied to it. A genuinely dead ComfyUI (not just a dropped socket) still fails fast: `get_history` raises a non-`ComfyUIError` (an `httpx` error) in that case, which propagates immediately instead of retrying. Real `execution_error` events from ComfyUI (an actual generation failure, not a connection issue) still raise `ComfyUIError` immediately and are not affected by this fallback.
- **Verified by:** `tests/test_comfyui_client.py` (4 cases, mocked) — polling retries until history appears, non-ComfyUI errors propagate immediately (no infinite retry against a dead server), a dropped socket falls back to polling rather than raising, and a real `execution_error` still raises immediately without going through the fallback. `python -m pytest tests/ -v` → 15 passed.
- **Not yet done:** live validation against a real ComfyUI instance — see "Current tasks" above. The original failure was only ever observed against a real running ComfyUI process; the fix has not been.

### ~~BUG-2 (P1) — `workflow_name` is not sanitized before being used to build a filesystem path~~ — FIXED 2026-07-17

`backend/api/routes/storyboard.py:39-47`:
```python
path = settings.workflow_path / f"{workflow_name}.json"
```
`workflow_name` came directly from the `StoryboardRequest` request body with no validation (no allowlist, no `Path.name`-only check, no rejection of `/` or `..`). A request like `{"workflow_name": "../../../../some/other/file"}` would attempt to read `<other/file>.json` from outside `backend/workflows/`. Combined with wildcard CORS (`allow_origins=["*"]`) and zero authentication on any endpoint (see BUG-3), this was reachable from any origin, including a malicious webpage in a browser on the same machine.
- **Fix applied:** `StoryboardRequest.workflow_name` in `backend/models/schemas.py` now has `pattern=r"^[A-Za-z0-9_-]+$"` on its `Field`, rejecting anything with `/`, `\`, `.`, spaces, or empty string at the Pydantic validation boundary (HTTP 422) before it ever reaches `_load_workflow`. Fixed at the model layer only — every call path into `_load_workflow` goes through this already-validated field, so no redundant check was added inside `_load_workflow` itself.
- **Verified by:** `tests/test_schemas.py` — 8 traversal/invalid-character cases rejected, 3 valid names (including the `"default_t2v"` default) still accepted. `python -m pytest tests/ -v` → 11 passed.
- **Residual risk, tracked separately:** BUG-3 (wildcard CORS + no auth) is what made this reachable from a browser in the first place and is still open — fixing BUG-2 closes this specific input, not the exposure model.

### BUG-3 (P1) — No authentication and wildcard CORS on every endpoint

`backend/app.py:48-53`: `allow_origins=["*"]`, `allow_methods=["*"]`, `allow_headers=["*"]`, and no auth dependency on any route. Fine as long as this only ever binds to `127.0.0.1` for a single local user (the documented intent) — but there's nothing in the code enforcing that assumption, and it's exactly what turns BUG-2 into something exploitable by any page the user's browser visits, not just a deliberate attacker with network access.
- **Fix:** at minimum, restrict CORS to explicit known origins once a frontend exists; consider binding uvicorn to `127.0.0.1` explicitly rather than relying on the operator to remember `--host` defaults, and/or a simple shared-secret header if the API will ever be reachable beyond localhost.

### BUG-4 (P2) — Partial job failure loses successful-shot results

`backend/api/routes/storyboard.py:90-129`: if shot *N* fails, the whole `_run_storyboard_job` handler raises, `worker.py`'s catch-all sets `job.status = FAILED` and `job.error = str(exc)`, but `job.result` is never populated — so shots 0..N-1 that *succeeded* (and whose files are sitting on disk in `output/<job_id>/shot_000/` etc.) are invisible to anyone polling `/api/jobs/{id}`. This directly happened in the one real run on record (BUG-1's job): shot 0's `.mp4` exists but the job API would report only an error, no indication shot 0 worked.
- **Fix:** accumulate `results` as shots complete and attach whatever was gathered so far to `job.result` (or a `partial_result`) even on failure, rather than only setting it on full success.

### BUG-5 (P3) — Two independent `ComfyUIClient` instances constructed for no functional reason

`backend/app.py:31` (lifespan health check) and `backend/api/routes/storyboard.py:31` (module-level) each construct their own `ComfyUIClient(get_settings())`. Harmless (the class is stateless) but redundant — worth consolidating into one shared instance if the class ever grows connection-pooling state.

---

## Missing features

- [ ] **Job persistence.** `SingleSlotWorker._jobs` is a plain in-process dict — restarting the backend loses all job history and in-flight-job state permanently. Explicitly self-documented as a known gap in README.md. (P1)
- [ ] **Job cancellation.** No way to cancel a queued or running job via the API — a mis-submitted 50-shot job has to run to completion or the whole process has to be killed. (P2)
- [ ] **Partial-result recovery / resume.** No retry-from-last-successful-shot if a job fails partway. BUG-1's fix removes the most common trigger (a transient WebSocket blip no longer costs the whole job), but a shot that fails for a *real* reason (bad prompt, ComfyUI OOM, genuine crash) still takes down every shot queued after it, and BUG-4 (still open) means the caller can't even see what succeeded first. (P1)
- [ ] **Automated tests — still mostly missing.** `tests/test_schemas.py` (11 cases) now covers the BUG-2 validation fix, but that's the only module that exists. Still needed: unit tests for `_naive_shot_split`, `_apply_shot_to_workflow`, and the worker's queue/failure semantics; an integration test that mocks the ComfyUI HTTP/WS surface. (P1)
- [ ] **CI.** No `.github/workflows/` or equivalent — nothing runs automatically on change. Now that `pytest` + a first test module exist, this is cheap to add (`pytest` on push/PR). (P2)
- [ ] **Frontend.** Explicitly out of scope so far per README ("No frontend yet — this is the orchestration backend only"). Not a bug, just the obvious next big chunk of work once the backend is hardened. (P2)
- [ ] **Structured/queryable job history.** Even with persistence, there's currently no endpoint to list jobs (only fetch by known ID) — worth adding once persistence lands. (P3)
- [ ] **Request size limits.** No length cap on `prompt`/`script` free-text fields. (P3)
- [ ] **Dependency pinning/lockfile.** `requirements.txt` uses floors only (`>=`); no lockfile, so a fresh install on a new machine isn't guaranteed to reproduce the exact verified environment. (P2)

---

## Recommended next steps (in order)

1. ~~`git init` and commit the current working state as-is~~ — done 2026-07-17.
2. ~~Fix BUG-2 (path traversal in `workflow_name`)~~ — done 2026-07-17.
3. ~~Fix/mitigate BUG-1 (WebSocket keepalive drop)~~ — mitigation implemented and unit-tested 2026-07-17. **Live validation against a real ComfyUI instance is still outstanding** (see "Current tasks") — do this before treating BUG-1 as fully closed, ideally before or alongside step 4.
4. **Fix BUG-4** — now the most impactful remaining gap: make partial progress visible/recoverable in the job API instead of silently discarded on failure. Pairs naturally with BUG-1's live validation, since that's exactly the scenario (a mid-job failure after some shots succeed) BUG-4 is about.
5. **Extend the test suite** (now covering `schemas.py` and `comfyui_client.py` — see `tests/`) to the worker's queue/failure semantics and the pure functions in `storyboard.py` (`_naive_shot_split`, `_apply_shot_to_workflow`) — would have caught the shape of BUG-4 immediately.
6. **Decide on job persistence** (SQLite is explicitly suggested in the README already) once the above reliability work is done — no point persisting a job model that's about to change shape for partial-result support.
7. **Revisit CORS/auth (BUG-3)** before any deployment beyond a single trusted local machine.
8. Only after 1–7: start on the frontend, since the API surface (especially job status/result shape) is likely to shift slightly from BUG-4's fix.
