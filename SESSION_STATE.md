# ManyTV — Session State

**Last updated:** 2026-07-17. Purpose of this file: let the next session (human or agent) pick up context immediately without re-deriving it. Update this file at the end of each work session — append a new dated entry to the Session Log rather than overwriting prior entries.

---

## Session Log

### 2026-07-17 ~22:15 — Frontend wired to GET /api/jobs; first automated frontend tests

**What was completed:** User asked for the two prior sessions' work to be committed first (done — see the commit note at the end of this entry), then asked for the next feature. Left as `<TBD>` in their milestone-status message, so confirmed via `AskUserQuestion`: wiring the frontend to the new `GET /api/jobs` endpoint (top of `TODO.md`'s backlog) over the other option (pushing to a GitHub remote). User's message also explicitly asked for "Review architecture / Add feature plan / Define tests" before starting, so used Plan mode (overwriting the previous session's now-unrelated plan file) rather than just diving in.

- **`frontend/src/api.ts`**: added `listJobs(status?: string)` → `GET /api/jobs` or `GET /api/jobs?status=...`.
- **`frontend/src/useJobList.ts`** (new, replaces `useTrackedJobs.ts` which was deleted outright — not left as dead code): polls `listJobs()` every 3s, holds `jobs: JobStatusResponse[]` directly. `refresh()` fires on mount, on each interval tick, and immediately after a new job is submitted (so it appears without waiting for the next tick). `updateJob(job)` still merges a single already-known-updated job (from a retry/cancel response) in place, same as before.
- **`frontend/src/App.tsx`**, **`ScriptForm.tsx`**, **`StoryboardForm.tsx`**: `onSubmitted` prop simplified from `(jobId: string) => void` to `() => void` — the id is no longer needed by the caller now that there's no client-side tracking to add it to.
- No backend changes at all — `GET /api/jobs` already returned exactly the shape needed.

**First automated frontend tests** (none existed before this — the initial frontend milestone was verified via TypeScript compilation + one live Playwright session only): added `vitest` + `@testing-library/react` + `jsdom` as devDependencies, config folded into the existing `vite.config.ts` (`test: { environment: 'jsdom', globals: true }`, no second config file), new `"test": "vitest run"` script. `frontend/src/api.test.ts` (3 cases — unfiltered call, `?status=` query building, error-detail propagation) and `frontend/src/useJobList.test.tsx` (4 cases — initial-mount fetch, interval re-fetch, in-place `updateJob`, resilience to a failed poll). One real debugging note worth keeping: mixing `@testing-library/react`'s `waitFor` (which polls via *real* timers internally) with `vi.useFakeTimers()` hangs every assertion until vitest's own 5s test timeout — fixed by either not using fake timers at all (most tests) or engaging fake timers *before* the hook mounts and flushing the initial effect with `vi.advanceTimersByTimeAsync(0)` instead of `waitFor` (the interval test). `npm run test` → 7 passed. `npm run build` still compiles clean.

**Live validation, the real point of this session:** started a real backend + real Vite dev server, then used the same throwaway Playwright setup from an earlier session (still cached in the scratchpad) to drive **two independent browser contexts** (separate `localStorage`, standing in for two different browsers/machines) against the live dashboard. Browser A submitted a new job; polled browser A specifically for the top row's id to *change* from what it was before submitting (closes a real timing race an earlier, cruder version of this check had — comparing raw job-row counts between the two browsers is inherently racy right now anyway, since this backend still has genuinely queued/running jobs left over from earlier sessions' live tests, so total counts can legitimately drift between two measurements taken seconds apart regardless of anything this feature does). Once browser A's new job id was confirmed, polled browser B — which never submitted anything itself, in a fully separate context — and confirmed it showed that exact same job id within one poll interval, purely from `GET /api/jobs` server-side history. Zero console errors on a plain page load.

**Commits from the prior two sessions** (requested at the start of this session, before starting this new feature): `f8cc8e1` (frontend addition) and `b7e6a05` (CORS/CI/retry-cancel/BUG-6/list-jobs backend work) — see the git log for detail; not re-described here since they predate this session's actual work.

**Files changed this session:** `frontend/src/api.ts`, `frontend/src/useJobList.ts` (new), `frontend/src/useTrackedJobs.ts` (deleted), `frontend/src/App.tsx`, `frontend/src/components/ScriptForm.tsx`, `frontend/src/components/StoryboardForm.tsx`, `frontend/src/api.test.ts` (new), `frontend/src/useJobList.test.tsx` (new), `frontend/vite.config.ts`, `frontend/package.json`, `frontend/package-lock.json`, `TODO.md`, `SESSION_STATE.md`.

