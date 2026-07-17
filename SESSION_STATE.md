# ManyTV — Session State

**Last updated:** 2026-07-17. Purpose of this file: let the next session (human or agent) pick up context immediately without re-deriving it. Update this file at the end of each work session — append a new dated entry to the Session Log rather than overwriting prior entries.

---

## Session Log

### 2026-07-17 ~11:18 — BUG-1 CLOSED: live-validated against a real ComfyUI instance

**Result: PASS.** Ran the full live-validation plan from the previous session's "exact next command." Created `.env` from `.env.example` (didn't exist yet). Started real ComfyUI (`scripts/run_comfyui.ps1`, confirmed Arc A750 / `xpu:0` / `pytorch_version 2.13.0+xpu` via `/system_stats`) and the real ManyTV backend, both as background processes with output redirected to log files for inspection. Ran a clean baseline 3-shot job first (`147c1e4e...`) with no interruption — completed successfully, confirming no regression from the BUG-1 code change.

Then attempted to force a real WebSocket disconnect without stopping ComfyUI. **Windows Firewall block rules were tested first and found to have zero effect** — confirmed empirically (added a real outbound block on port 8188, then successfully curled through it anyway) that Windows exempts loopback (`127.0.0.1`→`127.0.0.1`) traffic from normal firewall filtering for regular desktop processes. Documenting this as the requested limitation, then built a working alternative: a throwaway async TCP proxy (`tcp_proxy.py`, kept in the session scratchpad, **not part of the ManyTV codebase**) inserted between the backend and ComfyUI by temporarily pointing `.env`'s `COMFYUI_PORT` at the proxy and restarting the backend. Killing the proxy process and immediately starting a fresh one on the same port severs the backend's existing WebSocket connection (OS closes the dead proxy's sockets) while leaving ComfyUI's own process completely untouched, and lets subsequent HTTP polling succeed against the new proxy instance — a faithful simulation of "WS drops, ComfyUI and the path to it are still alive."

Submitted job `5cb0ab45-a739-4665-84ce-846cae8bc267`, waited for shot 0 to reach step 18/20, killed+restarted the proxy. Result: the backend logged `"WebSocket to ComfyUI dropped ... falling back to polling"`, polled `/history` three times at ~3.15s intervals (matching the configured 3.0s interval), found the completed entry, downloaded the file, and continued to shot 1 normally. Final job status: `done`, both shots' `.mp4` files present on disk and confirmed as valid (non-empty, real ISO Media/MP4 headers, not placeholders). Confirmed via ComfyUI's own log (`"Starting server"` appears exactly once for the whole session) that ComfyUI's process was never restarted or interrupted at any point.

All four documented failure conditions were checked explicitly and none occurred: prompt state wasn't lost (shot 0's real prompt_id and file were correctly recovered), no infinite retry loop (exactly 3 bounded polls), the job was not incorrectly marked failed (ComfyUI completing and the job completing matched), and `execution_error` handling wasn't disturbed by this change (though that specific guarantee is verified by the existing mocked unit test, not re-observed live, since no real execution error occurred during this session — noted as a scope boundary rather than claimed as directly proven live).

**BUG-1 flipped from MITIGATED to CLOSED in `TODO.md`.**

**Files changed:** none in the application/test source — this was a pure validation session. `TODO.md` and `SESSION_STATE.md` updated with the live evidence. `.env` was temporarily pointed at the test proxy's port and fully reverted to `COMFYUI_PORT=8188` afterward (`.env` is gitignored regardless, so this never touched git). No firewall rules were left behind (added and removed one test rule, confirmed cleanup). All background processes (ComfyUI, backend, proxy) were stopped at the end; verified only unrelated pre-existing processes (a different project, `chinesekhmerdubber_v1`) remained running, untouched.

