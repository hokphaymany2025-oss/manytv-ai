# ManyTV — Session State

**Last updated:** 2026-07-17. Purpose of this file: let the next session (human or agent) pick up context immediately without re-deriving it. Update this file at the end of each work session — append a new dated entry to the Session Log rather than overwriting prior entries.

---

## Session Log

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

- **Not a git repository.** No `.git/` directory exists, despite a correct `.gitignore` already being present. This is the top recommended first action in `TODO.md` — do it before any further changes.
- **`.venv/` exists and is populated** (backend deps only — `fastapi`, `uvicorn`, `httpx`, `websockets==16.1`, `openai`, `pydantic(-settings)`, etc.). **No `torch`/XPU stack installed in this venv** — confirmed by directory listing, meaning `backend/core/gpu_memory.py::release_gpu_memory()` is currently running in its no-op (`gc.collect()`-only) fallback path, exactly as designed for a backend-only venv.
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

1. Is the WebSocket keepalive drop (BUG-1) a one-off, or does it reproduce reliably on longer jobs? Worth attempting a fresh multi-shot run to see if it's timing-dependent (e.g., only shots that take >~50s trigger it) before investing in a fix.
2. Is this backend ever expected to be reachable from anywhere other than `127.0.0.1` on this one machine? That answer determines how urgently BUG-2/BUG-3 (path traversal + wildcard CORS/no auth) need fixing versus just documenting as "acceptable for the current single-machine, localhost-only deployment."
3. Was `git init` intentionally deferred, or just not gotten to yet? No evidence either way in the repo itself.

---

## Recommended entry point for next session

Run the exact next command/task logged above (`git init` + initial commit), then read `TODO.md`'s "Recommended next steps" section and continue from step 2 unless the user directs otherwise — everything else compounds risk the longer it stays undone.