**Remaining problems / blockers:** None blocking.
- CI still never observed running on a real GitHub Actions job — this repo still has no git remote. Now the single top open item.
- No status-filter UI in the dashboard (backend supports `?status=`, not exposed yet) — small, optional, low priority.
- `storyboard.py`'s `_naive_shot_split`/`_apply_shot_to_workflow` still have no dedicated direct tests.
- BUG-5 (cosmetic) and a dependency lockfile still open.
- No live services running — backend (:8000) and Vite dev server (:5173) both stopped cleanly at the end; confirmed via `Get-NetTCPConnection` that both ports are free.
- This session's changes are **not yet committed** — only the prior two sessions' work was committed at the start of this session, before this feature began.

**Exact next task:** Push this repo to a GitHub remote and confirm `.github/workflows/ci.yml` actually fires — the last standing "never observed running for real" item. Independent of that: commit this session's frontend changes (not yet done).

---

### 2026-07-17 ~21:30 — List-jobs endpoint added, live-validated against real multi-session history

**What was completed:** Picked up the prior session's "Exact next task" — verified repo consistency first (git log/status matched what `SESSION_STATE.md` claimed exactly: 5 commits, all of the last two sessions' work still uncommitted; 72/72 tests passing; no stray backend/frontend processes listening), then implemented `GET /api/jobs`.

- **`backend/core/job_store.py`**: `list_jobs(status_in=...)` now defaults to `None`/optional — an omitted or empty filter means "every job, no `WHERE` clause" (existing caller `recovery.py` is unaffected, it always passes an explicit filter). Added `ORDER BY created_at DESC` to both the filtered and unfiltered query paths (newest first) — safe since no existing caller depended on row order.
- **`backend/api/routes/storyboard.py`**: new `GET /api/jobs` route (`list_jobs_route`), optional `?status=failed,cancelled`-style comma-separated query filter. `_build_job_status_response` (already shared by `get_job_status`/`retry_job`/`cancel_job`) gained an optional pre-fetched `job_row` parameter so the list route builds each response from the row it already has from its own `list_jobs()` call, instead of re-fetching by id per job (avoiding an N+1 query pattern).
- Tests: `tests/test_job_store.py` (+2 — no-filter-returns-everything, newest-first ordering) and `tests/test_job_routes.py` (+4 — unfiltered list, comma-separated status filter, empty-list case, shots included for storyboard jobs). Full suite: `python -m pytest tests/ -v` → **78 passed** (72 prior + 6 new).

