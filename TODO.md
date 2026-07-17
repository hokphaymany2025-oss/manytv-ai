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
- [x] **BUG-1 mitigated** (2026-07-17): `ComfyUIClient.wait_for_completion` now falls back to polling `/history` instead of failing the job outright when the monitoring WebSocket drops. Unit-tested with mocks (`tests/test_comfyui_client.py`, 4 new cases).
- [x] **BUG-1 CLOSED — live-validated against real ComfyUI** (2026-07-17): forced a real WebSocket drop mid-generation against a live ComfyUI instance (Arc A750, XPU backend) and confirmed the fallback engages and the job still completes. Full evidence in "Bugs" below and `SESSION_STATE.md` Session Log.
- [x] **Job Manager + Queue + Resume System milestone (2026-07-17).** Job/shot state is now persisted to SQLite (`backend/core/job_store.py`, `output/jobs.db`) and survives a backend restart. A new `backend/core/recovery.py` reconciles incomplete jobs at startup — resubmitting never-started shots, and for shots already submitted to ComfyUI before a crash, checking `/history` first and reusing the result rather than blindly resubmitting (a real ComfyUI workflow isn't guaranteed deterministic, so resubmitting could silently produce a different video than one already relied on). **Live-validated end-to-end**, not just unit-tested: a job was killed mid-shot-1 (backend process only, ComfyUI untouched) and, on restart, correctly found shot 1's existing ComfyUI history and reused it instead of resubmitting, then completed shot 2 normally. Full evidence in `SESSION_STATE.md` Session Log. Design doc: `C:\Users\hokph\.claude\plans\warm-prancing-karp.md`.
  - This also **closes BUG-4** (partial job failure losing successful-shot results) as a natural side effect — `GET /api/jobs/{id}` now builds its response from the persisted `shots` table at read time, so completed shots are visible even while the job is still `RUNNING`, not only after full success. See BUG-4 below.
  - `project_id` (free-form, caller-supplied grouping tag, no backing entity) and `workflow_name` are now stored and returned on jobs; `JobStatusResponse` gained a `shots` field with per-shot `status`/`prompt_id`/`files`/`error`.
  - New test modules: `tests/test_job_store.py` (7 cases — CRUD, partial-update COALESCE semantics, shot upsert merging) and `tests/test_recovery.py` (10 cases — job-level routing, all three shot-reconciliation branches: DONE-skip, PENDING-normal, SUBMITTED×{history-found reuse, history-absent resubmit, ComfyUI-unreachable propagates without resubmitting}). `python -m pytest tests/ -v` → 32 passed.

## Current tasks [ ]

- [ ] Nothing actively in progress. Next up per "Recommended next steps": BUG-3 (wildcard CORS + no auth) is now the most impactful remaining open item.

---

## Bugs

### ~~BUG-1 (P0) — ComfyUI WebSocket connection dies mid-storyboard-job with "keepalive ping timeout", losing the whole job~~ — CLOSED 2026-07-17, live-validated

**Confirmed in `server.log`, not hypothetical.** Job `fb4850f6-17c8-4798-881f-6321b45413b5`: shot 0 succeeded fully; shot 1 was queued and then, ~50s later with no progress events logged, the client raised:
```
websockets.exceptions.ConnectionClosedError: sent 1011 (internal error) keepalive ping timeout; no close frame received
```
from `backend/core/comfyui_client.py:70` (inside `wait_for_completion`). The existing mitigation (`ping_interval=None` on the client's own `websockets.connect()`, `comfyui_client.py:76`) only disables the **client's** keepalive pings. Reading the installed `websockets==16.1` source confirms `keepalive()` only runs `if self.ping_interval is not None` — so this exact error can't be coming from the client. It's most likely **ComfyUI's own WebSocket server** failing its side of the keepalive because ComfyUI's process is synchronously blocked mid-generation and can't service its own ping/pong in time — the same failure mode the code comment already anticipated, just from the direction the fix doesn't cover.

- **Impact:** any multi-shot storyboard job can lose all remaining shots (and be marked fully `FAILED`) the moment ComfyUI is busy long enough to miss its own keepalive — which is likely to recur on longer/heavier generations, not a one-off fluke.
- **Fix applied:** rather than chase ComfyUI's own server-side ping/timeout flags (unconfirmed whether they even exist, and wouldn't cover every disconnect cause), `wait_for_completion` in `backend/core/comfyui_client.py` now catches the WebSocket drop and falls back to `_poll_history_until_done`, which polls `/history/{prompt_id}` every 3s until ComfyUI records a finished entry. This works because the prompt keeps executing inside ComfyUI regardless of whether the monitoring socket stays connected — the generation was never tied to it. A genuinely dead ComfyUI (not just a dropped socket) still fails fast: `get_history` raises a non-`ComfyUIError` (an `httpx` error) in that case, which propagates immediately instead of retrying. Real `execution_error` events from ComfyUI (an actual generation failure, not a connection issue) still raise `ComfyUIError` immediately and are not affected by this fallback.
- **Verified by mocks:** `tests/test_comfyui_client.py` (4 cases) — polling retries until history appears, non-ComfyUI errors propagate immediately (no infinite retry against a dead server), a dropped socket falls back to polling rather than raising, and a real `execution_error` still raises immediately without going through the fallback. `python -m pytest tests/ -v` → 15 passed.
- **Verified live (2026-07-17):** ComfyUI (real Arc A750, XPU backend, confirmed via `/system_stats`) and the ManyTV backend were both started for real. A baseline 3-shot job ran cleanly end-to-end first (job `147c1e4e...`, no regression). Windows Firewall block rules were tested and found to have **no effect on loopback traffic** (`127.0.0.1`→`127.0.0.1` is exempt from normal WFP filtering for regular desktop processes) — confirmed empirically, not assumed, so a throwaway TCP proxy (test infra only, not part of the app) was inserted between the backend and ComfyUI instead. Killing and instantly restarting that proxy process severed the backend's live WebSocket connection while never touching ComfyUI's own process. Result, job `5cb0ab45-a739-4665-84ce-846cae8bc267`, shot 0 (ComfyUI prompt `6de5b744-fef8-486b-b936-7c92d6b1beee`), interrupted at KSampler step 18/20:
  ```
  11:12:33,286 [WARNING] manytv.comfyui: WebSocket to ComfyUI dropped while waiting for
  prompt 6de5b744-fef8-486b-b936-7c92d6b1beee (no close frame received or sent);
  falling back to polling /history -- the job may still be running.
  11:12:33,466  GET /history/6de5b744... 200 OK   (poll 1)
  11:12:36,625  GET /history/6de5b744... 200 OK   (poll 2, ~3.16s later -- matches configured 3.0s interval)
  11:12:39,771  GET /history/6de5b744... 200 OK   (poll 3, history now present)
  11:12:39,880  shot 0 done: 1 file(s) saved to output\5cb0ab45.../shot_000\ManyTV_00009.mp4
  ```
  Job then proceeded to shot 1 normally and finished with `status: done`, both shots' `.mp4` files present, non-empty, and confirmed as valid ISO Media/MP4 (not empty/corrupt placeholders). ComfyUI's own log shows exactly one `"Starting server"` line for the entire session — it was never restarted or interrupted, only the backend's monitoring connection was severed. All four documented failure conditions were checked and none occurred: no lost prompt state, no infinite retry (exactly 3 bounded polls), job was not marked failed despite ComfyUI completing, and (per the existing mocked test, not re-exercised live since no real ComfyUI execution error occurred this session) `execution_error` handling is unaffected by this fallback path.
  - **Scope note:** the live run validates the WebSocket-drop → polling-fallback → job-completes path end-to-end for real. It does not independently re-prove the `execution_error`-still-raises-immediately guarantee live (no real execution error occurred to observe) — that guarantee rests on the mocked unit test only.

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

### ~~BUG-4 (P2) — Partial job failure loses successful-shot results~~ — CLOSED 2026-07-17

`backend/api/routes/storyboard.py`: if shot *N* failed, the whole `_run_storyboard_job` handler raised, `worker.py`'s catch-all set `job.status = FAILED` and `job.error = str(exc)`, but `job.result` was never populated — so shots 0..N-1 that *succeeded* (and whose files were sitting on disk in `output/<job_id>/shot_000/` etc.) were invisible to anyone polling `/api/jobs/{id}`. This directly happened in the one real run on record at the time (BUG-1's job): shot 0's `.mp4` existed but the job API reported only an error, no indication shot 0 worked.
- **Fix applied:** landed as a side effect of the Job Manager + Queue + Resume System milestone rather than as a standalone patch (deliberate sequencing decision — see that milestone's entry above). `GET /api/jobs/{id}` (`backend/api/routes/storyboard.py::get_job_status`) now builds its `result`/`shots` response from the persisted `shots` table at read time, which is populated as shots complete regardless of whether the job later fails — so partial progress is visible immediately, even while the job is still `RUNNING`, not only once it reaches a terminal state.
- **Verified by:** `tests/test_recovery.py` shot-reconciliation tests exercise the same persisted-shots read path; live-validated as part of the resume test (see Session Log) — a `GET /api/jobs/{id}` mid-job showed shot 0 as `done` while shot 1 was still `submitted`.

### BUG-5 (P3) — Two independent `ComfyUIClient` instances constructed for no functional reason

`backend/app.py:31` (lifespan health check) and `backend/api/routes/storyboard.py:31` (module-level) each construct their own `ComfyUIClient(get_settings())`. Harmless (the class is stateless) but redundant — worth consolidating into one shared instance if the class ever grows connection-pooling state.

---

## Missing features

- [x] ~~Job persistence.~~ Done 2026-07-17 — see the Job Manager + Queue + Resume System milestone above (`backend/core/job_store.py`, SQLite).
- [x] ~~Partial-result recovery / resume.~~ Done 2026-07-17, live-validated — same milestone. Note the scope boundary stated there: this covers *process-interruption* recovery (backend killed mid-job); automatic retry-from-last-successful-shot after a *genuine* shot failure (bad prompt, ComfyUI OOM) is still a distinct, unbuilt feature — a `FAILED` job is still terminal and isn't picked up by startup reconciliation.
- [ ] **Retry a FAILED job from its last successful shot.** Distinct from the resume system above (which only handles process-interruption, not genuine shot failures) — the `shots` table now has all the data this would need, but the retry trigger/logic itself isn't built. (P2)
- [ ] **Job cancellation.** No way to cancel a queued or running job via the API — a mis-submitted 50-shot job has to run to completion or the whole process has to be killed. (P2)
- [ ] **Structured/queryable job history.** No endpoint to list jobs (only fetch by known ID) — now that persistence exists (`JobStore.list_jobs` already supports filtering by status), this is cheap to expose as a route. (P3)
- [ ] **Automated tests — still incomplete.** Now covers `schemas.py`, `comfyui_client.py`, `job_store.py`, and the shot-reconciliation branches in `storyboard.py` (32 cases total). Still untested: the worker's queue/failure semantics directly (only exercised indirectly via the recovery tests), and the pure functions `_naive_shot_split`/`_apply_shot_to_workflow` in `storyboard.py`. (P2)
- [ ] **CI.** No `.github/workflows/` or equivalent — nothing runs automatically on change. Cheap to add now (`pytest` on push/PR, 32 fast tests, no live services needed). (P2)
- [ ] **Frontend.** Explicitly out of scope so far per README ("No frontend yet — this is the orchestration backend only"). Not a bug, just the obvious next big chunk of work once the backend is hardened. (P2)
- [ ] **Request size limits.** No length cap on `prompt`/`script` free-text fields. (P3)
- [ ] **Dependency pinning/lockfile.** `requirements.txt` uses floors only (`>=`); no lockfile, so a fresh install on a new machine isn't guaranteed to reproduce the exact verified environment. (P2)
- [ ] **`generate_script` resume is always a full re-run.** Documented, permanent asymmetry (an LLM completion has no ComfyUI-`/history`-style idempotency to check) — not a gap to fix, just worth remembering if it's ever surprising in practice. (informational)

---

## Recommended next steps (in order)

1. ~~`git init` and commit the current working state as-is~~ — done 2026-07-17.
2. ~~Fix BUG-2 (path traversal in `workflow_name`)~~ — done 2026-07-17.
3. ~~Fix/mitigate BUG-1 (WebSocket keepalive drop)~~ — implemented, unit-tested, and **live-validated against a real ComfyUI instance 2026-07-17**. Closed.
4. ~~Fix BUG-4 (partial job results) + add job persistence~~ — both landed together 2026-07-17 as the Job Manager + Queue + Resume System milestone, live-validated (real backend-crash-mid-job recovery observed). Closed/done.
5. **Revisit CORS/auth (BUG-3)** — now the top open item. Before any deployment beyond a single trusted local machine.
6. **Add CI** running the test suite (32 fast cases, no live services needed) — cheap now that there's real coverage worth protecting.
7. **Expose a list-jobs endpoint** — `JobStore.list_jobs` already supports it internally (used by `recovery.py`); just needs a route.
8. **Extend test coverage** to the worker's queue/failure semantics directly and `storyboard.py`'s `_naive_shot_split`/`_apply_shot_to_workflow`.
9. Only after 5–8: start on the frontend — the API surface (job status shape, `project_id`, `shots`) just changed with this session's milestone, so building UI against it now is more stable ground than it was before.