**Remaining problems** (see `TODO.md` for full detail):
- BUG-3 (P1) — Wildcard CORS + zero authentication on every endpoint. Still open. (Incidentally, this session's Windows loopback-exemption finding is a mild point *in favor* of the current localhost-only deployment being less exposed than a naive read of BUG-3 might suggest for same-machine threats specifically — though it does nothing to mitigate the actual documented risk, which is any browser tab on this machine making a same-origin-exempt request to the API.)
- BUG-4 (P2) — Partial storyboard failure silently discards already-succeeded shots' results. Still open, and now the top priority.
- BUG-5 (P3) — Two redundant `ComfyUIClient` instances (cosmetic). Still open.
- Worker queue/failure semantics and `storyboard.py`'s pure functions (`_naive_shot_split`, `_apply_shot_to_workflow`) are still untested.
- Minor, non-blocking observation from this session: one shot's ComfyUI-side execution took 98s instead of the usual ~25s (mostly before KSampler steps started, i.e. model-reload overhead, not the sampler itself). Not investigated further — didn't cause any failure, just latency variance. Worth keeping in mind if timeouts ever need tightening.

**Exact next command/task:**
No live services needed for the next step. Pick up BUG-4 (`backend/api/routes/storyboard.py`, `_run_storyboard_job` and `worker.py`'s failure handling) — accumulate shot results as they complete and attach them to `job.result` even on failure, rather than only setting it on full success. Can be implemented and unit-tested without ComfyUI/backend running live.

---

### 2026-07-17 ~10:53 — Mitigated BUG-1 (WebSocket keepalive drop) — live validation still pending

**What changed today:** Checked whether BUG-1 could be reproduced live first, per the previous session's "exact next command." It couldn't: neither ComfyUI (`127.0.0.1:8188`) nor the ManyTV backend was running (both `curl` checks returned no connection; only Ollama at `11434` was up). Starting ComfyUI to force a real multi-minute GPU generation wasn't something to do unprompted, so instead of blocking on that, implemented the fix the diagnosis already pointed to: `ComfyUIClient.wait_for_completion` (`backend/core/comfyui_client.py`) now catches a dropped WebSocket and falls back to a new `_poll_history_until_done` method, which polls `/history/{prompt_id}` every 3s until ComfyUI records a finished entry, instead of failing the job outright. Rationale: ComfyUI keeps executing a queued prompt regardless of whether the monitoring socket stays connected, so a dropped socket was never actually proof the job failed. A real `execution_error` from ComfyUI (an actual generation failure) still raises immediately and is untouched by this change; a genuinely dead ComfyUI (not just a dropped socket) still fails fast because `get_history` then raises an `httpx` error, not a `ComfyUIError`, which isn't caught by the polling loop's retry. Verified with 4 new mocked unit tests — real behavior against a live ComfyUI instance has **not** been observed yet, since none was running this session.

**Files modified:**
- `backend/core/comfyui_client.py` — added `import asyncio`; `wait_for_completion`'s except-block now falls back to the new `_poll_history_until_done` instead of raising `ComfyUIError` directly; added `_poll_history_until_done`
- `tests/test_comfyui_client.py` (new) — 4 cases: polling retries until history appears, non-`ComfyUIError` propagates immediately, a dropped socket triggers the fallback, a real `execution_error` still raises immediately
- `TODO.md` — BUG-1 marked mitigated (not closed) with full detail; Completed/Current-tasks/Missing-features/Recommended-next-steps sections updated to match
- `SESSION_STATE.md` — this entry

**Remaining problems** (see `TODO.md` for full detail):
- **BUG-1 is mitigated but not closed** — the fix is implemented and unit-tested against mocks, but the actual failure was only ever observed against a real running ComfyUI, and the fix hasn't been run against one yet. This is the single most important thing to do next.
- BUG-3 (P1) — Wildcard CORS + zero authentication on every endpoint. Still open.
- BUG-4 (P2) — Partial storyboard failure silently discards already-succeeded shots' results. Still open, and now the most impactful remaining gap — pairs with BUG-1 validation since a mid-job failure after some shots succeed is exactly the scenario BUG-4 is about.
- BUG-5 (P3) — Two redundant `ComfyUIClient` instances (cosmetic). Still open.
- Worker queue/failure semantics and `storyboard.py`'s pure functions (`_naive_shot_split`, `_apply_shot_to_workflow`) are still untested.

**Exact next command/task:**
```powershell
# Terminal 1
D:\NewProjects\ComfyUI\.venv\Scripts\Activate.ps1
D:\NewProjects\ManyTV\scripts\run_comfyui.ps1 -ComfyUIPath D:\NewProjects\ComfyUI -VramHeadroomGB 2

# Terminal 2
D:\NewProjects\ManyTV\.venv\Scripts\Activate.ps1
copy .env.example .env   # if not already present
uvicorn backend.app:app --reload --port 8000
```
Then submit a real multi-shot `/api/storyboard` job (2-3 shots is enough) and watch the backend log for either a clean run or, if a WebSocket drop recurs, the new `"WebSocket to ComfyUI dropped ... falling back to polling"` line followed by the job still completing. Once that's observed, flip BUG-1 from "mitigated" to "closed" in `TODO.md`. Only after that (or if the user prefers to skip straight ahead): pick up BUG-4.

---

### 2026-07-17 ~10:40 — Fixed BUG-2 (path traversal in `workflow_name`)

**What changed today:** `git init` + baseline commit landed first (see prior log entry's "exact next command" — done as the first commit of this session, capturing the repo exactly as the audit left it, docs included). Then BUG-2 was fixed: `StoryboardRequest.workflow_name` (`backend/models/schemas.py`) now requires `^[A-Za-z0-9_-]+$`, rejecting `/`, `\`, `.`, spaces, and empty string at the Pydantic validation boundary before the value can reach `_load_workflow`'s filesystem path construction. Test tooling was bootstrapped from nothing: `pytest` added to `requirements.txt` and installed into `.venv`, first test module `tests/test_schemas.py` written (11 cases: default value, 8 rejected traversal/invalid inputs, 3 accepted valid names) and run — all passing. `.pytest_cache/` added to `.gitignore`.

**Files modified:**
- `backend/models/schemas.py` — added `pattern=r"^[A-Za-z0-9_-]+$"` to `StoryboardRequest.workflow_name`'s `Field(...)`
- `requirements.txt` — added `pytest>=8.0` under a new "Dev / test" section
- `tests/test_schemas.py` (new) — regression coverage for the fix
- `.gitignore` — added `.pytest_cache/`
- `TODO.md` — BUG-2 marked fixed with detail; completed/current-tasks/missing-features/recommended-next-steps sections updated to match
- `SESSION_STATE.md` — this entry

**Remaining problems** (unchanged in substance, see `TODO.md` for full detail):
- BUG-1 (P0) — ComfyUI WebSocket connection dies mid-job with "keepalive ping timeout", losing all remaining shots. Still open — this session did not touch it.
- BUG-3 (P1) — Wildcard CORS + zero authentication on every endpoint. Still open. Note: this is what made BUG-2 exploitable from a browser in the first place; fixing BUG-2 closed that specific input, not the exposure model.
- BUG-4 (P2) — Partial storyboard failure silently discards already-succeeded shots' results. Still open.
- BUG-5 (P3) — Two redundant `ComfyUIClient` instances (cosmetic). Still open.
- Test coverage is still minimal — only `tests/test_schemas.py` exists; the worker queue/failure semantics and `storyboard.py`'s pure functions (`_naive_shot_split`, `_apply_shot_to_workflow`) are still untested.

**Exact next command/task:**
No command needed yet — next is investigative, not a fixed command. Reproduce BUG-1 to confirm the root-cause hypothesis before writing a fix: run a real multi-shot `/api/storyboard` job (ComfyUI must be running first, per README) and watch whether the WebSocket drop recurs on any shot that takes long enough for ComfyUI to be busy through a keepalive interval. If it reproduces, check whether ComfyUI exposes a server-side ping/timeout flag before reaching for a client-side reconnect-and-resume workaround (see `TODO.md` BUG-1 "Fix direction").

---

### 2026-07-17 — Full-repo read-only architecture audit

**What changed today:** Nothing in the source tree. This was a read-only audit — every `.py` file, both markdown docs, the workflow JSON, the PowerShell launch script, `requirements.txt`/`.env.example`, and the runtime artifacts (`server.log`, `output/`, `.e2e_job_id`) were read and cross-referenced, including checking one crash in `server.log` against the actually-installed `websockets==16.1` source in `.venv` to confirm a real root-cause hypothesis rather than guessing. Verified via mtime check that no `backend/`, `scripts/`, or `workflows/` file was touched during or after the audit — all predate this session (05:30–06:59) except the three docs below. The user opened `scripts/run_comfyui.ps1` in the IDE during this session; that was a view, not an edit — file content and mtime are unchanged from before the audit.

**Files modified:**
- Created `PROJECT_ANALYSIS.md` — architecture, design rationale, dependency review, confirmed-working-behavior evidence, code quality assessment
- Created `TODO.md` — completed work, bugs (prioritized), missing features, recommended next steps in order
- Created `SESSION_STATE.md` — this file
- No files under `backend/`, `scripts/`, `output/`, or any config file were modified.

**Remaining problems** (see `TODO.md` for full detail — unchanged by this session, since it made no fixes):
- BUG-1 (P0) — ComfyUI WebSocket connection dies mid-job with "keepalive ping timeout", losing all remaining shots. Confirmed in `server.log`; root cause not yet fixed, only diagnosed.
- BUG-2 (P1) — `workflow_name` request field not sanitized before filesystem path construction (path traversal).
- BUG-3 (P1) — Wildcard CORS (`allow_origins=["*"]`) + zero authentication on every endpoint.
- BUG-4 (P2) — Partial storyboard failure silently discards already-succeeded shots' results from the job API response.
- BUG-5 (P3) — Two redundant `ComfyUIClient` instances constructed (cosmetic).
- **Project is still not a git repository** — no `.git/` exists. Nothing done in this session is under version control yet.

**Exact next command/task:**
```powershell
cd D:\NewProjects\ManyTV
git init
git add -A
git commit -m "Initial commit: working ManyTV backend (script gen + storyboard pipeline)"
```
Then proceed to `TODO.md` step 2: fix BUG-2 (sanitize `workflow_name` in `backend/api/routes/storyboard.py`).

---

## Repo state as of this session

- **Now a git repository.** `git init` + a baseline commit landed at the start of the 10:40 session entry above, followed by a second commit for the BUG-2 fix. Check `git log --oneline` for the current state rather than trusting this file if it's been a while — this section is a snapshot, not a live view.
- **`.venv/` exists and is populated** (backend deps only — `fastapi`, `uvicorn`, `httpx`, `websockets==16.1`, `openai`, `pydantic(-settings)`, **`pytest>=8.0` now too**, etc.). **No `torch`/XPU stack installed in this venv** — confirmed by directory listing, meaning `backend/core/gpu_memory.py::release_gpu_memory()` is currently running in its no-op (`gc.collect()`-only) fallback path, exactly as designed for a backend-only venv.
- **No `.env` file exists** — only `.env.example`. Whatever backend process produced `server.log` was either run with an `.env` that was later deleted, or with `config.py`'s built-in defaults (which match `.env.example` exactly, so behavior would be identical either way). If you need to run the backend again, `copy .env.example .env` first per README.
- **`server.log` (70 lines) captures one real backend process run**, uvicorn on port 8003 (not the README's example port 8000 — just an ad-hoc choice for that run, not a config issue). Lifespan startup succeeded and confirmed a reachable ComfyUI at `127.0.0.1:8188`.
- **One real job was submitted and partially completed:** `POST /api/storyboard` → job `fb4850f6-17c8-4798-881f-6321b45413b5`. Shot 0 fully succeeded (`output/fb4850f6-17c8-4798-881f-6321b45413b5/shot_000/ManyTV_00002.mp4` exists on disk — a real, valid SD1.5+AnimateDiff clip). Shot 1 was queued, then the job died with a WebSocket `ConnectionClosedError` ~50s later (see `PROJECT_ANALYSIS.md` §5 and `TODO.md` BUG-1 for full analysis). The job's final in-memory status was `FAILED`.
- **`.e2e_job_id`** at the repo root just contains that same job ID (`fb4850f6-17c8-4798-881f-6321b45413b5`) — a leftover scratch marker from that manual test, not application code. Safe to leave, delete, or gitignore once git is initialized; not investigated further as it's not source.
- **No processes are known to be running right now** from this session — the log's last line is a `GET /api/jobs/{id}` response for the failed job; there's no indication in-band of whether that uvicorn process (or ComfyUI) is still alive. **Verify with a fresh health check before assuming either service is up:**
  ```powershell
  curl http://127.0.0.1:8000/health        # or :8003 if that's still the live one
  curl http://127.0.0.1:8188/system_stats  # ComfyUI
  ```
- **ComfyUI itself** lives outside this repo at `D:\NewProjects\ComfyUI` (per README) — not audited in this session beyond what `backend/workflows/README.md` and `server.log` reveal about it. It has at least the SD1.5 checkpoint + AnimateDiff v3 motion module + AnimateDiff-Evolved + VideoHelperSuite custom nodes installed and working (confirmed by the successful shot 0).

---

## Open questions for the user / next session

These weren't answerable from the repository alone:

1. ~~Is the WebSocket keepalive drop (BUG-1) a one-off, or does it reproduce reliably on longer jobs?~~ Still not directly answered (ComfyUI wasn't running to test against), but no longer blocking — the fix implemented this session (poll `/history` as a fallback) is robust to the drop regardless of how often it recurs, since it doesn't depend on knowing the exact trigger. Live validation is still worth doing (see Session Log above) to confirm the fallback actually engages and completes the job, not to diagnose the trigger further.
2. Is this backend ever expected to be reachable from anywhere other than `127.0.0.1` on this one machine? That answer determines how urgently BUG-3 (wildcard CORS/no auth) needs fixing versus just documenting as "acceptable for the current single-machine, localhost-only deployment." (BUG-2, the path-traversal input itself, is fixed regardless.)

---

## Recommended entry point for next session

BUG-1 is closed. Pick up BUG-4 (`TODO.md` step 4) — no live services needed, can be implemented and unit-tested standalone.