**Live validation:** started a fresh real backend (clean startup, confirmed via `Get-NetTCPConnection` that port 8000 was free first — no repeat of the prior session's silent-stale-process mistake). Submitted two new real jobs, then called the new endpoint against the backend's **actual accumulated multi-session history** (20 real jobs persisted in `output/jobs.db` across this and every prior session's live tests, not synthetic test data): unfiltered `GET /api/jobs` returned all 20, correctly newest-first (the two just-submitted jobs at the top); `GET /api/jobs?status=failed` correctly returned exactly the 6 real jobs that had failed (all for the same real reason — ComfyUI not running). Stopped the test backend cleanly afterward, confirmed port 8000 free again.

**Files changed:** `backend/core/job_store.py`, `backend/api/routes/storyboard.py`, `tests/test_job_store.py`, `tests/test_job_routes.py`, `TODO.md`, `SESSION_STATE.md`.

**Remaining problems / blockers:** None blocking.
- The frontend still doesn't use this new endpoint — it still client-tracks submitted job ids via `localStorage` (from the prior session, before this endpoint existed). Updating it to show real server-side history is the natural next step and now the top open item.
- CI still never observed running on a real GitHub Actions job (no git remote configured).
- BUG-5 (cosmetic), `_naive_shot_split`/`_apply_shot_to_workflow` direct tests, and a dependency lockfile are all still open, all low priority.
- No live services running — the test backend was stopped cleanly at the end of this session.

**Exact next task:** Update `frontend/src/useTrackedJobs.ts` (or add a sibling hook) to call the new `GET /api/jobs` instead of (or in addition to) polling only `localStorage`-tracked ids — would let the dashboard show real job history from any client, not just what this specific browser submitted. Alternatively, push this repo to a GitHub remote to finally confirm CI fires for real — independent, either can go first.

---

### 2026-07-17 ~18:40 — Job retry + cancellation + first frontend milestone, live-validated (found & fixed BUG-6)

**What was completed:** User asked for three things bundled together: retry a `FAILED` job, job cancellation, and starting a frontend. Scope disambiguation took several rounds (the user's "stage 3/4/7" references turned out to map to `TODO.md`'s "Missing features" list, not the "Recommended next steps" roadmap I'd used earlier) — worth remembering that "stage N" isn't inherently anchored to one list in this repo. Given the size (three features, one of them open-ended), used Plan mode: three parallel `Explore` agents read `worker.py`/`job_store.py`, `storyboard.py`/`schemas.py`/`recovery.py`/`app.py`, and confirmed no frontend/Node tooling existed anywhere in the repo; `AskUserQuestion` settled frontend stack (React+Vite+TS), scope (minimal job dashboard), and the job-list-source gap (client-tracked via `localStorage`, no new list-jobs endpoint — deliberately not what "frontend" scope meant this time). A dedicated Plan agent then stress-tested the retry/cancel state-machine design and found two real gaps before any code was written (both incorporated into the plan, see `TODO.md`'s milestone entry for detail): a dequeue-time check that needed to treat `CANCELLING` the same as `CANCELLED` (else silently clobbered back to `RUNNING`), and retry needing to reject a job cancelled while still `QUEUED` (`started_at IS NULL`) to avoid a duplicate queue entry. Full plan: `C:\Users\hokph\.claude\plans\logical-mapping-biscuit.md`.

**Backend implementation:**
- `backend/core/worker.py`: new `JobStatus.CANCELLING`/`CANCELLED`, new `JobCancelled` exception. `_run()` now checks the persisted row before marking `RUNNING` (skips/finalizes an already-cancelled-or-cancelling job without invoking its handler) and catches `JobCancelled` ahead of the generic `except Exception` to finalize as `CANCELLED` rather than `FAILED`.
- `backend/core/job_store.py`: new `reset_job_for_retry`/`reset_shots_by_status` methods (explicit blanking of stale `error`/`result`, since `update_job_status`'s COALESCE semantics can't do this). Also gained a `threading.Lock` around `_run()`'s dispatch — see BUG-6 below.
- `backend/core/recovery.py`: fixed the Plan-agent-caught race in `_resume_comfyui_jobs` (re-fetches each row fresh instead of trusting a snapshot captured before a possibly-indefinite ComfyUI-health wait); `resume_incomplete_jobs`'s startup query now includes `CANCELLING`, finalized to `CANCELLED` rather than resubmitted.
- `backend/api/routes/storyboard.py`: cooperative cancellation check at the top of every shot-loop iteration (fires even on DONE-skipped ones); new `POST /api/jobs/{id}/retry` and `POST /api/jobs/{id}/cancel` routes; shared `_build_job_status_response` helper factored out of `get_job_status` so all three job routes return identically-shaped responses; new narrow `GET /api/jobs/{id}/files/{shot_index}/{filename}` download route reusing BUG-2's exact `Field(pattern=...)` technique (deliberately not a blanket `StaticFiles` mount, since `output/` also holds `jobs.db`).
- Tests: `tests/test_worker.py` (new, 9 cases), `tests/test_job_routes.py` (new, 17 cases, including one real `TestClient` call since `Path(..., pattern=...)` validation can't be exercised by calling a route function directly), `tests/test_job_store.py` (+5, including the BUG-6 concurrency regression test), `tests/test_recovery.py` (+6). Full suite: 72 passed.

**Frontend (`frontend/`):** React + Vite + TypeScript, scaffolded via `npm create vite@latest`. Node.js LTS wasn't installed anywhere on this machine — installed via `winget install OpenJS.NodeJS.LTS` with the user's explicit approval first (confirmed `winget` was available before asking, rather than asking blind). Minimal job dashboard: `ScriptForm`/`StoryboardForm` submit jobs, `useTrackedJobs` hook polls each tracked job id (persisted to `localStorage`) every 3s via the existing `GET /api/jobs/{id}`, `JobRow` shows status/shots/error and conditionally renders Retry/Cancel buttons and download links. TypeScript compiles clean, `npm run build` succeeds. `.env`/`.env.example` (both root and `frontend/`) updated so `CORS_ALLOWED_ORIGINS` includes the Vite dev server's origin.

**Live validation, not just tests — and this is what actually mattered this session:**
- Direct `curl` against a real running backend confirmed: cancelling a `QUEUED` generate_script job (real log line: `"was cancelled before it started running; skipping"`), a storyboard job failing for a real reason (ComfyUI down) then being retried and genuinely re-running (same real failure recurred, proving state was actually reset and re-executed, not faked), cancel-on-terminal-job → 409, download-missing-file → 404, and a real CORS preflight from the frontend's actual origin succeeding.
- `chromium-cli` (the `run` skill's preferred driver) wasn't available in this environment, so set up a throwaway Playwright install in the scratchpad instead and drove a real headless Chromium against the real dev server + real backend: submitted both job kinds through the actual UI, confirmed the job list live-polls to terminal states, confirmed the Retry button appears on a real `failed` job and clicking it genuinely re-queues it (status flips to `queued` in the UI), confirmed Cancel works on a real queued job, screenshotted throughout (`06-retry-button-visible.png` etc., in the session scratchpad).
- **This live pass caught a real bug the 71 mocked tests had not** (BUG-6, now in `TODO.md`): the first run showed browser console errors that looked like a CORS misconfiguration (`No 'Access-Control-Allow-Origin' header`). Chased it down via the actual backend log rather than trusting the browser's framing of the error, and found the real cause: `sqlite3.InterfaceError: bad parameter or other API misuse` — `JobStore`'s single `sqlite3.Connection`, used via `asyncio.to_thread` from multiple concurrent requests (exactly what the frontend's polling does, and nothing before this session ever did), was never actually thread-safe despite `check_same_thread=False` (that flag only disables Python's *check*, not real concurrency safety). Fixed with a `threading.Lock` in `JobStore._run()`. Confirmed the fix's regression test reliably reproduces the exact real error 3/3 times when the lock is removed, and passes clean with it restored. Re-ran the full Playwright session against the restarted, fixed backend: zero console errors.
- One process-management lesson from this session: a "restart the backend" attempt silently failed to take effect once (`pkill` didn't actually kill the old process; the new `uvicorn` hit `Errno 10048` port-in-use and never started, so curl kept hitting the stale process) — caught by checking `Get-NetTCPConnection`/`Get-Process` directly rather than assuming a background command's own "completed" notification meant the new process was live.

**Files changed:** `backend/core/worker.py`, `backend/core/job_store.py`, `backend/core/recovery.py`, `backend/api/routes/storyboard.py`, `tests/test_worker.py` (new), `tests/test_job_routes.py` (new), `tests/test_job_store.py`, `tests/test_recovery.py`, `tests/test_config.py` (one test fixed to use `_env_file=None`, since the real `.env` now legitimately sets `CORS_ALLOWED_ORIGINS` for frontend dev use), `frontend/` (new directory, full Vite+React+TS app), `.env` (local, added `CORS_ALLOWED_ORIGINS`), `.env.example`, `README.md` (new "Frontend" section, updated Endpoints table and "Known gaps" pointer), `TODO.md`, `SESSION_STATE.md`.

**Remaining problems / blockers:** None blocking.
- No list-jobs endpoint — now the single top-priority open item (`JobStore.list_jobs` already implemented internally). The frontend's job list is client-tracked via `localStorage` specifically because this doesn't exist yet.
- CI (`.github/workflows/ci.yml`) still has never been observed running on a real GitHub Actions job — this repo still has no git remote.
- `storyboard.py`'s `_naive_shot_split`/`_apply_shot_to_workflow` pure functions still have no dedicated direct tests (low priority — already exercised indirectly).
- BUG-5 (two redundant `ComfyUIClient` instances, cosmetic) still open.
- No dependency lockfile.
- Frontend is a genuine first pass only: no routing, no server-wide job history, cancellation of a `RUNNING` shot only takes effect between shots (no ComfyUI `/interrupt`), a `RUNNING` `generate_script` job can't be cancelled at all.
- All test/dev processes (backend on :8000, Vite dev server on :5173) were stopped cleanly at the end of this session; confirmed via `Get-NetTCPConnection` that both ports are free.

**Exact next task:** Expose `GET /api/jobs` (or similar) over the already-implemented `JobStore.list_jobs` — small, no live services needed. Would also let the frontend show real job history instead of only browser-local `localStorage` state. Push this repo to a GitHub remote is the other standing item (unblocks confirming CI actually runs).

---

### 2026-07-17 ~17:20 — CI added (GitHub Actions + requirements.txt split)

**What was completed:** Continued from the prior session's "Recommended entry point," which named CI as the next open item. Before implementing, explained current CI requirements, proposed a design, listed files that would change, and named risks (per the user's explicit request), then got approval on the recommended defaults before touching anything.

- **Requirements audit first:** confirmed no `.github/` directory and no git remote exists (`git remote -v` empty) — meaning a workflow file added now has nowhere to actually execute until this repo is pushed to a GitHub-hosted remote. Also found that `requirements.txt` bundled the Intel Arc/XPU `torch`/`torchvision`/`torchaudio` stack, which: (a) nothing in the 35-case test suite imports (confirmed by grep — only `backend/core/gpu_memory.py` touches `torch`, inside a defensive `try/except ImportError`), (b) isn't even installed in the real working dev venv per earlier session notes, and (c) would make CI download a multi-GB, hardware-specific stack for no reason.
- **`requirements-gpu.txt`** (new): the torch/XPU block, moved out verbatim (with updated comments) from `requirements.txt`. Install with `pip install -r requirements-gpu.txt`, only if real VRAM cleanup in `gpu_memory.py` is wanted instead of its no-op fallback.
- **`requirements.txt`**: torch block removed, replaced with a one-line pointer to `requirements-gpu.txt`.
- **`README.md`**: Setup step 1 gained a short note on the split and when `requirements-gpu.txt` is actually needed.
- **`.github/workflows/ci.yml`** (new): triggers on `push`/`pull_request` to `master` (this repo's actual current branch — there's no `main`), `ubuntu-latest`, Python 3.12 with pip caching, `pip install -r requirements.txt`, `pytest tests/ -v`. Kept deliberately minimal — no coverage/lint/badges — matching what `TODO.md` asked for.

**Live validation (of what could be validated without a remote):** built a completely fresh, throwaway venv (outside the repo, in the session scratchpad) and ran the exact install+test sequence CI will run: `pip install -r requirements.txt` (confirmed no `torch` in the resolved set, ~1m49s, all from PyPI) then `pytest tests/ -v` → **35 passed in 12.5s**, torch entirely absent from that venv. This proves the workflow's steps are correct and self-sufficient. **What this does *not* prove:** that GitHub Actions itself will actually run the workflow — this repo still has no git remote, so the YAML has never executed on a real GitHub-hosted runner. That's an explicit, named gap, not an oversight.

**Files changed:** `.github/workflows/ci.yml` (new), `requirements-gpu.txt` (new), `requirements.txt`, `README.md`, `TODO.md`, `SESSION_STATE.md`. No live services (ComfyUI/backend/Ollama) needed or started.

**Remaining problems / blockers:**
- **CI has never run for real** — push this repo to a GitHub remote and confirm the workflow actually fires before fully trusting it. This is the most important loose end from this session.
- BUG-5 (P3) — two redundant `ComfyUIClient` instances (cosmetic). Still open.
- No list-jobs endpoint — now the top-priority open item once CI is confirmed live (`JobStore.list_jobs` already supports the query internally).
- Worker queue/failure semantics and `storyboard.py`'s pure functions (`_naive_shot_split`, `_apply_shot_to_workflow`) still untested directly.
- Dependency lockfile still doesn't exist — CI installs whatever `requirements.txt`'s unpinned floors resolve to on the day it runs, unchanged by this session's work.
- No frontend yet.

**Exact next task:** Push this repo to a GitHub remote (or equivalent CI provider) and confirm `.github/workflows/ci.yml` actually runs and passes — that's the one thing this session couldn't validate directly. After that, per `TODO.md` "Recommended next steps" step 7: expose a `GET /api/jobs`-style list endpoint over `JobStore.list_jobs` (already implemented internally, just needs a route) — no live services needed.

---

### 2026-07-17 ~16:43 — BUG-3 CLOSED: CORS allowlist + host-binding hardening

**What was completed:** Continued from the prior session's "Recommended entry point," which named BUG-3 (wildcard CORS + no auth) as the next open item. Before implementing, asked the user (via `AskUserQuestion`) to resolve the open question left unanswered in this file's "Open questions" section — whether the backend needs to be reachable beyond this one machine. Answer: **localhost-only, single user, no LAN/remote access planned.** That confirmed scope: a CORS allowlist + host-binding fix, not a new auth mechanism (a shared-secret header would have been unnecessary work for the confirmed deployment model).

- **`backend/core/config.py`**: new `Settings.cors_allowed_origins` (comma-separated string, env var `CORS_ALLOWED_ORIGINS`, defaults to **empty**) and a `cors_allowed_origins_list` property that splits/strips/drops-empties into a `list[str]`.
- **`backend/app.py`**: `CORSMiddleware` now uses `settings.cors_allowed_origins_list` instead of `allow_origins=["*"]`; `allow_methods`/`allow_headers` narrowed from `["*"]` to `["GET", "POST"]`/`["Content-Type"]` — the only ones the API actually uses.
- **Host binding:** confirmed via `uvicorn --help` (installed version) that `uvicorn`'s own default is already `127.0.0.1` — the README's run command never overrode this, so no code change was needed, but README now documents this explicitly as a deliberate security boundary (new "CORS and network exposure" section) rather than an easily-forgotten default.
- **`.env.example`**: documented `CORS_ALLOWED_ORIGINS` with the empty default and an example for a future local frontend dev server.
- **README.md**: fixed staleness unrelated to BUG-3 but noticed while touching this file — "Known gaps" still described job state as in-memory-only (false since the Job Manager milestone) and the shipped workflow as missing/named `default_i2v.json` (false; `backend/workflows/default_t2v.json` is the real, shipped, verified-working default). Replaced with a pointer to `TODO.md` as the single source of truth instead of a second copy of status that can drift. Fixed the `default_i2v` → `default_t2v` references in the "Add a workflow" and example-curl sections too.

**Tests:** `tests/test_config.py` (new, 3 cases — empty default, comma-separated parsing, whitespace/empty-entry handling). Full suite: `python -m pytest tests/ -v` → **35 passed** (32 prior + 3 new).

**Live validation (not just unit tests):** built the real `FastAPI` app via `TestClient` and sent an actual cross-origin preflight (`OPTIONS /api/storyboard`, `Origin: http://evil.example`). Pre-fix this would have returned `200` with `Access-Control-Allow-Origin: *`; post-fix it returned `400` with no ACAO header — confirming the browser-side attack vector that made BUG-2 exploitable is now closed. A same-machine, no-`Origin`-header request (`GET /health`) still returned `200` normally, confirming the fix doesn't break legitimate same-machine use.

**Files changed:** `backend/core/config.py`, `backend/app.py`, `.env.example`, `README.md`, `tests/test_config.py` (new), `TODO.md`, `SESSION_STATE.md`. No live services (ComfyUI/backend/Ollama) were needed or started this session — config + route-level work only, as anticipated.

**Remaining problems / blockers:** None. Open items, unchanged in kind from before this session except where noted:
- BUG-5 (P3) — two redundant `ComfyUIClient` instances (cosmetic). Still open.
- No CI yet — now the top-priority open item (see `TODO.md` "Recommended next steps" step 6).
- No list-jobs endpoint (though `JobStore.list_jobs` already supports the query internally).
- Worker queue/failure semantics and `storyboard.py`'s pure functions (`_naive_shot_split`, `_apply_shot_to_workflow`) still untested directly.
- No frontend yet. Note for whenever that starts: its dev origin will need adding to `CORS_ALLOWED_ORIGINS`.

**Exact next task:** Per `TODO.md`'s "Recommended next steps" step 6, add CI (GitHub Actions or equivalent) running `pytest` on push/PR — no live services needed, all 35 cases run against mocks/tmp fixtures. Step 7 (list-jobs endpoint) is a similarly small, independent next choice if CI isn't the priority.

---

### 2026-07-17 ~13:13 — Job Manager + Queue + Resume System milestone, live-validated

**What was completed:** Designed (via plan mode, approved by the user) and implemented persistent job/shot state, replacing the in-memory-only `SingleSlotWorker._jobs` dict that lost everything on restart. Full design doc: `C:\Users\hokph\.claude\plans\warm-prancing-karp.md`.

- **`backend/core/job_store.py`** (new): `JobStore`, a thin `sqlite3` wrapper (stdlib, no new dependency — every call goes through `asyncio.to_thread`), WAL mode, one connection, schema = `jobs` table (id, kind, status, project_id, workflow_name, payload, result, error, timestamps) + `shots` table (job_id, shot_index, status, prompt_id, files, error, timestamps), `PRIMARY KEY (job_id, shot_index)`. File: `output/jobs.db` (new `Settings.db_path`, documented in `.env.example`).
- **`backend/core/worker.py`**: `JobStatus` gained `RESUMING`; new `ShotStatus` enum (`PENDING/SUBMITTED/DONE/FAILED`); `SingleSlotWorker` now writes to `JobStore` at its 3 job-level transitions (create/RUNNING/DONE-FAILED) and gained `resubmit()` for re-enqueuing a job reconstructed from a persisted row. Removed the now-dead `_jobs` in-memory dict and `get_job()` accessor — persisted state is the sole source of truth for querying a job now.
- **`backend/api/routes/storyboard.py`**: `_run_storyboard_job` refactored to read/write shot state from `JobStore` on every invocation (fresh and resumed jobs share the exact same code path — a fresh job just has no persisted shots yet). Three reconciliation branches for a shot found `SUBMITTED` from a prior process: history found on ComfyUI → reuse it, don't resubmit (this workflow isn't guaranteed deterministic — resubmitting could silently produce a different video); history absent (`ComfyUIError`) → safe to resubmit fresh; any other error (ComfyUI unreachable) → propagate, don't guess. `GET /api/jobs/{id}` now reads from `JobStore` and builds `shots`/`result` from persisted shot rows at read time — this is also what closes BUG-4 (partial progress visible mid-`RUNNING`, not just after failure).
- **`backend/core/recovery.py`** (new): `resume_incomplete_jobs()`, called from `app.py`'s `lifespan` after `worker.start()`. Non-`storyboard` (i.e. `generate_script`) jobs resubmit immediately, no ComfyUI dependency. `storyboard` jobs needing ComfyUI reconciliation are resumed from a background `asyncio.create_task` that polls `health_check()` and waits **indefinitely** (confirmed decision, not a timeout) rather than blocking FastAPI's own startup/readiness — `GET /health` stays 200 regardless of ComfyUI's state.
- **`backend/models/schemas.py`**: `project_id` (free-form, caller-supplied, no backing `Project` entity — confirmed decision) added to both request kinds; `JobStatusResponse` gained `project_id`, `workflow_name`, `shots: Optional[list[ShotStatusResponse]]`.
- Decisions confirmed with the user before implementation (via `AskUserQuestion` during planning): `project_id` as free-form tag not a real entity; fold BUG-4 into this milestone rather than patch separately; wait indefinitely for ComfyUI on resume, no auto-fail timeout; stdlib `sqlite3` not `aiosqlite`.

**Tests:** `tests/test_job_store.py` (7 cases) and `tests/test_recovery.py` (10 cases, covering all three shot-reconciliation branches plus recovery's job-level routing) — both new, both passing. Full suite: `python -m pytest tests/ -v` → **32 passed**.

**Live validation (not just mocks) — three real runs against the actual ComfyUI/Arc A750 stack:**
1. Baseline 3-shot job, no interruption → completed cleanly (no regression).
2. Restarted the backend against an already-`DONE` job → correctly ignored by recovery (only `QUEUED`/`RUNNING` are picked up), still fully queryable via the API after restart with `project_id`/`workflow_name`/`shots` all intact.
3. **The real test:** submitted a job, let shot 0 finish and shot 1 reach `SUBMITTED` (a real ComfyUI `prompt_id` in flight), then killed the **backend process only** (`Stop-Process -Force`, ComfyUI untouched) and restarted it ~40s later. Backend log on restart:
   ```
   manytv.recovery: Found 1 incomplete job(s) from a previous run; resuming.
   manytv.recovery: Job bc89fff6... was mid-flight when the backend last stopped; marked RESUMING.
   manytv.worker: Resumed job bc89fff6...
   manytv.api.storyboard: Job bc89fff6... shot 1: found existing ComfyUI history for prompt
     efa56da6..., reusing it instead of resubmitting.
   manytv.api.storyboard: Job bc89fff6... shot 1 done: 1 file(s) saved to .../shot_001
   manytv.api.storyboard: Job bc89fff6...: shot 2 queued as ComfyUI prompt 3f30934b...
   manytv.worker: Job bc89fff6... completed in 26.6s.
   ```
   Shot 1 was correctly reconciled (not resubmitted — its ComfyUI history had already finished during the gap) and shot 2 (never reached before the crash) proceeded normally. Final job: `status: done`, all 3 shots `done` with real files on disk, confirmed via `GET /api/jobs/{id}`.

**Note on why the first interruption attempt didn't count:** an earlier attempt at run 3 killed the backend too late — the job had already fully completed by the time the kill command was issued (too much latency between reading a progress log and issuing the next tool call). Documented plainly rather than silently discarded; the retry above is the one that actually exercised the resume path.

**Files changed:** `backend/core/job_store.py` (new), `backend/core/recovery.py` (new), `backend/core/worker.py`, `backend/core/config.py`, `backend/api/routes/storyboard.py`, `backend/api/routes/generate_script.py`, `backend/app.py`, `backend/models/schemas.py`, `.env.example`, `.gitignore` (added `.claude/settings.local.json` — a harness-local permissions file, not project source, that appeared as untracked during this session), `tests/test_job_store.py` (new), `tests/test_recovery.py` (new), `TODO.md`, `SESSION_STATE.md`.

**Remaining problems / blockers:** None blocking — no known bugs introduced. Open items, unchanged in kind from before this session except where noted:
- BUG-3 (P1) — wildcard CORS + zero authentication on every endpoint. Still open, now the top-priority open item (see TODO.md "Recommended next steps").
- BUG-5 (P3) — two redundant `ComfyUIClient` instances (cosmetic). Still open.
- No CI. No list-jobs endpoint (though `JobStore.list_jobs` already supports the query internally). Worker queue/failure semantics and `storyboard.py`'s pure functions (`_naive_shot_split`, `_apply_shot_to_workflow`) still untested directly. No frontend yet.
- New, small, explicitly-scoped-out item: retrying a `FAILED` job (genuine shot failure, not process interruption) from its last successful shot is not built — the `shots` table has the data for it, but the trigger/logic doesn't exist. Distinct from this session's resume system, which only covers process-interruption recovery.

**Exact next task:** Per `TODO.md`'s "Recommended next steps," pick up BUG-3 (CORS/auth hardening) — no live services needed to start that work (it's config + route-level, not GPU-dependent). Alternatively, adding CI (running the now-32-case test suite on push) is a quick, independent win that could be done first or in parallel.

---

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
2. ~~Is this backend ever expected to be reachable from anywhere other than `127.0.0.1` on this one machine?~~ **Answered 2026-07-17 (see `~16:43` Session Log entry): no** — confirmed localhost-only, single-user, no LAN/remote access planned. BUG-3 fixed accordingly (CORS allowlist + host-binding, no new auth layer). Revisit this answer together with the fix if the deployment model ever changes.

---

## Recommended entry point for next session

BUG-1 through BUG-6 are all closed; job persistence + resume, CORS/auth hardening, CI, job retry/cancellation, a first frontend, a list-jobs endpoint, and the frontend using that endpoint are all done and live-validated (see the `~22:15` Session Log entry above for full detail — that entry, plus the ones below it, supersede the "Repo state"/"Open questions" sections further down, which are historical snapshots and no longer current). Current state in brief:
- Two commits landed at the start of the `~22:15` session (`f8cc8e1` frontend, `b7e6a05` backend hardening) covering everything through the list-jobs-endpoint session. **This session's own changes (frontend wired to GET /api/jobs + its first test suite) are not yet committed.** Verify fresh with `git log --oneline` / `git status` rather than trusting this file.
- `output/jobs.db` (SQLite, gitignored) holds real persisted job history spanning many sessions' live tests.
- **No live services running** — backend (:8000) and Vite dev server (:5173) both stopped cleanly at the end of the last session; confirmed via `Get-NetTCPConnection`.
- **This repo has no git remote.** CI (`.github/workflows/ci.yml`) still has never been observed running on a real GitHub Actions job — still dry-run-validated locally only. Now the single top open item.
- Backend: 78 tests passing (`python -m pytest tests/ -v`). Frontend: 7 tests passing (`cd frontend && npm run test`), first automated frontend suite.

**Exact next task:** Push this repo to a GitHub remote and confirm `.github/workflows/ci.yml` actually fires — the last standing "never observed running for real" item, and now the only thing left on the main backlog. Commit the current session's frontend changes first (not yet done).

**Commands to resume:**
```powershell
cd D:\NewProjects\ManyTV
git log --oneline -5              # confirm what's actually committed
git status                        # confirm working tree state
python -m pytest tests/ -v        # confirm still 78 passed
cd frontend && npm run test       # confirm still 7 passed
```
