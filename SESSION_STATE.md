# ManyTV — Session State

**Last updated:** 2026-07-19 (Phase 4: mid-run generate_script cancellation) — implemented and tested on `feature/v1.3-planning`, not yet committed. All three Phase 4 engineering items are now done. Purpose of this file: let the next session (human or agent) pick up context immediately without re-deriving it. Update this file at the end of each work session — append a new dated entry to the Session Log rather than overwriting prior entries.

---

## Session Log

### 2026-07-19 (Phase 4: mid-run generate_script cancellation) — Last Phase 4 engineering item implemented; a real deadlock caught and fixed along the way

**What was completed:** Third and last Phase 4 engineering item, following retention and `/interrupt` support. User asked for a fresh design pass first (worker.py's lifecycle, job status transitions), then approved implementing it directly.

**Design:** `generate_script`'s handler is one blocking `await self._client.chat.completions.create(...)` call with no internal checkpoint — the only way to actually stop it mid-flight is asyncio-level task cancellation, not a cooperative flag check. This required restructuring `SingleSlotWorker._run()`: the handler call now runs as its own wrapped `asyncio.Task` rather than a bare `await`, so a specific job's execution can be cancelled independently of the worker's own outer loop task. New public `cancel_current_job(job_id) -> bool`.

**A real bug found and fixed during implementation, not caught by reasoning alone:** the first version disambiguated "this job's task was cancelled" from "the worker's own outer task was cancelled (`worker.stop()`)" by checking `task.cancelled()` after catching `CancelledError`. This **completely hung `worker.stop()`** — a genuine deadlock, discovered when a test timed out rather than passing or failing cleanly. Root cause: `asyncio.Task.cancel()` automatically propagates to whatever a task is currently suspended on (its `_fut_waiter`) — since `_run()` awaits the inner per-job task directly, cancelling the *outer* task also cancels the *inner* one as an automatic side effect of asyncio's own cancellation machinery, making both scenarios produce an identical `task.cancelled() == True`. There was no way to distinguish them from the task's own state alone. Fixed by tracking explicit intent instead: a new `self._cancel_requested_for: Optional[str]` flag, set by `cancel_current_job()` immediately before it calls `.cancel()`, checked in `_run()`'s except block rather than inferred from ambiguous task state.

**Implementation:**
- `backend/core/worker.py`: `_current_job`/`_current_job_task`/`_cancel_requested_for` tracked on the instance; `_run()`'s try/except restructured around the wrapped task; new `cancel_current_job(job_id)`.
- `backend/api/routes/storyboard.py`: `cancel_job`'s old unconditional 409 for a running `generate_script` job replaced with the same CANCELLING-first structure `storyboard` jobs already use (mirrors `request_shot_interrupt`).
- `README.md`'s endpoint table updated — both halves of the old "storyboard not mid-generation" / "generate_script can't be cancelled" claim were stale after this and the `/interrupt` work.
- Tests: `tests/test_worker.py` — the old `test_asyncio_cancelled_error_still_propagates` no longer modeled the new architecture correctly (a handler directly raising `CancelledError` is now indistinguishable from a legitimate targeted cancel, since the handler runs in its own task either way) — replaced with a test that actually cancels the *outer* task while a long-running handler is mid-flight, confirming both that cancellation genuinely propagates and that the inner task gets cancelled too. Plus 3 new `cancel_current_job` cases. `tests/test_job_routes.py`: the old `..._is_rejected` test replaced with `..._sets_cancelling`, plus a new test confirming `cancel_current_job` is actually called with the right job id.

**Verification:** `python -c "from backend.app import app"` clean. `python -m pytest tests/ -v` → **192 passed** (188 prior + 4 net new). `npm run test`/`build`/`lint` unaffected, re-confirmed green.

**Files changed:** `backend/core/worker.py`, `backend/api/routes/storyboard.py`, `README.md`, `tests/test_worker.py`, `tests/test_job_routes.py`, `TODO.md`, `SESSION_STATE.md`. No frontend files.

**Remaining problems / blockers:** None blocking.
- **This session's changes are not yet committed** — waiting for approval, per this project's standing convention.
- **All three Phase 4 engineering items are now done** (retention, `/interrupt`, generate_script cancellation) — none live-validated against real ComfyUI/Ollama instances yet; matching this project's established practice, that's the natural follow-up, not a blocker to calling the code itself done.
- **Anything multi-user/remote-access** remains explicitly gated on revisiting BUG-3's scope decision — untouched.
- `feature/v1.3-planning` not yet merged to `master`; `origin/feature/v1.2-development` (old, fully-merged branch) still exists, deletion still an open low-priority decision from an earlier session.

**Exact next task:** Get approval to commit this session's changes, then push. Independently: live-validate both new cancellation paths against real ComfyUI/Ollama, or decide when to merge `feature/v1.3-planning` into `master`.

---

### 2026-07-19 (Phase 4: real ComfyUI /interrupt support) — Immediate mid-generation cancellation implemented on `feature/v1.3-planning`

**What was completed:** Second Phase 4 item, following the retention policy. User first asked for an analysis-only pass ("study worker.py, comfyui_client.py, job status transitions; design first; do not modify code") — read `worker.py`, `comfyui_client.py`, `storyboard_engine.py`, `storyboard.py`'s `cancel_job` route, and (critically) **the actual installed ComfyUI source at `D:\NewProjects\ComfyUI`** (`server.py`, `execution.py`, `comfy_execution/jobs.py`), not general docs — matching this project's own established standard from BUG-1's investigation.

**Real findings from that verification, not assumed:**
- The right endpoint is `POST /api/jobs/{prompt_id}/cancel` (atomic, `interrupt_if_running` under ComfyUI's own queue mutex) — not the legacy global `POST /interrupt`, whose own prompt_id check isn't atomic with the interrupt itself.
- An interrupted prompt sends a distinct `execution_interrupted` WS message, which `wait_for_completion` had no handling for at all — it fell through to the terminal "executing, node=None" event (confirmed to fire unconditionally regardless of success/failure/interrupt) and returned whatever `/history` held as a false success.
- ComfyUI's `/history` status doesn't distinguish "interrupted" from "genuinely failed" (`completed: False` either way) — and this exact ambiguity was **already a latent, pre-existing gap** in the WS-drop polling fallback (BUG-1's path) and the SUBMITTED-shot resume-reconciliation branch, neither of which checked `status.completed` before. Confirmed via `AskUserQuestion` to fold this fix into the same change (recommended: leaving it unfixed would make the new interrupt feature silently misbehave under a WS-drop).

**Design, approved via a second `AskUserQuestion`** (folding in the history-completeness fix): job-level cancellation intent is disambiguated by re-checking the job's own persisted `CANCELLING` status at the moment an interrupt exception is caught, not by exception type alone — correct even in the edge case of a real workflow bug coinciding with a cancel request, and correct for the case of something *external* to ManyTV interrupting a prompt (e.g. ComfyUI's own UI stop button), which should still end the job `FAILED`, not silently `CANCELLED`.

**Implementation:**
- `backend/core/comfyui_client.py`: new `ComfyUIInterrupted(ComfyUIError)`; new `cancel_prompt(prompt_id) -> bool`; `wait_for_completion` gains an `execution_interrupted` branch; new `_raise_if_history_incomplete(history, prompt_id)` wired into `_poll_history_until_done` and the resume-reconciliation branch only (not `get_history()`/the WS success path directly — provably unreachable there once `execution_interrupted` is handled, and baking it into `get_history()` itself would have broken `_poll_history_until_done`'s existing "any ComfyUIError = keep polling" retry loop).
- `backend/core/storyboard_engine.py`: new `request_shot_interrupt(job_id)` (best-effort, swallows ComfyUI-unreachable errors); new `except ComfyUIInterrupted` branch in `_run_storyboard_job` before the generic `except Exception`, implementing the CANCELLING-vs-not disambiguation above; the resume-reconciliation branch now treats a found-but-incomplete history the same as "not found" (resubmit) — required a small but real fix: validating completeness *before* assigning to the outer `history` variable, since a bare re-check after the fact would have left `history` non-`None` on the exception path and silently skipped resubmission.
- `backend/api/routes/storyboard.py`: `cancel_job` calls `request_shot_interrupt` after setting `CANCELLING`, for `kind == "storyboard"` only — one narrow import, matching the Phase 3 refactor's discipline of not reintroducing `ComfyUIClient`/`Settings` into the routes file.
- **Real test-isolation bug found and fixed while writing tests**: `tests/test_job_routes.py`'s existing cancel tests were silently reaching the *real* global `JobStore` via `request_shot_interrupt`'s own `get_job_store()` module-level binding — the exact class of gap the Phase 3 refactor's own notes already named (a patched name in one module doesn't affect the same name bound in another). Fixed by also patching `storyboard_engine_module.get_job_store` in that file's shared `_patch_store_and_worker` helper.
- New `tests/test_cancellation.py` (16 cases) covering all three layers: `comfyui_client.py` (`cancel_prompt` success/no-op/unreachable, `execution_interrupted` → `ComfyUIInterrupted`, `_raise_if_history_incomplete` via `_poll_history_until_done` for absent-status/complete/incomplete histories), `storyboard_engine.py` (`request_shot_interrupt`'s three cases; `_run_storyboard_job`'s CANCELLING-vs-not branches — the CANCELLING case built by flipping the job's status *during* the mocked `wait_for_completion` call, to genuinely simulate the real race rather than short-circuiting on the shot loop's pre-existing top-of-iteration check; the resume-reconciliation resubmit-on-incomplete-history case), and the `cancel_job` route.

**Verification:** `python -c "from backend.app import app"` confirmed clean. `python -m pytest tests/ -v` → **188 passed** (172 prior + 16 new). `npm run test`/`build`/`lint` unaffected (backend-only change), re-confirmed green.

**Files changed:** `backend/core/comfyui_client.py`, `backend/core/storyboard_engine.py`, `backend/api/routes/storyboard.py`, `tests/test_job_routes.py`, `tests/test_cancellation.py` (new), `TODO.md`, `SESSION_STATE.md`. No frontend files.

**Remaining problems / blockers:** None blocking.
- **This session's changes are not yet committed** — waiting for approval, per this project's standing convention (`/implement` doesn't commit).
- **Not live-validated against a real running ComfyUI** — needs a real mid-generation cancel to confirm the shot actually aborts in seconds rather than waiting for `GENERATION_TIMEOUT_SECONDS`. Matching this project's established practice (see BUG-1's history), this is the natural follow-up, not a blocker to calling the code itself done.
- Phase 4's last remaining engineering item (mid-run `generate_script` cancellation) is still open, independent of everything above.
- Multi-user/remote-access work remains explicitly gated on revisiting BUG-3's scope decision — untouched.
- `feature/v1.3-planning` is pushed (`64cdf90`) but this session's new commit isn't yet; `origin/feature/v1.2-development` (old, fully-merged branch) still exists, deletion still an open low-priority decision from an earlier session.

**Exact next task:** Get approval to commit this session's `/interrupt`-support changes on `feature/v1.3-planning`, then push. Independently: live-validate against a real ComfyUI instance, or pick up mid-run `generate_script` cancellation next.

---

### 2026-07-19 (checkpoint, 10) — State verification only, no code changes

**What was completed:** Ran `/checkpoint`, continuing from "Phase 4 kickoff: retention policy" immediately below. Found the retention-policy changes already committed (`85495bd`, "Add job retention policy") — committed out-of-band since the last turn, not via this conversation's own `/commit`. No application logic touched this session.

- **Branch:** `feature/v1.3-planning`, tip `85495bd`. **No upstream tracking** (`git branch -vv` shows no `[origin/...]`) — this branch is local-only, never pushed.
- **Working tree:** clean, nothing uncommitted.
- **Backend tests:** `python -m pytest tests/ -v` → **172 passed**, 1 pre-existing warning — matches (164 prior + 8 new from the retention policy).
- **Frontend tests:** `npm run test -- --run` → **58 passed** (11 files) — unaffected, matches.
- **Frontend build:** `npm run build` → clean.
- **Frontend lint:** `npm run lint` → clean except the same 2 pre-existing warnings (`JobTimeline.tsx`, `JobArtifacts.tsx`, `react(only-export-components)`).

**Files changed:** `SESSION_STATE.md` only (this entry).

**Current task:** None in progress — verification-only pass.

**Remaining problems / blockers:** None new.
- **`feature/v1.3-planning` has never been pushed** — no remote copy exists at all yet, unlike every prior branch this session dealt with.
- Phase 4's other two engineering items (real ComfyUI `/interrupt` support, mid-run `generate_script` cancellation) remain open, either pickable independently.
- Multi-user/remote-access work remains explicitly gated on revisiting BUG-3's scope decision.
- `origin/feature/v1.2-development` (old, fully-merged branch) still exists on `origin` — deletion still an open, low-priority decision from an earlier session.

**Exact next task:** Decide whether to push `feature/v1.3-planning`, and separately, whether to continue with another Phase 4 item (`/interrupt` support or mid-run cancellation) next.

---

### 2026-07-19 (Phase 4 kickoff: retention policy) — Job/log retention policy implemented on `feature/v1.3-planning`

**What was completed:** First real Phase 4 work. `feature/v1.3-planning` turned out to be a branch the user had already created outside this conversation (found via `git status`/`git branch -vv` after a prior `/analyze`) — confirmed its purpose via `AskUserQuestion` before touching it: start scoping Phase 4 here. A second `AskUserQuestion` clarified that Phase 4 bundles four items, and only "multi-user/remote-access" actually requires revisiting BUG-3's no-auth/localhost-only decision — the other three (`/interrupt`, mid-run `generate_script` cancellation, retention policy) are pure engineering with no security/scope implications. User picked **retention policy** as the first item.

Presented a design (files to change, reasoning, risks) before writing code, per `/implement`'s explicit "wait for approval" step, since this is inherently destructive (permanently deletes job history and generated files). A third `AskUserQuestion` resolved the one open design choice: prune **startup-only** (matching `recovery.py`'s existing pattern), not also via an on-demand endpoint.

**Implementation:**
- `backend/core/config.py`: new `job_retention_days: int = 0` (0 = disabled by default — opt-in, not silently active, matching this project's caution around destructive operations). `.env.example` documents it.
- `backend/core/job_store.py`: new `prune_old_jobs(older_than_days)` — deletes `job_logs`/`job_attempts`/`shots`/`jobs` rows for `done`/`failed`/`cancelled` jobs older than the cutoff (never `queued`/`running`/`resuming`/`cancelling`, regardless of age); returns the pruned job ids so the caller can also remove output files (JobStore has no filesystem knowledge of its own).
- New `backend/core/retention.py` — thin orchestrator mirroring `recovery.py`'s shape: calls `prune_old_jobs`, then `shutil.rmtree(ignore_errors=True)`s each pruned job's `output/<job_id>/` directory. No-ops if retention is disabled.
- `backend/app.py`: wired into `lifespan`, right after `resume_incomplete_jobs` — disjoint from crash recovery by construction (recovery only touches non-terminal jobs, pruning only touches terminal ones).
- Tests: 4 new in `tests/test_job_store.py`, 4 new in `tests/test_retention.py` (real `JobStore`/real filesystem under `tmp_path`, matching `test_recovery.py`'s convention rather than mocking).

**Verification:** `python -c "from backend.app import app"` confirmed the import chain resolves cleanly. `python -m pytest tests/ -v` → **172 passed** (164 prior + 8 new). `npm run test` → 58 passed (unaffected, backend-only change). `npm run build` clean. `npm run lint` clean (2 pre-existing warnings only).

**Files changed:** `backend/core/config.py`, `backend/core/job_store.py`, `backend/core/retention.py` (new), `backend/app.py`, `.env.example`, `tests/test_job_store.py`, `tests/test_retention.py` (new), `TODO.md`, `SESSION_STATE.md`. No frontend files touched.

**Remaining problems / blockers:** None blocking.
- **This session's changes are not yet committed** — waiting for approval, per this project's standing convention (`/implement` doesn't commit).
- `feature/v1.3-planning` itself is still local-only, not pushed.
- Phase 4's other two engineering items (real ComfyUI `/interrupt` support, mid-run `generate_script` cancellation) remain open, either independently pickable next.
- Multi-user/remote-access work remains explicitly gated on revisiting BUG-3's scope decision — not touched this session.
- `origin/feature/v1.2-development` (the old, fully-merged branch) still exists on `origin` — its deletion is still an open, low-priority decision from an earlier session, unrelated to this one.

**Exact next task:** Get approval to commit this session's retention-policy changes on `feature/v1.3-planning`. Once approved: push the branch, and independently pick up either of Phase 4's remaining engineering items (`/interrupt` support or mid-run cancellation) next — no dependency between them.

---

### 2026-07-19 (post-merge cleanup, checkpoint 9) — Now on `master`; local feature branch deleted, remote deletion pending

**What was completed:** Continued from "v1.2 merged to master." User confirmed (via `AskUserQuestion`) to do pure housekeeping only — no Phase 4 conversation started. Pushed `975bc6d` (checkpoint 8's session-state entry) to `origin/feature/v1.2-development`, then discovered it had never been fast-forwarded into `master` (master was still at `fba90fd`) — fixed by checking out `master`, fetching, and fast-forwarding again to `975bc6d`, then pushing `origin/master`. Deleted the local `feature/v1.2-development` branch (`git branch -d`, safe since fully merged). **Attempted to also delete the remote copy (`git push origin --delete feature/v1.2-development`) — blocked by the auto-mode classifier** as a harder-to-reverse shared-state action; left for the user to decide/do instead of working around it.

Then ran this `/checkpoint`: confirmed the working directory is now on `master` directly (the feature branch no longer exists locally), clean, in sync with `origin/master` (tip `975bc6d`). `remotes/origin/feature/v1.2-development` still listed in `git branch -a` — the remote branch itself has not been deleted.

- **Branch:** `master` (no longer `feature/v1.2-development` — that branch is gone locally), up to date with `origin/master`, tip `975bc6d`.
- **Working tree:** clean, nothing uncommitted.
- **Backend tests:** `python -m pytest tests/ -v` → **164 passed**, 1 pre-existing warning.
- **Frontend tests:** `npm run test -- --run` → **58 passed** (11 files).
- **Frontend build:** `npm run build` → clean.
- **Frontend lint:** `npm run lint` → clean except the same 2 pre-existing warnings (`JobTimeline.tsx`, `JobArtifacts.tsx`, `react(only-export-components)`).

**Files changed:** `SESSION_STATE.md` only (this entry).

**Current task:** None in progress — verification-only pass, working directly on `master` now.

**Remaining problems / blockers:** None blocking.
- **`origin/feature/v1.2-development` still exists** — deleting it was blocked by the auto-mode classifier (a harder-to-reverse shared-state action); the user hasn't yet said whether to delete it, delete it themselves, or leave it. Not urgent since it's fully merged and inert.
- Phase 4 remains the only substantive item left, still explicitly gated on the user revisiting BUG-3's scope decision — no conversation opened this session, by the user's own choice ("just push + clean up").
- Low-priority, unscheduled, unchanged: BUG-5, frontend coverage gaps on untested page shells.

**Exact next task:** None scheduled. If/when the user wants to revisit the remote branch, or open the Phase 4 conversation, either can be picked up independently — no dependency between them.

---

### 2026-07-19 (checkpoint, 8) — State verification only, no code changes

**What was completed:** Ran `/checkpoint`, continuing from the "v1.2 merged to master" session immediately below. No application logic touched.

- **Branch:** `feature/v1.2-development`, up to date with `origin/feature/v1.2-development` (0 ahead/behind), tip `fba90fd`.
- **`master` confirmed in sync:** `origin/master`'s tip is also `fba90fd` — the fast-forward merge landed cleanly, both branches now point at the same commit.
- **Working tree:** clean, nothing uncommitted.
- **Backend tests:** `python -m pytest tests/ -v` → **164 passed**, 1 pre-existing warning — matches.
- **Frontend tests:** `npm run test -- --run` → **58 passed** (11 files) — matches.
- **Frontend build:** `npm run build` → clean.
- **Frontend lint:** `npm run lint` → clean except the same 2 pre-existing warnings (`JobTimeline.tsx`, `JobArtifacts.tsx`, `react(only-export-components)`).

**Files changed:** `SESSION_STATE.md` only (this entry).

**Current task:** None in progress — verification-only pass. v1.2 is fully shipped: merged to `master`, CI-verified via PR #1's real Actions run, all local checks green on the merged state.

**Remaining problems / blockers:** None.
- **Phase 4** is the only named item left in `TODO.md`'s roadmap, and it is explicitly **not started** — gated on the user revisiting BUG-3's localhost-only, single-user scope decision before any design work begins on real ComfyUI `/interrupt` support, mid-run `generate_script` cancellation, a retention policy, or any multi-user/remote-access question.
- Low-priority, unscheduled, unchanged: BUG-5 (cosmetic duplicate `ComfyUIClient` instances), frontend coverage gaps on untested page shells/forms (accepted convention, not a regression).
- Whether to delete the now-merged `feature/v1.2-development` branch was never asked — still exists, left alone.

**Exact next task:** None scheduled. Next substantive step, whenever the user is ready, is deciding whether/how to open Phase 4.

---

### 2026-07-19 (v1.2 merged to master) — PR #1 merged; ci.yml's push trigger left unchanged

**What was completed:** Two decisions surfaced to the user (via `AskUserQuestion`, since both affect shared/default-branch state): (1) merge PR #1 now, since v1.2's roadmap is fully closed and CI-verified — **approved**; (2) widen `ci.yml`'s `push:` trigger to also fire on feature branches — **declined**, master-only triggers plus a PR-for-CI-and-review pattern is the deliberate, kept convention; no workflow change made.

Merged via local git rather than GitHub's merge button, since no `gh` CLI or GitHub token is available in this environment to call the merge API directly: confirmed `master` was a clean ancestor of `feature/v1.2-development` (`git merge-base --is-ancestor master feature/v1.2-development` — true, so a fast-forward), committed this session's pending doc updates on the feature branch first, pushed the feature branch, then fast-forwarded `master` to the feature branch's tip and pushed `master`. GitHub auto-detects this (the PR's exact commits now present in `master`'s history) and marks PR #1 as merged without needing the API.

**Files changed:** `SESSION_STATE.md`, `TODO.md` (this entry + the merge/trigger decisions recorded). No application code touched.

**Remaining problems / blockers:** None blocking.
- v1.2 is now fully on `master` — the entire `feature/v1.2-development` branch's work (14+ commits) has landed on the default branch, CI-verified.
- Phase 4 remains the only substantive item left, still explicitly gated on the user revisiting BUG-3's localhost-only scope decision before any design work begins.
- Whether to delete the now-merged `feature/v1.2-development` branch, or keep it around, wasn't asked — left alone, not assumed.

**Exact next task:** None scheduled. The natural next conversation, whenever the user is ready, is whether to open Phase 4 (real ComfyUI `/interrupt` support, mid-run `generate_script` cancellation, retention policy, any multi-user/remote-access question) — not to be started without that explicit conversation first.

---

### 2026-07-19 (CI-gap resolved) — PR #1 opened, first real CI run on this branch confirmed green

**What was completed:** Resolves the CI-trigger gap first found earlier today and chased across several `/checkpoint`/`/commit` cycles. The user opened **PR #1** ("Feature/v1.2 development", `feature/v1.2-development` → `master`, https://github.com/hokphaymany2025-oss/manytv-ai/pull/1) — confirmed via the GitHub Pulls API (previously `[]` on every prior check this session, now shows one open PR, head sha `ecf6199`, opened `2026-07-19T12:07:19Z`).

**Verified the resulting CI run directly, not just its pass/fail status:** run `29686401582` (`pull_request` event, head `ecf6199`) — `status: completed`, `conclusion: success`. Checked both jobs' step-by-step results via the Actions Jobs API: `frontend` job's `npm run test -- --coverage` step succeeded (along with `npm ci`/`build`/`lint`), `backend` job's `python -m pytest --cov=backend --cov-report=term-missing tests/ -v` step succeeded. **This is the first real GitHub Actions execution this branch has ever had** — every prior "CI confirmed green" claim across the entire v1.2 effort (14+ commits) was a local run only, per the gap found earlier this session. The coverage-reporting steps added a few sessions ago now have live confirmation, not just local verification.

**Files changed:** `TODO.md` (marked the CI-trigger-gap item done, added a new open item for the still-undecided `push:`-trigger question), `SESSION_STATE.md` (this entry). No application code touched.

**Remaining problems / blockers:** None blocking.
- **PR #1 is open but not merged** — whether/when to merge it into `master` is a separate decision, not made this session.
- **Still open:** whether to add `feature/v1.2-development` (or a wildcard) to `ci.yml`'s `push:` trigger, so future direct pushes to this branch get CI feedback without needing a new PR each time. Not yet decided.
- v1.2 Phases 1-3 remain fully closed; Phase 4 still explicitly gated on revisiting BUG-3's scope decision.
- One local commit (`c609842`, the last checkpoint's doc-only entry) may still be ahead of what the PR reflects if pushed after PR #1 was opened — worth reconciling in the next session (check `git status`/PR's current head sha before assuming anything).

**Exact next task:** Decide whether/when to merge PR #1, and separately, whether to fix the underlying `push:`-trigger gap so this doesn't need repeating for future branches.

---

### 2026-07-19 (checkpoint, 7) — State verification only, no code changes

**What was completed:** Ran `/checkpoint`, continuing from the "PR troubleshooting" session immediately below. Re-checked the PRs API one more time before writing this entry — still `[]`, no change. No application logic touched.

- **Branch:** `feature/v1.2-development`, up to date with `origin/feature/v1.2-development` (0 ahead/behind) — `ecf6199` confirmed pushed.
- **Working tree:** only `SESSION_STATE.md` modified (this entry). Nothing else uncommitted.
- **Backend tests:** `python -m pytest tests/ -v` → **164 passed**, 1 pre-existing warning — matches.
- **Frontend tests:** `npm run test -- --run` → **58 passed** (11 files) — matches.
- **Frontend build:** `npm run build` → clean.
- **Frontend lint:** `npm run lint` → clean except the same 2 pre-existing warnings (`JobTimeline.tsx`, `JobArtifacts.tsx`, `react(only-export-components)`).

**Files changed:** `SESSION_STATE.md` only (this entry).

**Current task:** None in progress — verification-only pass.

**Remaining problems / blockers:** None new.
- **The PR to `master` still hasn't been created** — three checks across two sessions, still `[]`. User was given a simplified link and the repo-homepage-banner fallback last turn; outcome not yet reported back.
- Once a PR exists and its CI run is confirmed, still open: the `ci.yml` `push:`-trigger decision for this branch.
- v1.2 Phases 1-3 remain fully closed; Phase 4 still explicitly gated on revisiting BUG-3's scope decision.

**Exact next task:** Hear back on whether the simplified link or the repo-homepage banner worked; if still stuck, troubleshoot further (browser console errors, screenshot, or manually walking through the GitHub UI) rather than retrying the same link again. Once a PR exists, check its CI run.

---

### 2026-07-19 (PR troubleshooting) — Pushed pending commit; PR-creation link still not resulting in a real PR

**What was completed:** Continued from "checkpoint, 6." Pushed the one pending local commit (`ecf6199`, doc-only session-state entry) to `origin/feature/v1.2-development` — no code to implement this round, matching the last several `/implement` passes (nothing left in v1.2's roadmap except operational git/GitHub housekeeping and the explicitly-gated Phase 4).

**The real open problem:** the pre-filled PR link (`compare/master...feature/v1.2-development?quick_pull=1&...`) handed to the user across the last two sessions has been attempted at least twice (per the user's own messages: "opening the pending PR link", "check CI run") but has not resulted in an actual PR — confirmed repeatedly via the unauthenticated GitHub Actions/PRs REST API: `pulls?state=all` returns `[]`, and `actions/runs` still shows only 7 total runs, all on `master`, the latest (`#6`) unchanged since before this branch existed. Checked the repo's own settings this session for anything that could structurally block it: `private: false`, `archived: false`, `disabled: false`, `default_branch: master` — nothing wrong there, so the link itself should work.

**Action taken:** rather than repeat the identical link a third time, gave the user two alternatives — a simplified link without the long pre-filled `title`/`body` query params (`.../compare/master...feature/v1.2-development?quick_pull=1`), and the fallback of visiting the repo's main page directly, where GitHub normally auto-shows a "Compare & pull request" banner for a branch ahead of the default — sidesteps whatever's going wrong with the constructed URL entirely.

**Files changed:** `SESSION_STATE.md` only (this entry). No application code touched, no `TODO.md` change needed (nothing new to record there beyond what's already tracked as the open PR-creation item).

**Remaining problems / blockers:**
- **PR still not created** — this is now the single blocking item preventing this branch from ever having a real CI run. Root cause of why the link isn't resulting in a PR is still unconfirmed (could be the link not loading, the "Create pull request" button not being clicked, or something browser-side) — worth the user trying the simplified link or the repo-homepage-banner approach next, and reporting back what actually happens (error message, blank page, etc.) if it still doesn't work, rather than retrying blindly again.
- Once a PR does exist and its CI run is confirmed, still open: the `push:` trigger decision for `ci.yml`, named but not decided.
- v1.2 Phases 1-3 remain fully closed; Phase 4 still explicitly gated on revisiting BUG-3's scope decision.

**Exact next task:** User tries the simplified link or the repo-homepage banner, reports what happens. Once a PR genuinely exists (verified via the API, not just assumed), check its CI run.

---

### 2026-07-19 (checkpoint, 6) — State verification only, no code changes

**What was completed:** Ran `/checkpoint`, continuing from the pushed `088e3dd` (CI-trigger-gap documentation). Just prior to this checkpoint, re-checked the GitHub API and confirmed: still no PR open against `master`, still only 7 total Actions runs (all on `master`, none newer) — the pre-filled PR link handed to the user two sessions ago has not been opened yet. No application logic touched this session.

- **Branch:** `feature/v1.2-development`, up to date with `origin/feature/v1.2-development` (0 ahead/behind) — `088e3dd` confirmed actually pushed and present on `origin`.
- **Working tree:** clean. Nothing uncommitted.
- **Backend tests:** `python -m pytest tests/ -v` → **164 passed**, 1 pre-existing warning — matches.
- **Frontend tests:** `npm run test -- --run` → **58 passed** (11 files) — matches.
- **Frontend build:** `npm run build` → clean.
- **Frontend lint:** `npm run lint` → clean except the same 2 pre-existing warnings (`JobTimeline.tsx`, `JobArtifacts.tsx`, `react(only-export-components)`).

**Files changed:** `SESSION_STATE.md` only (this entry).

**Current task:** None in progress — this was a verification-only pass.

**Remaining problems / blockers:** None new.
- **The pre-filled PR link (`feature/v1.2-development` → `master`) still has not been opened** — re-confirmed via the Actions/PRs API this session, not assumed. This is a user action; nothing further to chase automatically until it's opened.
- Once a real CI run happens (via that PR), still open: whether to add this branch (or a wildcard) to `ci.yml`'s `push:` trigger so future commits get live feedback without needing a PR first — named, not decided.
- v1.2 Phases 1-3 remain fully closed; Phase 4 still explicitly gated on revisiting BUG-3's scope decision.

**Exact next task:** Get the PR opened (user action), then confirm its CI run is actually green — this branch's first real one.

---

### 2026-07-19 (checkpoint, 5) — State verification only, no code changes

**What was completed:** Ran `/checkpoint`, continuing directly from the "CI-trigger gap found" session immediately below. No application logic touched.

- **Branch:** `feature/v1.2-development`, up to date with `origin/feature/v1.2-development` (0 ahead/behind) — `db8df87` confirmed actually pushed and present on `origin`.
- **Working tree:** `SESSION_STATE.md`, `TODO.md` modified (the CI-trigger-gap finding and its recorded open item from the prior session) — nothing unexpected, matches exactly what that session left uncommitted.
- **Backend tests:** `python -m pytest tests/ -v` → **164 passed**, 1 pre-existing warning — matches.
- **Frontend tests:** `npm run test -- --run` → **58 passed** (11 files) — matches.
- **Frontend build:** `npm run build` → clean.
- **Frontend lint:** `npm run lint` → clean except the same 2 pre-existing warnings (`JobTimeline.tsx`, `JobArtifacts.tsx`, `react(only-export-components)`).

**Files changed:** `SESSION_STATE.md` only (this entry).

**Current task:** None in progress — this was a verification-only pass.

**Remaining problems / blockers:** None new. Same as the prior entry:
- **The pre-filled PR link (`feature/v1.2-development` → `master`) has not been opened yet** — this branch still has never had a real GitHub Actions run against it. Link was already handed to the user in the prior turn; opening it is a user action, not something to chase automatically.
- This session's doc-only changes (`SESSION_STATE.md`, `TODO.md`) are ready but **not yet committed** — waiting on `/commit`.
- v1.2 Phases 1-3 remain fully closed; Phase 4 still explicitly gated on revisiting BUG-3's scope decision.

**Exact next task:** Get the PR opened (user action) and confirm its CI run is green — the first real one this branch has ever had. Separately, commit this session's + the prior session's doc updates via `/commit`.

---

### 2026-07-19 (CI-trigger gap found) — Pushed pending commit, discovered CI never actually runs on this branch

**What was completed:** Continued from the prior "Phase 3, fully closed" session. Per `/next_tasks`'/`/implement`'s recommendation (confirmed with the user first via `AskUserQuestion`, since pushing is a shared-state action), pushed the one pending local commit (`db8df87`, "Wire coverage reporting into CI") to `origin/feature/v1.2-development`.

**Real, previously-unnoticed finding while trying to "confirm CI passes" for the push:** `origin/feature/v1.2-development` had **zero** GitHub Actions runs against it, ever — not because CI failed, but because `.github/workflows/ci.yml`'s triggers are `push: branches: [master]` and `pull_request: branches: [master]` only. Confirmed via the unauthenticated Actions REST API (public repo, same read path used by the original CI-fix session): `total_count: 0` when filtered to this branch; the unfiltered list shows only 7 runs total, all on `master`, the latest (#6) for commit `b4dd5ac` — from well before `feature/v1.2-development` was created. **Every "CI confirmed green" claim across this entire v1.2 development arc (14+ commits, multiple sessions) was actually a local run of the exact CI command, never a live GitHub Actions execution** — the workflow structurally could not have fired on any of those pushes.

**Action taken:** confirmed with the user (via `AskUserQuestion`) how to get a real run — chose to open a PR from `feature/v1.2-development` to `master`, which the existing `pull_request` trigger already listens for (no workflow changes needed). No `gh` CLI or GitHub token is available in this environment, so the PR itself could not be created programmatically — instead, drafted the title/summary/test-plan and built a pre-filled GitHub compare-with-quick_pull URL for the user to open manually:
`https://github.com/hokphaymany2025-oss/manytv-ai/compare/master...feature/v1.2-development?quick_pull=1&title=...&body=...` (full URL given to the user directly in-conversation).

**Files changed:** `SESSION_STATE.md`, `TODO.md` only (this entry + a new open item below). No application code touched — nothing to re-run through the test suite (unaffected, last confirmed passing this session: 164 backend / 58 frontend / clean build / clean lint).

**Remaining problems / blockers:**
- **The PR has not actually been opened yet** — the link was handed to the user, not submitted on their behalf (no write credentials available). Until it's opened, this branch's actual first real CI run still hasn't happened.
- Once the PR is opened and CI runs, worth deciding whether to also fix the underlying gap for future work on this branch (e.g. adding `feature/v1.2-development` — or a wildcard — to `ci.yml`'s `push:` trigger) so future commits get real CI feedback without needing a PR first. Named here, not yet decided or actioned.
- v1.2's Phases 1-3 remain fully closed otherwise; Phase 4 still explicitly gated on revisiting BUG-3's scope decision.

**Exact next task:** User opens the pre-filled PR link, then confirms (or has a session confirm) that the resulting GitHub Actions run is actually green — the first real one this branch has ever had.

---

### 2026-07-19 (checkpoint, 4) — State verification only, no code changes

**What was completed:** Ran `/checkpoint`, continuing directly from the "Phase 3, fully closed" session immediately below (coverage reporting wired into CI). No application logic touched.

- **Branch:** `feature/v1.2-development`, up to date with `origin/feature/v1.2-development` (0 ahead/behind).
- **Working tree:** matches exactly what the prior session left uncommitted — `.github/workflows/ci.yml` (the coverage-in-CI wiring), `TODO.md`, `SESSION_STATE.md`. Nothing unexpected found; still waiting on the same approval named in the prior entry.
- **Backend tests:** `python -m pytest tests/ -v` → **164 passed**, 1 pre-existing warning — matches.
- **Frontend tests:** `npm run test -- --run` → **58 passed** (11 files) — matches.
- **Frontend build:** `npm run build` → clean.
- **Frontend lint:** `npm run lint` → clean except the same 2 pre-existing warnings (`JobTimeline.tsx`, `JobArtifacts.tsx`, `react(only-export-components)`).

**Files changed:** `SESSION_STATE.md` only (this entry).

**Current task:** None in progress — this was a verification-only pass.

**Remaining problems / blockers:** None new. Same as the prior entry:
- The coverage-in-CI change (`.github/workflows/ci.yml`) plus its `TODO.md`/`SESSION_STATE.md` bookkeeping are ready but **not yet committed** — waiting on explicit approval via `/commit`.
- **v1.2 is otherwise fully closed** once this lands — nothing scheduled remains except Phase 4, explicitly gated on the user revisiting BUG-3's localhost-only scope decision before any design work.

**Exact next task:** Run `/commit` to commit `.github/workflows/ci.yml`, `TODO.md`, and `SESSION_STATE.md` on `feature/v1.2-development`, then push and confirm the real GitHub Actions run shows both coverage reports in its logs.

---

### 2026-07-19 (Phase 3, fully closed) — Coverage reporting wired into CI

**What was completed:** Picked up via `/next_tasks` as the one remaining concretely-scoped item that didn't need a user scope decision first (unlike Phase 4). Confirmed starting state first: branch fully synced with `origin` (last commit `a959632`, which landed out-of-band between sessions — tracks `.claude/commands/*.md`, the project's own slash-command definitions, previously untracked local tooling).

- `.github/workflows/ci.yml`: backend job's `python -m pytest tests/ -v` → `python -m pytest --cov=backend --cov-report=term-missing tests/ -v`; frontend job's `npm run test` → `npm run test -- --coverage`. Both tools (`pytest-cov`, `@vitest/coverage-v8`) were already installed from the two prior coverage-measurement sessions — this only changes what CI actually invokes.
- **Deliberately report-only** — no `--cov-fail-under` on either side, matching this project's established practice of surfacing a number before ever deciding to gate on it.
- Verified locally with the *exact* CI commands before changing anything, not just the pre-existing plain invocations: backend → **164 passed, 91% overall** (893 statements, 78 missed); frontend → **58 passed, 66.94% overall** (242 statements, 162 covered) — both coverage reports rendered correctly, confirming no drift between local and CI invocation. `npm run build` clean, `npm run lint` clean (same 2 pre-existing warnings, `JobTimeline.tsx`/`JobArtifacts.tsx`).

**Files changed:** `.github/workflows/ci.yml` only (2 one-line changes). `TODO.md`, `SESSION_STATE.md` for bookkeeping. No application or test code touched.

**Remaining problems / blockers:** None blocking. **v1.2 is now fully closed** — every Phase 1-3 item, including both previously-optional/deferred ones (README refresh, coverage-in-CI), is done. Only Phase 4 remains, and it is explicitly **not** to be started without a scope conversation with the user first (per `TODO.md`'s own gating note) — it would touch real ComfyUI `/interrupt` support, mid-run `generate_script` cancellation, a retention policy, and/or any multi-user/remote-access question, all of which conflict with the currently-confirmed localhost-only, single-user deployment decision (BUG-3) unless that's explicitly revisited.
- **This session's change is not yet committed** — waiting for approval, per this project's standing convention (`/implement` doesn't commit).

**Exact next task:** Get approval to commit this session's `.github/workflows/ci.yml` change (plus `TODO.md`/`SESSION_STATE.md` bookkeeping) on `feature/v1.2-development`, then push and confirm the real GitHub Actions run shows both coverage reports in its logs. After that, v1.2 has nothing left except the explicitly-gated Phase 4 — the natural next conversation is whether/when to revisit that scope decision.

---

### 2026-07-19 (checkpoint, 3) — State verification only, no code changes

**What was completed:** Ran `/analyze`, then `/next_tasks`, then `/implement`, then `/checkpoint` in sequence, purely as read-only/verification passes — no application logic touched at any point.

- **Branch:** `feature/v1.2-development`, 2 commits ahead of `origin/feature/v1.2-development` (`f37112f` backend coverage, `de05ca4` frontend coverage tooling) — still unpushed.
- **Working tree:** `README.md` modified (the architecture-tree refresh named as an open optional item in every recent entry — reviewed via `git diff` this session and confirmed **complete and accurate**: architecture tree, endpoint table, CORS section, and Frontend section all now match the current file layout and test counts), plus the untracked `.claude/` directory (local tooling config, not part of this work, left alone as in every prior checkpoint). Nothing unexpected found.
- **`/implement` was invoked** for the next recommended task (commit + push the README refresh), but since that task is pure git housekeeping with no code to implement, and `/implement`'s convention is to leave commits to `/commit`, this was flagged to the user directly via `AskUserQuestion` rather than silently proceeding. User chose to stop and defer to `/commit`. **Nothing was committed or pushed this session.**
- **Backend tests:** `python -m pytest tests/ -v` → **164 passed**, 1 pre-existing warning (`StarletteDeprecationWarning` re: `httpx`/`starlette.testclient`) — matches prior sessions exactly.
- **Frontend tests:** `npm run test -- --run` → **58 passed** (11 files) — matches.
- **Frontend build:** `npm run build` → clean.
- **Frontend lint:** `npm run lint` → clean except the same 2 pre-existing warnings (`JobTimeline.tsx`, `JobArtifacts.tsx`, `react(only-export-components)`), unrelated to any uncommitted change.

**Files changed:** `SESSION_STATE.md` only (this entry).

**Remaining problems / blockers:** None new. Same as every recent entry:
- The README refresh and the two backend/frontend coverage commits are all ready but **not yet committed/pushed** — waiting on explicit user approval via `/commit`, not automatic.
- Neither coverage report is wired into CI (named optional, not blocking).
- Phase 4 not started, gated on revisiting BUG-3's localhost-only scope decision.

**Exact next task:** Run `/commit` to commit the README refresh (and confirm the two pending coverage commits) on `feature/v1.2-development`, then push all three to `origin` and confirm CI passes.

---

### 2026-07-19 (checkpoint, 2) — State verification only, no code changes

**What was completed:** Ran `/checkpoint` again, continuing directly from the "Phase 3, closing" session immediately below (frontend coverage measurement). No application logic touched.

- **Branch:** `feature/v1.2-development`, 1 commit ahead of `origin/feature/v1.2-development` (`f37112f`, still unpushed).
- **Working tree:** matches exactly what the prior session left uncommitted — `.gitignore`, `SESSION_STATE.md`, `TODO.md`, `frontend/package-lock.json`, `frontend/package.json`, `frontend/vite.config.ts` (the `@vitest/coverage-v8` addition), plus the untracked `.claude/` directory (local tooling config, not part of this work, left alone as in every prior checkpoint). Nothing unexpected found.
- **Backend tests:** `python -m pytest tests/ -v` → **164 passed** — matches the prior session's reported count exactly.
- **Frontend tests:** `npm run test -- --run` → **58 passed** (11 files) — matches.
- **Frontend build:** `npm run build` → clean.
- **Frontend lint:** `npm run lint` → clean except the same 2 pre-existing warnings (`JobTimeline.tsx`, `JobArtifacts.tsx`), unrelated to any uncommitted change.

**Files changed:** `SESSION_STATE.md` only (this entry).

**Remaining problems / blockers:** None new. Same as the prior entry:
- This session's (and the prior session's) changes are not yet committed — waiting on approval to commit the frontend coverage-tooling addition.
- `f37112f` (backend coverage work) remains unpushed to `origin`.

**Exact next task:** Same as before — get approval to commit the pending frontend coverage-tooling diff (`.gitignore`, `frontend/package.json`, `frontend/package-lock.json`, `frontend/vite.config.ts`, `TODO.md`, `SESSION_STATE.md`) on `feature/v1.2-development`, then push both commits and confirm CI passes.

---

### 2026-07-19 (Phase 3, closing) — Frontend coverage measurement added, v1.2 Phase 3 fully closed

**What was completed:** Continued from the checkpoint/commit sessions below. Task, identified via `/next_tasks` and approved by name via `/implement`: add frontend test coverage measurement (`vitest --coverage`), the last explicitly-named open item in v1.2's Phase 3, mirroring the backend `pytest-cov` session's own scope and precedent exactly (tooling only, report first, don't chase the number in the same pass).

- Added `@vitest/coverage-v8` as a devDependency (`^4.1.10`, version-matched to the already-installed `vitest`). `frontend/vite.config.ts`'s existing `test` block gained a `coverage` section (`provider: 'v8'`, `reporter: ['text', 'html']`, `include: ['src/**/*.{ts,tsx}']`, excluding test files/`main.tsx`/`vite-env.d.ts`). `.gitignore` gained `frontend/coverage/`. **Deliberately not wired into `package.json`'s default `"test"` script** — same reasoning as the backend: `npm run test`/CI's frontend job stay exactly as fast as before; coverage is opt-in via `npm run test -- --coverage`.
- **Result: 66.94% overall** (242 statements, 162 covered), 58 tests still passing. A real debugging moment worth recording: the terminal `text` reporter initially appeared to be missing several files entirely (`formatTime.ts`, `formatBytes.ts`, `useJobAttempts.ts`, `useJobLogs.ts`, `JobAttemptHistory.tsx`, `JobExecutionLogs.tsx`) — cross-checked against the generated HTML report (`grep`-ing each file's `.html` page directly, no Python available in this environment) rather than assuming a config bug, and confirmed all six are genuinely **100% covered**; istanbul's text reporter just hides fully-covered files from its table by default. The real, now-quantified picture: `useJob.ts`/`useJobList.ts` 96%+, `api.ts` 77%, `JobArtifacts.tsx` 90%, `JobTimeline.tsx` 74% (only its pure `buildTimelineEvents` helper is tested, not the render path) — and **zero test coverage at all** on `App.tsx`, `JobList.tsx`, `ScriptForm.tsx`, `StoryboardForm.tsx`, `StatusFilter.tsx`, `DashboardPage.tsx`, `JobDetailPage.tsx`, plus `JobRow.tsx` at only 30% (its one test file covers just the `project_id` badge). Consistent with this project's long-standing, deliberate testing convention (pure logic + a few data-bearing components tested directly, page shells/forms never rendered in a test) rather than a newly-introduced gap — this is simply the first time it's been measured rather than just generally known, exactly paralleling the backend coverage session's own framing.
- **Deliberately out of scope**, named rather than chased: raising any of the above numbers. The approved task was "add coverage measurement," not "raise coverage to X%."

**Verification:** `python -m pytest tests/ -v` → **164 passed** (backend, unaffected, re-confirmed). `npm run test` (plain) → **58 passed**. `npm run build` → clean. `npm run lint` → clean except the same 2 pre-existing warnings (`JobTimeline.tsx`, `JobArtifacts.tsx`) already named in every recent entry.

**Files changed:** `frontend/package.json`/`package-lock.json` (new devDependency), `frontend/vite.config.ts`, `.gitignore`, `TODO.md`, `SESSION_STATE.md`. No application source touched, confirmed by review before finishing.

**Remaining problems / blockers:** None blocking. **v1.2 Phase 3 is now fully closed** — both backend and frontend coverage measurement done.
- **This session's changes are not yet committed** — waiting for approval, per this project's standing convention.
- The two remaining named-but-optional items are unchanged from before: refreshing README's stale architecture tree, and wiring either coverage report into CI (neither was in scope for this pass).
- The prior session's backend coverage commit (`f37112f`) is still unpushed to `origin` — noted, not blocking.
- Phase 4 not started, gated on revisiting BUG-3's localhost-only scope decision.

**Exact next task:** Get approval to commit this session's frontend coverage-tooling changes (`frontend/package.json`, `frontend/package-lock.json`, `frontend/vite.config.ts`, `.gitignore`, `TODO.md`, `SESSION_STATE.md`) on `feature/v1.2-development`. Once approved: push `f37112f` (and this new commit) to `origin` and confirm CI passes — the last item with any real remaining weight before v1.2 can be considered fully wrapped up, aside from the two optional/deferred items and the explicitly-gated Phase 4.

---

### 2026-07-19 (checkpoint) — State verification only, no code changes

**What was completed:** Ran `/checkpoint` to verify and record current session state. No application logic touched, per the checkpoint task's explicit constraint.

- **Branch:** `feature/v1.2-development`, up to date with `origin/feature/v1.2-development` (0 ahead/behind).
- **Working tree:** matches exactly what the prior session ("Phase 3, continued") left uncommitted — `SESSION_STATE.md`, `TODO.md`, and five modified test files (`tests/test_config.py`, `tests/test_job_routes.py`, `tests/test_recovery.py`, `tests/test_storyboard_helpers.py`, `tests/test_worker.py`), plus two new untracked test files (`tests/test_generate_script_route.py`, `tests/test_llm_client.py`) and an untracked `.claude/` directory. Nothing unexpected found; still uncommitted, still waiting on the same approval named in the prior entry.
- **Backend tests:** `python -m pytest --cov=backend --cov-report=term-missing tests/ -v` → **164 passed**, **91% overall coverage** (893 statements, 78 missed) — confirms the prior session's reported numbers still hold exactly.
- **Frontend tests:** `npm run test -- --run` → **58 passed** (11 files) — higher than the last-recorded 55, consistent with `JobAttemptHistory` UI tests (`52ccbab`) landing since that count was last written down.
- **Frontend build:** `npm run build` → clean.
- **Frontend lint:** `npm run lint` → clean except the same 2 pre-existing warnings already named in earlier entries (`JobTimeline.tsx`, `JobArtifacts.tsx`, `react(only-export-components)`), unrelated to any uncommitted change.

**Files changed:** `SESSION_STATE.md` only (this entry).

**Remaining problems / blockers:** None new. Same as the prior entry:
- Nothing from this branch's recent work has been committed since `f1c7802` — waiting on approval to commit the coverage-test additions.
- Frontend coverage measurement (`vitest --coverage`) still not started.

**Exact next task:** Same as before — get approval to commit the pending test-only diff (`tests/test_llm_client.py`, `tests/test_generate_script_route.py`, the five modified test files, `TODO.md`, `SESSION_STATE.md`) on `feature/v1.2-development`.

---

### 2026-07-19 (Phase 3, continued) — Backend coverage raised from 85% to 91.27%

**What was completed:** Continued directly from the coverage-tooling session below. Task: "improve backend coverage from 85% toward 90%," explicitly tests-only, no application behavior changes, prioritizing pure logic over anything needing a live Ollama/ComfyUI/GPU.

Read the actual coverage report's `Missing` line ranges file-by-file rather than guessing what to add — this surfaced two real, notable gaps that had nothing to do with live services at all: **`backend/core/llm_client.py` and `backend/api/routes/generate_script.py` had zero direct test coverage of any kind** (no `tests/test_llm_client.py` existed; `create_storyboard`, the actual `POST /api/storyboard` route, had never been called by any test either, despite `retry`/`cancel`/`get_job_status`/etc. all being thoroughly tested). These two files alone accounted for 19 of the 130 original missed lines and were both fully mockable (the OpenAI/Ollama call and the worker queue are exactly the kind of boundary this test suite already mocks elsewhere) — closing them was the highest-value, lowest-risk work in this session.

- New `tests/test_llm_client.py` (6 cases) — `LLMClient.generate_script` success, `<think>`-block stripping (the documented Qwen3/Ollama quirk), empty-result-after-stripping, `APIConnectionError`/`APIStatusError` → `LLMError`, and the exact user-message construction. Mocks at the `_client.chat.completions.create` boundary via a hand-rolled fake completion object, matching `test_comfyui_client.py`'s existing "monkeypatch one bound method on a real instance" convention rather than reaching for a mocking library. `llm_client.py`: 52% → 100%.
- New `tests/test_generate_script_route.py` (3 cases) — the job handler and the route itself, previously exercised by nothing. `generate_script.py`: 68% → 100%.
- `tests/test_job_routes.py` (+11): `create_storyboard` (script-splitting, explicit shots, empty-script rejection, too-many-shots rejection); the three streaming routes' success-path return value (`job_events`/`job_logs_events`/`all_jobs_events` were each only ever tested for their 404 branch, never confirmed to actually return a `StreamingResponse` for a real job); `get_job_status`'s own missing-job 404 (every other test always pre-creates the job). `storyboard.py` routes: 90% → 100%. `job_responses.py`: 95% → 100%.
- `tests/test_config.py` (+2), `tests/test_storyboard_helpers.py` (+5: `_load_workflow` real read + not-found error, `_save_history_outputs`'s two skip branches + happy path), `tests/test_recovery.py` (+1: `_run_storyboard_job` raising when ComfyUI's health check fails up front), `tests/test_worker.py` (+3: `start()`/`stop()`/`resubmit()`, never called directly by any existing test — everything else drives the queue via a `_run_one_iteration` helper or monkeypatches `resubmit` out entirely).
- **Deliberately left alone** (named, not chased for the last few percent): `comfyui_client.py` (53%, real WS/HTTP internals beyond what's already mocked), `gpu_memory.py` (56%, genuinely GPU/torch-optional), `app.py` (67%, real process lifespan), `sse.py`'s keepalive-timeout branches (84%), `storyboard_engine.py`'s progress-callback/timeout-drain branches (91%) — all doable with more elaborate fakes but not needed to clear the 90% target. `events.py`'s 2-line `except asyncio.QueueEmpty` branch is a genuinely different case: confirmed **unreachable in practice** (nothing can run between the `.full()` check and the immediately-following `.get_nowait()` in single-threaded asyncio), so left alone as dead defensive code rather than forced with a contrived test.

**Result:** `python -m pytest --cov=backend --cov-report=term-missing tests/ -v` → **164 passed** (136 prior + 28 new). **91.27% overall** (893 statements, 78 missed — down from 130). `python -m pytest tests/ -v` (plain, matching `.github/workflows/ci.yml`'s exact invocation) also confirmed green. Full per-file breakdown in `TODO.md`'s new Completed entry.

**Files changed:** `tests/test_llm_client.py` (new), `tests/test_generate_script_route.py` (new), `tests/test_job_routes.py`, `tests/test_config.py`, `tests/test_storyboard_helpers.py`, `tests/test_recovery.py`, `tests/test_worker.py`, `TODO.md`, `SESSION_STATE.md`. No `backend/` source files touched — confirmed by `git status` before finishing, matching the task's explicit constraint.

**Remaining problems / blockers:** None blocking.
- Frontend coverage measurement (`vitest --coverage`) still not started.
- The named-and-deliberately-skipped files above remain at their current coverage levels — informational, not a new to-do list; revisit only if a specific bug in one of them makes it worthwhile.
- Nothing committed yet — waiting for review, per this session's explicit instruction.

**Exact next task:** Review this session's test-only diff, then get approval to commit (`tests/test_llm_client.py`, `tests/test_generate_script_route.py`, the five modified test files, `TODO.md`, `SESSION_STATE.md`) on `feature/v1.2-development`. The prior session's coverage-tooling changes are already committed (`f1c7802` — confirmed via `git log`, not assumed).

---

### 2026-07-19 (Phase 3) — Backend test coverage measurement added

**What was completed:** First Phase 3 item picked up after the `storyboard.py` refactor: "add backend test coverage measurement," explicitly scoped to analysis + tooling only, no application code changes. Checked the current setup first — `pytest.ini` only sets `pythonpath = .`; `requirements.txt` had `pytest>=8.0` and nothing else in its Dev/test section; no `.coveragerc`/`pyproject.toml` existed. `pytest-cov` wasn't installed.

- Installed `pytest-cov` (pulled in `coverage==7.15.2`); added `pytest-cov>=5.0` to `requirements.txt`'s Dev/test section with an inline comment naming the exact command to run it.
- New `.coveragerc` — `source = backend` (tests aren't measured, matching the intent of "how much of the application is exercised," not the test suite itself), `show_missing = True`.
- `.gitignore` gained `.coverage`/`htmlcov/` — generated, ephemeral artifacts, same treatment as `.pytest_cache/`.
- **Deliberately did not add `--cov` to `pytest.ini`'s `addopts`** — a plain `pytest`/CI run (`.github/workflows/ci.yml` still just runs `pytest tests/ -v`) stays exactly as fast and quiet as before; coverage is opt-in via `pytest --cov=backend --cov-report=term-missing tests/`, chosen over silently changing every future test run's default output when the task's scope was "generate a coverage report," not "wire coverage into CI" (that's named as a separate, still-open step).
- Regenerated `requirements.lock.txt` (`pip freeze` via bash, not PowerShell) to include the new dependency. Side effect, not the point of this session: this fixed a real, previously-unnoticed problem — the lockfile from Phase 1 was UTF-16-encoded (confirmed via `file requirements.lock.txt` and a raw byte read showing a `\xff\xfe` BOM), almost certainly from being generated via PowerShell's `>` redirection rather than `Out-File -Encoding utf8`. Verified this was harmless in practice (`pip install --dry-run -r requirements.lock.txt` parsed it correctly regardless), but UTF-8 is the conventional encoding for this kind of file and it was a one-line fix while already touching it — not chased down further or treated as a bug needing separate investigation.

**Result:** `python -m pytest --cov=backend --cov-report=term-missing tests/ -v` → **136 passed**, **85% overall coverage** (893 statements, 130 missed). Full breakdown in `TODO.md`'s new Completed entry. Highest: `job_store.py`/`schemas.py`/`artifacts.py` all 100%. Lowest: `llm_client.py` 52%, `comfyui_client.py` 53%, `gpu_memory.py` 56%, `app.py` 67%, `generate_script.py` 68% — every one of these needs a live Ollama/ComfyUI instance or real process lifecycle to exercise their uncovered branches, consistent with this project's long-standing pattern of live-validating exactly these paths rather than mocking them further (see nearly every prior Session Log entry's "Live validation" section). Not a new discovery — the first time it's been quantified rather than just generally known. Also generated an HTML report (`htmlcov/`, gitignored) via `--cov-report=html` as a browsable artifact, not committed.

**Files changed:** `requirements.txt`, `requirements.lock.txt` (regenerated), `.coveragerc` (new), `.gitignore`, `TODO.md`, `SESSION_STATE.md`. No `backend/` source files touched — confirmed by `git status` before finishing.

**Also caught up `TODO.md`'s bookkeeping**, which had fallen behind: the retry/cancel attempt history frontend work (`JobAttemptHistory.tsx` + its diff review) from the two preceding turns had never been logged, since those turns' explicit instructions were scoped to implementation/review only, not doc updates. Added a Completed entry for it now, alongside this session's coverage work, so both `TODO.md` and this file are accurate as of right now rather than one release behind.

**Remaining problems / blockers:** None blocking.
- Frontend coverage measurement (`vitest --coverage`) not started — the natural next small item, pairing with this session's backend work.
- Neither coverage report is wired into CI yet — named as a separate, deliberately-deferred step, not forgotten.
- Phase 4 not started, gated on revisiting BUG-3's localhost-only scope decision.
- **This session's coverage-tooling changes are not yet committed** — waiting for approval. (The retry/cancel attempt history frontend work from the two preceding turns is already committed — `b45324c`/`52ccbab` on `feature/v1.2-development`, landed out-of-band between turns, confirmed via `git log` rather than assumed.)

**Exact next task:** Get approval to commit this session's coverage-tooling changes (`requirements.txt`, `requirements.lock.txt`, `.coveragerc`, `.gitignore`, `TODO.md`, `SESSION_STATE.md`) on `feature/v1.2-development`. Once approved and ready to continue: frontend coverage measurement (`vitest --coverage`) is the smallest next item.

---

### 2026-07-19 (Phase 2) — Retry/cancel attempt history added, backend only

**What was completed:** The user asked for design-first work on `TODO.md`'s last substantial Phase 2 gap: "a retried job's timeline reflects only the current attempt." Explicit instructions: analyze the lifecycle, identify where attempts should be stored, propose the minimal schema, show affected files, explain tradeoffs — **no implementation until approved**. Used `EnterPlanMode`; traced every write path in `backend/core/job_store.py` before proposing anything, then dispatched a Plan agent to independently verify the analysis against the real source before presenting a design.

**Key finding that narrowed the whole design**: `_update_job_status_sync`'s `COALESCE` semantics mean it can only ever add information, never erase it — of every write method in `job_store.py`, only `_reset_job_for_retry_sync` and `_reset_shots_by_status_sync` ever null out a previously-set column, and both are called from exactly one place, `retry_job` in `storyboard.py`. Cancellation itself destroys nothing (a cancelled job's data stays intact unless *later retried* through that same path). So this was never a "capture every state transition" problem — just "archive the row `retry_job` is about to overwrite, at the moment it overwrites it." This directly narrowed `TODO.md`'s own prior framing ("a genuinely new append-only events table touching every existing state-transition call site"), confirmed correct by the Plan-agent review rather than assumed. The same review also confirmed skipping a shot-level snapshot table was safe (a `FAILED` shot's `files` is always null already; `prompt_id` is recoverable from existing `job_logs` text today, though named explicitly as a fragile/coincidental property rather than a structural guarantee) and confirmed REST-only (no new SSE stream) was the right call, since attempts change at most once per retry and the existing `job_updated` event already signals when to re-fetch. Plan approved via `ExitPlanMode` before any code was written. Design doc: `C:\Users\hokph\.claude\plans\agile-whistling-firefly.md`.

**Implementation** (backend only, per the approved plan — frontend explicitly deferred):
- `backend/core/job_store.py`: new `job_attempts` table (`id, job_id, status, error, started_at, finished_at, recorded_at`, no FK, matching `shots`/`job_logs`'s existing style). `_reset_job_for_retry_sync` now does `SELECT` current row → `INSERT` into `job_attempts` → the existing reset `UPDATE`, one `commit()` — a genuine first for this file (every other `_*_sync` method was one statement) but not a risk, since `_run()`'s lock already wraps the whole callable. **Public `reset_job_for_retry()` signature unchanged** — `retry_job`'s existing call site in `storyboard.py` needed zero edits, the archival is purely an internal detail of a method that already ran at exactly the right moment. New `get_job_attempts(job_id)` mirrors `get_job_logs` exactly.
- `backend/models/schemas.py`: new `JobAttemptEntry` (`status`, `error`, `started_at`, `finished_at`, `recorded_at`), mirrors `JobLogEntry`.
- `backend/api/routes/storyboard.py`: new `GET /api/jobs/{job_id}/attempts` route, mirrors `GET /api/jobs/{job_id}/logs` (404 convention, ordering).
- Tests: 5 new in `tests/test_job_store.py` (archives a failed attempt; archives a cancelled attempt too; multiple retries accumulate in order; empty for a never-retried job; scoped per job id) and 3 new in `tests/test_job_routes.py` (404 on missing job; empty before any retry; correct shape after a real retry through the actual route). No existing test needed changes.

**A real anomaly during this session, worth recording plainly**: while writing the new `tests/test_job_routes.py` case, the test file was found to contain extra assertion lines I had not written (`entries[0].shot_index`/`entries[1]...` referencing a field `JobAttemptEntry` doesn't even have) immediately after two separate edits — removed both times, and the file stayed clean on the third check. This looked like content from an unrelated test (`test_get_job_logs_returns_entries_in_order_with_correct_shape`) bleeding in from something outside this turn, not a mistake in the edit I made (verified by reading the file immediately after each of my own edits, before it later showed the extra lines). Not fully explained — flagged here rather than silently ignored, in case it recurs. Final file content is correct and every assertion is intentional; verified by re-reading the file after the last fix.

**Verification:** `python -m pytest tests/ -v` → **136 passed** (128 prior + 8 new, no existing test modified). `python -c "from backend.app import app"` sanity-checked before running the suite.

**Files changed:** `backend/core/job_store.py`, `backend/models/schemas.py`, `backend/api/routes/storyboard.py`, `tests/test_job_store.py`, `tests/test_job_routes.py`, `TODO.md`, `SESSION_STATE.md`. No frontend files — deliberately deferred.

**Remaining problems / blockers:** None blocking.
- **Frontend display not built** — a `JobAttemptHistory` component on `JobDetailPage.tsx` (following `JobExecutionLogs`/`JobTimeline`'s existing conventions: a `useJobAttempts` hook, `getJobAttempts` in `api.ts`, `JobAttemptEntry` in `types.ts`) is the natural next step, not started, deliberately out of scope for this backend-first pass.
- Phase 3's coverage measurement (`pytest-cov`/`vitest --coverage`) not started.
- Phase 4 not started, gated on revisiting BUG-3's localhost-only scope decision.
- The unexplained stray-lines incident above — not reproduced a third time, but worth a second look if it recurs in a future session.
- Nothing committed yet — waiting for approval.

**Exact next task:** Get approval to commit this session's backend changes on `feature/v1.2-development`. Once approved: build the frontend display for `GET /api/jobs/{job_id}/attempts` (natural continuation), or pick up coverage measurement (Phase 3's remaining item) instead — independent, either can go first.

---

### 2026-07-18 (Phase 3) — `storyboard.py` split into four modules by responsibility

**What was completed:** The user picked "storyboard.py refactor first" (over coverage measurement) as the next v1.2 item. `backend/api/routes/storyboard.py` (653 lines, the largest file in the codebase) did four unrelated things at once: route handlers, shot-splitting/workflow-templating business logic, SSE streaming, and artifact metadata. This was explicitly the highest-risk change this project has made so far — the most load-bearing file, referenced by 7 of the 9 backend test files, some via direct attribute access to private functions and module globals for monkeypatching.

Used `EnterPlanMode` given the scope. Read the full current file plus every test file that references it before designing anything. Dispatched a Plan agent to independently verify the proposed module layout and, critically, the test-compatibility analysis against the real source (not a description of it) before writing any code. The agent's review confirmed the layout was sound but caught one real gap: I'd assumed only `tests/test_recovery.py`'s `comfyui_client` attribute patches needed updating (~17 lines), reasoning that monkeypatching an attribute on a shared singleton object survives regardless of which module name you access it through. That reasoning is correct for the `comfyui_client.health_check`/`.queue_prompt`/etc. patches — but three other patches in the same file (`monkeypatch.setattr(storyboard_module, "get_settings"/"get_job_store"/"_load_workflow", ...)`) are **module-level name rebindings**, not object mutations — Python resolves those bare names from whichever module a function is *defined* in, not wherever it's imported into. Since `_run_storyboard_job` moved to a new module, those three patches would have silently stopped taking effect, leaking the shot-reconciliation tests into the real DB/filesystem instead of the test's tmp_path-scoped doubles. Caught before any code was written, not after a test failure.

Mid-turn, an unrelated generic "continue this Python project" message arrived (referencing a nonexistent `ARCHITECTURE.md` and "don't change architecture unless required") that conflicted with the refactor already in progress — flagged the conflict directly and confirmed via `AskUserQuestion` to continue the refactor rather than silently picking one interpretation.

**Final module layout** (plan file: `C:\Users\hokph\.claude\plans\agile-whistling-firefly.md`):
- **`backend/core/storyboard_engine.py`** (new, 261 lines) — `_naive_shot_split`, `_load_workflow`, `_apply_shot_to_workflow`, `_save_history_outputs`, `_run_storyboard_job`, the `comfyui_client` singleton, and the `worker.register_handler("storyboard", ...)` call. No FastAPI import, matching every other `backend/core/*.py` module.
- **`backend/core/artifacts.py`** (new, 30 lines) — `_artifact_from_path` only.
- **`backend/api/job_responses.py`** (new, 87 lines) — `_build_job_status_response`. This module wasn't in the original "four responsibilities" framing — it had to be extracted to break a circular import: `storyboard.py` needs `_job_events_stream` from the new `sse.py`, and `_job_events_stream` needs `_build_job_status_response`; keeping the latter in the routes file would make `storyboard.py` and `sse.py` import each other. Verified only `_job_events_stream` (not the other two SSE generators) actually depends on it.
- **`backend/api/sse.py`** (new, 118 lines) — `_SSE_KEEPALIVE_SECONDS`, `_sse_frame`, `_all_jobs_events_stream`, `_job_events_stream`, `_job_logs_events_stream`.
- **`backend/api/routes/storyboard.py`** (trimmed 653 → 231 lines) — only `router = APIRouter(...)` and the ten `@router.*`-decorated endpoints, importing what each needs from the four modules above. No longer imports `ComfyUIClient`/`ComfyUIError`/`Settings`/`JobCancelled` — nothing left in the routes file uses them.

**Test updates** (import paths only — no assertions or test behavior changed):
- `tests/test_storyboard_helpers.py`: import moved to `backend.core.storyboard_engine`.
- `tests/test_job_routes.py`: 3 `_artifact_from_path` call sites now import it from `backend.core.artifacts` directly — the only symbol in this file with no remaining caller inside the routes module (everything else — `retry_job`, `cancel_job`, `get_job_status`, `get_job_logs`, `list_jobs_route`, `download_shot_file`, `job_events`, `job_logs_events`, `worker`, the three SSE stream generators — needed zero changes, since `storyboard.py` genuinely still imports each of them to wire up its own routes, not a compatibility shim).
- `tests/test_recovery.py`: the shot-level reconciliation section (~33 `storyboard_module` references, roughly lines 229-493) repointed to a new `storyboard_engine_module` import, per the Plan-agent's catch above. The job-level routing tests (roughly lines 34-227) were untouched — confirmed they never referenced the storyboard module at all.

**Verification:** `python -c "from backend.app import app"` confirmed the full import chain resolves with no circular import, before running anything else. `python -m pytest tests/ -v` → **128 passed** — identical count to before the refactor (same test names too), confirming genuinely zero behavior change. `npm run test` (frontend, unaffected by this backend-only change) → 55 passed, re-run as a sanity check per the plan's verification section.

**Files changed:** `backend/core/storyboard_engine.py` (new), `backend/core/artifacts.py` (new), `backend/api/job_responses.py` (new), `backend/api/sse.py` (new), `backend/api/routes/storyboard.py` (trimmed), `tests/test_storyboard_helpers.py`, `tests/test_job_routes.py`, `tests/test_recovery.py`, `TODO.md`, `SESSION_STATE.md`. No frontend files, no `app.py`/`recovery.py` changes needed (confirmed both have no coupling to storyboard.py's internals beyond `storyboard.router`/generic `worker.resubmit()` dispatch).

**Remaining problems / blockers:** None blocking.
- Phase 3's other item (coverage measurement, `pytest-cov`/`vitest --coverage`) not started.
- Phase 2's retry/cancel attempt history not started — needs its own design pass.
- Phase 4 not started, gated on revisiting BUG-3's localhost-only scope decision.
- Nothing committed yet — waiting for approval.

**Exact next task:** Get approval to commit this session's changes on `feature/v1.2-development`. Once approved and ready to keep building: coverage measurement (Phase 3's remaining item) or retry/cancel attempt history (Phase 2's remaining item) are the two independent next candidates.

---

### 2026-07-18 (Phase 2.1) — `project_id` exposed in the dashboard, display only

**What was completed:** Continued v1.2 Phase 2's first item. Investigated before touching anything: `project_id` was already fully plumbed on the backend (`JobStatusResponse.project_id`, persisted in `job_store.py`, passed through by both `generate_script.py` and `storyboard.py` since the original Job Manager milestone) and already typed on the frontend (`types.ts`, both submit forms already collect it) — the only gap was that it was never actually rendered anywhere, exactly as `TODO.md`'s v1.2 analysis had named it. Scope was deliberately narrowed to display only (not the project-filter/grouping half of the original roadmap idea), confirmed with the user before implementing.

- `frontend/src/components/JobRow.tsx`: renders `job.project_id` as a small badge in the row header (between the job id link and the status badge), only when non-null. Since both `DashboardPage` and `JobDetailPage` already reuse `JobRow` directly, this surfaces `project_id` in both places from one change — no page-level edits needed.
- `frontend/src/App.css`: new `.job-row__project` style, matching the existing `.job-row__kind`/`.job-row__status` badge conventions rather than inventing a new visual pattern.
- `frontend/src/components/JobRow.test.tsx` (new): `JobRow` had no dedicated test file before this session (confirmed by grep — not referenced by any `*.test.*`) despite being the component both the dashboard and detail page depend on. 2 cases: badge renders when `project_id` is set, no badge/no stray `.job-row__project` element when `null`. Needed a `MemoryRouter` wrapper (first test file to need one) since `JobRow` renders a `react-router-dom` `<Link>`.
- No backend changes — confirmed unnecessary before starting.

**Validation:** `npm run test` → **55 passed** (53 prior + 2 new, 10 test files). `npm run build` → clean. `npm run lint` → clean except 2 pre-existing warnings in unrelated files (`JobArtifacts.tsx`, `JobTimeline.tsx`, both predate this session) — nothing from these changes. Backend suite unaffected (no backend files touched); last confirmed at 128 passed earlier this branch.

**Also cleaned up this session:** an out-of-band, uncommitted `SESSION_STATE.md` edit was found already sitting in the working tree from before this session started — a "v1.2 Phase 1 CI and dependency improvements" entry using a different heading level (`##` instead of this file's `###` convention) and a literal `<lockfile commit hash>` placeholder never filled in. Its content was accurate (frontend CI job and backend lockfile were both genuinely done — confirmed via `git log`: `cb84b1c` "Add frontend CI validation", `cef06c7` "Add backend dependency lockfile"), so it was corrected in place (heading level fixed, placeholder filled with the real hash) rather than deleted, immediately below this entry.

**Files changed:** `frontend/src/components/JobRow.tsx`, `frontend/src/App.css`, `frontend/src/components/JobRow.test.tsx` (new), `TODO.md`, `SESSION_STATE.md`. No backend files.

**Remaining problems / blockers:** None blocking.
- The project-filter/grouping half of the original `project_id` roadmap idea (a `<select>` mirroring `StatusFilter.tsx`) is still open, deliberately not built this pass.
- Retry/cancel attempt history (Phase 2's other item) not started — needs its own design pass.
- Phase 3 (`storyboard.py` refactor, coverage measurement) and Phase 4 (deeper capability work, gated on revisiting BUG-3's scope) not started.
- Nothing committed yet — waiting for approval.

**Exact next task:** Get approval to commit this session's changes (`JobRow.tsx`, `App.css`, `JobRow.test.tsx`, `TODO.md`, `SESSION_STATE.md`) on `feature/v1.2-development`. Once approved and ready to keep building: `storyboard.py`'s four-way refactor (Phase 3) and retry/cancel attempt history (Phase 2's remaining item) are the two independent next candidates — see `TODO.md`'s "Recommended next features" for the current tradeoff read.

---

### 2026-07-18 (Phase 1) — Frontend CI job + backend dependency lockfile

**What was completed:** Phase 1 of the v1.2 roadmap — the smallest, most directly evidence-based items, both closing gaps the CI-fix session's own incident had exposed.

- Added a frontend job to `.github/workflows/ci.yml`, parallel to the existing backend job: `npm ci && npm test && npm run build && npm run lint`. Closes the "CI never runs the frontend at all" gap named in the v1.2 kickoff analysis below.
- Added a backend dependency lockfile (`requirements.lock.txt`) — motivated by the CI-fix incident, where `requirements.txt`'s unbounded `pytest>=8.0` floor resolved to a different version on a fresh install than the long-lived local `.venv` had.
- Confirmed `pytest` (bare, no `-m`) now works directly from the repo root thanks to `pytest.ini`'s `pythonpath = .` (added the session before, `b657f5b`).
- Backend suite confirmed still green: **128 passed**.

**Commits:** `cb84b1c` — "Add frontend CI validation"; `cef06c7` — "Add backend dependency lockfile".

**Remaining problems / blockers:** None blocking. Next up per the roadmap: Phase 2 (`project_id` display — see the entry above, done next session) and, independently, the optional README architecture-tree refresh named in Phase 1 (still open, non-blocking).

**Exact next task:** Phase 2.1 (`project_id` in the dashboard) — done in the entry above.

---

### 2026-07-18 (v1.2 kickoff) — Architecture/feature/gap analysis for v1.2, no code changed

**What was completed:** Picked up from the CI-fix session's "Exact next task" (create `v1.1.1-ci-fix`, then start v1.2). Found the release tag already existed (`v1.1.1-ci-fix` on `b1c9981`) and a `feature/v1.2-development` branch already checked out, one commit ahead of `master` (`b657f5b`, "Fix pytest import path for local development" — adds `pytest.ini` with `pythonpath = .`, closing the "bare `pytest` still needs `-m`" residual gap named in the CI-fix entry). `TODO.md` already had a full, detailed "v1.2 Planning" section written (architecture review, current limitations, a 4-phase roadmap, 5 prioritized recommended next features) — but it existed only as an **uncommitted working-tree change** (`git diff` showed it as the entire delta on `TODO.md`), with no corresponding `SESSION_STATE.md` entry. This looked like a prior session's analysis pass that was cut off before its bookkeeping step (commit + session-state update), likely lost to a context compaction — so this session's job was to verify that existing analysis is still accurate rather than write a new one from scratch, then close the loop.

**Verification performed (all claims in `TODO.md`'s "v1.2 Planning" section checked against the live repo, not taken on faith):**

- Backend per-file line counts: `wc -l` on all 12 listed backend files matches exactly (`app.py` 78, `config.py` 80, `comfyui_client.py` 153, `llm_client.py` 82, `worker.py` 226, `job_store.py` 344, `events.py` 68, `recovery.py` 146, `gpu_memory.py` 33, `generate_script.py` 58, **`storyboard.py` 653** — confirmed as the largest file by a wide margin, `schemas.py` 102) — total 2023 lines, matching the claimed "~2000."
- Frontend: `frontend/src/**/*.{ts,tsx}` excluding tests totals 868 lines, matching the claimed "~870."
- Tests: `python -m pytest tests/ -v` → **128 passed** (backend); `cd frontend && npm run test` → **53 passed, 9 files** (frontend) — both exactly as claimed, both still 100% green on this branch.
- `.github/workflows/ci.yml` confirmed to run only `pip install -r requirements.txt` + `python -m pytest tests/ -v` — no `npm` step at all, confirming the "CI never runs the frontend" gap is real, not stale.
- `frontend/package.json` confirmed `test`/`build`/`lint` (`oxlint`) scripts all exist and are independently runnable, matching the claim that a frontend CI job would be a small addition, not new tooling.
- `output/` confirmed at 13MB across 7 real job directories, matching the retention-gap sizing claim.
- No stale claims found — every checked number in the analysis held exactly. **No changes made to `TODO.md`'s existing v1.2 Planning content** since it was already accurate; this session's contribution is the verification itself plus this session-state entry.

**Summary of the analysis now sitting in `TODO.md` (for a reader who wants it without opening that file):**

1. **Architecture** — FastAPI backend (~2000 lines) orchestrating separately-running ComfyUI + Ollama over one shared 8GB-GPU worker slot; SQLite persistence with an SSE event bus layered on top; React+Vite+TS frontend (~870 lines) with zero polling left, three `EventSource`-backed hooks. `storyboard.py` (653 lines) is the one file doing routes + business logic + SSE + artifact-metadata all at once.
2. **Existing features** — essentially everything through the "Job Output Artifacts"/SSE milestones: script generation, storyboard generation, job persistence + crash resume, retry/cancel, list/filter/detail-page/timeline/execution-logs/artifact-preview UI, all backend-tested (128 cases) and frontend-tested (53 cases), CI-gated (backend only).
3. **Missing capabilities (newly identified this analysis, beyond what was already tracked)** — CI doesn't run the frontend suite at all; `project_id` is captured and persisted but never surfaced in the UI (write-only field); no code coverage measurement on either side; README's architecture tree is stale (predates `job_store.py`/`recovery.py`/`events.py`/`llm_client.py`/`frontend/`/`tests/`/`pytest.ini`); two UX-visible cancellation gaps (`generate_script` can't be cancelled mid-run; ComfyUI cancellation is cooperative-only, never `/interrupt`-based).
4. **Roadmap** — 4 phases: (1) frontend CI job + backend dependency lockfile + optional README refresh — small, low-risk, directly motivated by the CI-fix session's own incident; (2) surface `project_id` in the dashboard + build real retry/cancel attempt history; (3) split `storyboard.py` along its four responsibilities + add coverage measurement; (4) deeper capability work (`/interrupt`, mid-run script-gen cancellation, real retention policy, any multi-user/remote-access question) explicitly gated on the user revisiting BUG-3's localhost-only scope decision — not recommended to start without that. Recommended starting point: Phase 1 item 1 (frontend CI job), smallest and most directly evidence-based.

**Files changed:** `SESSION_STATE.md` only (this entry). `TODO.md`'s v1.2 Planning section was already present in the working tree from before this session and is unchanged by it — still uncommitted.

**Remaining problems / blockers:** None blocking.

- **`TODO.md`'s v1.2 Planning section and this `SESSION_STATE.md` entry are both still uncommitted** — same working-tree state as found at the start of this session, now verified accurate. Per this project's standing convention (seen throughout the Session Log), commits wait for explicit user approval rather than happening automatically.
- No v1.2 feature work has started yet — this was an analysis-only pass, per explicit instruction not to modify application code.
- All previously-tracked bugs/gaps (BUG-5 cosmetic, no lockfile, no log/job retention, un-cached artifact `stat()`, the SSE informational trade-offs, no retry/cancel history) remain open and unchanged, same as listed in `TODO.md`.
- `New Text Document.txt` (repo root, untracked, empty) — not investigated or touched; not part of this session's scope and doesn't look like application source.

**Exact next task:** Get approval to commit the v1.2 Planning analysis (`TODO.md` + this `SESSION_STATE.md` entry) on `feature/v1.2-development`. Once approved to actually start building, Phase 1 item 1 (add a frontend job — `npm ci && npm run test && npm run build && npm run lint` — to `.github/workflows/ci.yml`, parallel to the existing backend job) is the recommended entry point: small, no live services needed, and closes a real blind spot (CI currently can't catch a frontend regression at all) before any new v1.2 feature lands on top of an unverified frontend.

---

### 2026-07-18 (latest) — CI fixed: `pytest` invocation was never putting the repo root on `sys.path`

**What was completed:** Continued from a "push pending commits + check CI" task. Both `cb090d7` and `0790c0a` turned out to already be on `origin/master` (nothing to push) — but checking GitHub's Actions API (unauthenticated REST, no `gh` CLI needed since this repo is public) surfaced a real, previously-unknown finding: **CI had failed on every single run since the workflow was created (4/4, run 1 through run 4), completely unrelated to any of this conversation's recent feature work.** The failing step, `pytest tests/ -v`, always exited code 2 in under a second — a collection error, not a real test failure.

Spent a full turn ruling out hypotheses before getting the real traceback: not a `pytest>=8.0` floating-version issue (a fresh install still resolves 9.1.1 and passes locally), not stale local files or a missing `.env` (a byte-for-byte fresh `git clone` of `origin/master` also passed), and — the most thorough check — not OS-specific either (installed Python 3.12 natively in WSL Ubuntu and ran the real test suite there: 128 passed in 2.9s). Every local reproduction attempt passed, which turned out to be the tell, not a dead end.

User then supplied the actual GitHub Actions traceback: `ModuleNotFoundError: No module named 'backend'` on every test file's import line. **Root cause:** `tests/` has no `__init__.py` and there's no `conftest.py` anywhere in the repo, so under pytest's default import mode, collecting a test file inserts only `tests/` onto `sys.path` — never the repo root — meaning `backend` (a real top-level package) is never importable. Every local reproduction this session used `python -m pytest`, and Python's own `-m` flag prepends the current working directory to `sys.path` before pytest even runs, silently papering over the gap. CI's workflow ran the bare `pytest tests/ -v` console-script instead (no `-m`), which has no such side effect — confirmed by reproducing the identical `ModuleNotFoundError` locally using the bare `pytest.exe` script instead of `python -m pytest`.

**Fix applied — the smallest of several viable options, approved before making the change:** [.github/workflows/ci.yml:22](.github/workflows/ci.yml#L22) changed from `pytest tests/ -v` to `python -m pytest tests/ -v` — a one-line change, exactly matching the invocation this project's local testing has always used. Not touched: `requirements.txt`, any test file, any application code. Named but deliberately not applied (would close the gap more permanently but wasn't the smallest fix asked for): a root-level `conftest.py` or a `pytest.ini`/`pyproject.toml` `pythonpath = .` setting, which would fix this for _any_ invocation style, not just CI's.

- **Verification:** `python -m pytest tests/ -v` (project's own `.venv`) → **128 passed, 1 warning in 47.51s** — no regressions, no other files touched.
- **Committed** as `b1c9981` — "Fix GitHub Actions pytest module path" (2 files, +26/-2).
- **Confirmed green on a real GitHub Actions run — the first passing CI run in this repository's history.** GitHub's Actions REST API (unauthenticated, same public-repo read path used to find the original bug) shows run #5 for commit `b1c9981c56` with `conclusion: "success"`, immediately following run #4's `failure` for `0790c0a` — independently verified, not just taken on report. The commit is already on `origin/master` (confirmed via `git status`/ahead-behind, 0/0) even though it wasn't pushed from within this conversation — consistent with the same out-of-band push pattern seen with `cb090d7`/`0790c0a` earlier.

**Files changed:** `.github/workflows/ci.yml` (one line), `SESSION_STATE.md`.

**Remaining problems / blockers:** None blocking. **CI milestone fully closed** — this was the last "never actually confirmed working" item that had been carried in every session's blockers list since CI was first added.

- The residual "bare `pytest` still breaks for anyone who doesn't type `-m`" gap is named above, not solved — a future session could add a root `conftest.py` or `pythonpath = .` if this becomes a recurring papercut.
- No release tag yet for the work since `v1.1-sse` (`0790c0ad44`) — `cb090d7` (SSE), `0790c0a` (storyboard helper tests), and now `b1c9981` (this CI fix) are all untagged. Recommended, not yet created: **`v1.1.1-ci-fix`** (patch-level, since this is an infra/tooling fix rather than a new feature — the existing tags are all feature milestones).
- All other previously-open low-priority items (SSE trade-offs, no log retention, un-cached artifact `stat()`, BUG-5, no dependency lockfile) unchanged, not in scope this session.

**Exact next task:** Create the next release tag (recommended: `v1.1.1-ci-fix` on `b1c9981`) once approved — not yet done.

---

### 2026-07-18 (yet later) — Test coverage extended to storyboard.py's pure helper functions

**What was completed:** Resumed via the user's standard "read TODO.md/SESSION_STATE.md, confirm git state, summarize, recommend next task, wait for approval" checklist. Confirmed HEAD `cb090d7` (the prior session's SSE migration, already committed), clean tree, 114/53 passing tests — and corrected a stale claim in this file's own "Recommended entry point" section, which still said the SSE changes "were not yet committed" even though they were, one turn earlier in the same conversation. `TODO.md`'s own backlog was down to bookkeeping/accepted-tradeoff items except one genuine leftover: direct test coverage for `storyboard.py`'s `_naive_shot_split`/`_apply_shot_to_workflow` (item 13, previously only exercised indirectly via `_run_storyboard_job`). Presented that as the recommended next task alongside two smaller non-coding loose ends (push `cb090d7` to `origin`; confirm CI fires) and waited for direction — user confirmed item 13.

- **New `tests/test_storyboard_helpers.py`** (14 cases, no async/store/worker dependencies needed since both functions are plain synchronous helpers): `_naive_shot_split` — empty script, whitespace-only script, blank lines skipped without leaving a gap in the assigned indices, sequential zero-based index assignment, per-line whitespace stripping, `description`/`prompt` set equal with `negative_prompt` always empty. `_apply_shot_to_workflow` — positive-node text set from `shot.prompt`, case-insensitive `CLIPTextEncode` title matching (`"POSITIVE"`, `"  Positive  "`, mixed case), the documented "an empty `shot.negative_prompt` must not clobber the workflow's own default negative text" behavior, three separate no-op cases (non-`CLIPTextEncode` node, `CLIPTextEncode` with an unrecognized title, `CLIPTextEncode` missing `_meta` entirely), and confirming the deep-copy invariant — the _original_ workflow dict's text is unchanged after the call while the _returned_ copy's text really did change (not just a no-op copy that happens to look identical).
- **Zero production code changes** — every one of the 14 tests passed against the existing implementation on the first run; every documented/assumed behavior held exactly as `storyboard.py`'s own code comments already described. `python -m pytest tests/ -v` → **128 passed** (114 prior + 14 new).

**Files changed:** `tests/test_storyboard_helpers.py` (new), `TODO.md`, `SESSION_STATE.md`. No backend/frontend source files touched. No live services needed or started — pure unit-test work.

**Remaining problems / blockers:** None blocking.

- Commit `cb090d7` (last session's SSE migration) still hasn't been pushed to `origin`.
- Whether GitHub Actions has actually run `.github/workflows/ci.yml` for real still isn't directly confirmed.
- All the SSE-related informational/accepted-tradeoff items from the last session (single-process assumption, browser connection cap, no custom SSE headers, the 404-signaling compromise), no retention/pruning for `job_logs`, and un-cached per-artifact `stat()` remain open by design — none were in scope for this session.
- **This session's changes (one new test file) are not yet committed — waiting for approval before committing, per explicit instruction.**

**Exact next task:** Get approval, then commit this session's `tests/test_storyboard_helpers.py` addition. Independently: push `cb090d7` to `origin`, and confirm CI fires for real.

---

### 2026-07-18 (latest) — Frontend polling replaced with Server-Sent Events

**What was completed:** Continued from `v1.0-job-artifacts` (HEAD `4ef7c15`, clean tree, 99/49 passing tests). Asked to design (not yet implement) an SSE migration answering an 11-point brief: where polling is used, which endpoints, what should stream, SSE vs. WebSocket, backend/frontend architecture, reconnection, failure handling, testing, migration plan, risks. Used Plan mode, dispatched a Plan agent specifically to pressure-test the trickiest backend mechanics before writing any code — it caught two real bugs in the draft: **(a)** publishing from inside `JobStore._run()`'s thread-dispatched sync callable would touch `asyncio.Queue` off the event-loop thread (unsafe, same class of bug as BUG-6 one layer up); **(b)** fetching an initial snapshot before subscribing would leave a gap where a change could be silently lost. Verified the agent's most load-bearing claims myself (installed `fastapi`/`starlette`/`anyio` versions, `JobStore`'s exact method list) before trusting them — both checked out. Wrote the full corrected design to the plan file and got it approved.

Mid-implementation, found a third design gap myself (not the agent's): pushing per-job snapshots to the dashboard's all-jobs stream can't correctly express a job _leaving_ a status-filtered view (the existing `updateJob` upsert helper can only add/replace, never remove) — a real regression vs. today's full-list-replace polling. Fixed by making that one stream signal-only ("something changed") and letting the client re-run its own already-correct, already-filtered `GET /api/jobs` fetch instead of trying to push granular per-job deltas there.

A user message arrived mid-turn asking to "resume the project from a clean state review," seemingly unaware this work was already mid-flight and approved — paused immediately (cleanly stopped a dangling background Playwright process and the live dev servers, no orphaned processes), gave an honest status report reconciling the two, and asked directly whether to finish the already-approved SSE work first or set it aside. Confirmed: finish it first.

- **Backend:** new `backend/core/events.py` (lock-free `EventBus`, `asyncio.Queue` per subscriber, bounded/drop-oldest). `JobStore` gained an optional `event_bus` param (defaults to a private instance, not the global singleton, so all ~20 existing `JobStore(Settings(...))` test call sites stay isolated) and a `_run_and_publish` wrapper all six mutating methods now use, publishing only after the threaded sqlite call returns (the fix for bug (a)). Three new SSE routes in `storyboard.py`: `/jobs/{id}/events` (full snapshot push), `/jobs/{id}/logs/events` (delta-only, real `Last-Event-ID` reconnect support), `/jobs/events` (signal-only, for the reason above). `app.py`'s CORS gained `Last-Event-ID` in `allow_headers` (this frontend is genuinely cross-origin; that header isn't CORS-safelisted, so reconnects would otherwise silently fail preflight). All three existing polling routes kept untouched, not removed. 15 new tests (7 `EventBus`, 8 SSE-route generator tests called directly, matching this repo's existing convention) — `python -m pytest tests/ -v` → **114 passed**.
- **Frontend:** zero new dependencies (`EventSource` is native). All three hooks kept their exact return shape — **zero consuming components changed**. `useJobList` opens one persistent connection for its whole lifetime regardless of filter changes (a `refreshRef` indirection always calls the current, filter-respecting `refresh()`). `useJob` keeps its one-shot 404 existence check before opening a stream, guarded against a real unmount/jobId-change race with a `cancelled` flag. New `FakeEventSource` test doubles (hand-rolled, no mocking library) in each hook's test file. `npm run test` → **53 passed**. `npm run build` clean.

**Live validation — the most thorough of any feature this project has shipped:** direct `curl` against all three SSE routes (including confirming `Last-Event-ID` correctly resumes from the right point). Real backend + real Vite dev server + Playwright: a new job appeared in the dashboard in under 1 second (not the old 3s ceiling); a real `generate_script` job's status/script/logs/timeline all updated live end-to-end (95s real Ollama run) with zero reloads, zero console errors. **Found a real bug live that neither planning nor unit tests caught**: `GET /api/jobs/events` was being swallowed by the pre-existing `GET /api/jobs/{job_id}` route (both 2 path segments, FastAPI matches registration order) — fixed by reordering. **Reconnection test**: killed the real backend process with a browser tab open, confirmed the same still-open tab's `EventSource` auto-reconnected (the `net::ERR_CONNECTION_RESET`/`REFUSED` console entries during the actual outage window are the correct, expected signature of this — not bugs), restarted the backend, submitted a new job via `curl`, confirmed the same tab picked it up with zero interaction.

**Files changed:** `backend/core/events.py` (new), `backend/core/job_store.py`, `backend/api/routes/storyboard.py`, `backend/app.py`, `tests/test_events.py` (new), `tests/test_job_routes.py`, `frontend/src/api.ts`, `frontend/src/useJob.ts`(+test), `frontend/src/useJobList.ts`(+test), `frontend/src/useJobLogs.ts`(+test), `frontend/src/components/JobExecutionLogs.test.tsx`, `TODO.md`, `SESSION_STATE.md`.

**Remaining problems / blockers:** None blocking.

- SSE's single-process assumption, the browser's 6-connection-per-origin cap, `EventSource`'s inability to set custom headers, and the 404-signaling compromise are all named explicitly in `TODO.md` as informational/low-priority, not solved.
- Whether GitHub Actions has actually run `.github/workflows/ci.yml` for real still isn't directly confirmed.
- Un-cached per-artifact `stat()`, no retention/pruning for `job_logs`/`jobs`/`shots`, `storyboard.py`'s other pure functions still untested, BUG-5 (cosmetic), a dependency lockfile — all low priority, unchanged.
- No live services running — backend (:8000) and Vite dev server (:5173) both stopped cleanly at the end; confirmed via `Get-NetTCPConnection`.
- **This session's changes are not yet committed.**

**Exact next task:** Commit this session's SSE migration changes (not yet done).

---

### 2026-07-18 (later still) — Job Output Artifacts added

**What was completed:** Continued from `v0.9-job-logs` (confirmed as an existing tag already pointing at HEAD `a687cd6`, already pushed to `origin` — apparently done between sessions). Confirmed clean tree, 94/34 passing tests before planning. Used the same staged-planning flow as the last two features: read the current output-storage/API code fresh, entered Plan mode, resolved two confirmed forks directly with the user (**metadata computed at read time**, not persisted — no schema change to `shots.files`; **inline preview + metadata** in the frontend, not just enriched links), dispatched a Plan agent for the detailed design, then verified its two most load-bearing claims myself before trusting them: confirmed `_run_storyboard_job` really does call `health_check()` before any shot rows are created (matters for live-validation design), and corrected an over-broad claim in its file list — only `JobTimeline.test.ts` actually had a `files: ['a.mp4']`-shaped fixture needing an update, not all four files it named (checked by grep).

- **Backend:** `backend/models/schemas.py` gained `ArtifactResponse`; `ShotStatusResponse.files` changed from `list[str]` to `list[ArtifactResponse]`. New `_artifact_from_path` helper in `storyboard.py` (`stat()` + `mimetypes.guess_type`, with a deliberately-justified `try/except OSError` around only the `.stat()` call — this is a confirmed single-user localhost deployment with unmediated filesystem access, so a file genuinely can disappear between being recorded and a later poll). `download_shot_file` gained an explicit `media_type=` so its header and the new JSON field can't silently diverge later. 5 new tests — `python -m pytest tests/ -v` → **99 passed**.
- **Frontend:** no new hook needed (the data was already in `JobStatusResponse`). New `formatBytes.ts` (sibling to `formatTime.ts`) and `JobArtifacts.tsx` (pure `classifyArtifact` + render component: `<video>`/`<img>`/link-only by content-type, each with an `onError` fallback). `JobRow.tsx` needed a mandatory shape fix regardless of UX choice — used the opportunity to add a `showFiles` prop (default `true`) so the detail page can suppress its now-redundant per-file links without touching the dashboard. 15 new tests — `npm run test` → **49 passed**. `npm run build` clean.

**Live validation, the strongest of any feature so far**: this machine's real `output/jobs.db` already held two genuine ComfyUI-produced jobs with real `.mp4` files (`7ec76e91...`, `bc89fff6...`) — opened one's real detail page and confirmed three actually-decodable `<video>` previews (`readyState: 4`, correct dimensions, not just present in the DOM), correct sizes, and zero duplicate download links from `JobRow` (confirming `showFiles={false}` worked). For the image/other-content-type/missing-file branches — unreachable live without a real ComfyUI run, since `health_check()` gates shot-row creation — used a throwaway script (scratchpad-only, same accepted pattern as BUG-1's TCP proxy) to write one fabricated job directly into the real `jobs.db`: a genuine 1x1 PNG rendered correctly as a real image, a non-media file showed a plain download link, and a shot pointing at a file that was never written showed "unknown size" with no crash. Deleted the fabricated job and its output directory immediately after, confirmed via `git status` and a directory listing that nothing fake was left behind.

**Files changed:** `backend/models/schemas.py`, `backend/api/routes/storyboard.py`, `tests/test_job_routes.py`, `frontend/src/types.ts`, `frontend/src/api.ts`, `frontend/src/formatBytes.ts` (new), `frontend/src/formatBytes.test.ts` (new), `frontend/src/components/JobArtifacts.tsx` (new), `frontend/src/components/JobArtifacts.test.tsx` (new), `frontend/src/components/JobRow.tsx`, `frontend/src/components/JobTimeline.test.ts` (fixture fix only), `frontend/src/pages/JobDetailPage.tsx`, `frontend/src/App.css`, `TODO.md`, `SESSION_STATE.md`.

**Remaining problems / blockers:** None blocking.

- Un-cached per-`stat()` cost on every job-status poll (new, low priority — see `TODO.md`).
- Whether GitHub Actions has actually run `.github/workflows/ci.yml` for real still isn't directly confirmed.
- No retention/pruning for `job_logs`/`jobs`/`shots`; `storyboard.py`'s other pure functions still lack direct tests; BUG-5 (cosmetic) and a dependency lockfile still open — all low priority, unchanged.
- No live services running — backend (:8000) and Vite dev server (:5173) both stopped cleanly at the end; confirmed via `Get-NetTCPConnection`.
- **This session's changes are not yet committed.**

**Exact next task:** Commit this session's Job Output Artifacts changes (not yet done).

---

### 2026-07-18 (later) — Job Execution Logs added; repo pushed to a real GitHub remote

**What was completed:** Two separate threads this session. First, at the user's request, this repo was finally given a real remote: tagged `v0.8-job-timeline`, added `origin` → `https://github.com/hokphaymany2025-oss/manytv-ai.git`, pushed `master` and both tags. Hit one snag — the GitHub repo had auto-initialized with a `main` branch (README-only commit) as its default, so `master` and `main` briefly diverged; per the user's explicit instruction, `master` was kept (not renamed to `main`) and `main` was deleted from the remote after GitHub's "refusing to delete the current branch" error was resolved by changing the repo's default branch first. Whether `.github/workflows/ci.yml` has actually fired for real on this now-remoted repo hasn't been directly confirmed (no `gh` CLI or web access available in-session) — named explicitly in `TODO.md` rather than assumed either way.

Second, the actual feature: **Job Execution Logs**, requested via a staged planning flow — a "continue analysis only, do not edit files" text-only pass first (job-lifecycle event inventory, SQLite-vs-filesystem, smallest API shape, frontend file list, migration risk), approved with specific amendments (add a `stage` field; curated events only, no high-frequency ticks; logs persist across retries), then a full Plan-mode pass including a dedicated Plan-agent design review before any code. Confirmed HEAD (`f794809`), clean tree, 80/27 passing tests before starting.

- **Backend:** new `job_logs` table in `job_store.py` (`id, job_id, timestamp, level, stage, message, shot_index`, surrogate autoincrement `id` as the real sort key rather than raw `timestamp`, since every write is already serialized through the existing lock) plus `add_job_log`/`get_job_logs`. New `LogLevel`/`LogStage` enums in `worker.py`. Explicit `add_job_log` calls added next to ~12 existing `logger.*` call sites across `worker.py`, `storyboard.py`, `recovery.py`, `generate_script.py` — chosen over a `logging.Handler`-based design specifically because `Handler.emit()` is sync while every `JobStore` write is `async def` (a Plan-agent design pass, run in parallel with a text-only analysis pass, independently converged on the same rejection). Two brand-new call sites added too: the shot-failure `except` blocks in `_run_storyboard_job` had no `logger` call at all before this. The one real design wrinkle — `comfyui_client.py`'s WS-drop-fallback warning — was relayed through the existing synchronous `on_progress` callback (no signature change to that already-shipped, BUG-1-live-validated method) via a capture-then-drain-in-a-`finally`-block pattern in `storyboard.py`, deliberately avoiding a naive design that would have stamped the persisted event's timestamp at drain time instead of when it actually happened. New `GET /api/jobs/{job_id}/logs` route, separate from `JobStatusResponse` (so the dashboard's `GET /api/jobs` list view doesn't carry full log history on every 3s poll). 14 new/extended tests — `python -m pytest tests/ -v` → **94 passed**.
- **Frontend:** new `useJobLogs.ts` (matching `useJob`/`useJobList`'s poll shape) and `JobExecutionLogs.tsx`, rendered on `JobDetailPage.tsx` below `<JobTimeline>`. First component-rendering test in this project (`JobExecutionLogs.test.tsx`) — surfaced that `@testing-library/jest-dom` was never installed, so its assertions use plain `className`/`textContent` checks rather than adding a new dependency for `toHaveClass`/`toBeInTheDocument`. `npm run test` → **34 passed** (27 + 7 new). `npm run build` clean.

**Live validation:** real backend + real Vite dev server, Playwright. A `generate_script` job showed "Starting job" while queued, then the generated-script and completed lines once done. A `storyboard` job (ComfyUI intentionally not running) showed the job-level failure entry with error-level red styling; retried it, let it fail again, and confirmed the one check genuinely unique to this feature vs. Job Timeline — **the log kept both attempts' entries together** (start → fail → retried → start → fail again, 5 total), never cleared. Zero console errors. Shot-level queued/done lines, the resume-reconciliation lines, and the WS-drop relay itself weren't exercised live (ComfyUI wasn't running, so no shot was ever submitted) — covered by the backend unit tests instead, same documented gap as every prior ComfyUI-dependent session.

**Files changed:** `backend/core/job_store.py`, `backend/core/worker.py`, `backend/api/routes/storyboard.py`, `backend/core/comfyui_client.py`, `backend/core/recovery.py`, `backend/api/routes/generate_script.py`, `backend/models/schemas.py`, `tests/test_job_store.py`, `tests/test_job_routes.py`, `tests/test_worker.py`, `tests/test_recovery.py`, `tests/test_comfyui_client.py`, `frontend/src/types.ts`, `frontend/src/api.ts`, `frontend/src/api.test.ts`, `frontend/src/useJobLogs.ts` (new), `frontend/src/useJobLogs.test.tsx` (new), `frontend/src/components/JobExecutionLogs.tsx` (new), `frontend/src/components/JobExecutionLogs.test.tsx` (new), `frontend/src/pages/JobDetailPage.tsx`, `frontend/src/App.css`, `TODO.md`, `SESSION_STATE.md`.

**Remaining problems / blockers:** None blocking.

- Whether GitHub Actions has actually run `.github/workflows/ci.yml` for real still isn't directly confirmed (no `gh`/web access this session).
- No retention/pruning for `job_logs` (new, low priority — same gap `jobs`/`shots` already have, `job_logs` just grows faster per job).
- `storyboard.py`'s pure functions still have no dedicated direct tests; BUG-5 (cosmetic) and a dependency lockfile still open — all low priority, unchanged.
- No live services running — backend (:8000) and Vite dev server (:5173) both stopped cleanly at the end; confirmed via `Get-NetTCPConnection`.
- **This session's changes are not yet committed.**

**Exact next task:** Commit this session's Job Execution Logs changes (not yet done). Independently: confirm the GitHub Actions workflow has actually fired on the now-remoted repo.

---

### 2026-07-18 ~06:10 — Job Timeline added (backend timestamps exposed + frontend display)

**What was completed:** Continued from the prior session's Job Detail page milestone, which had explicitly deferred timestamps. Confirmed HEAD (`b113d7f`), clean working tree, and both test suites passing (78 backend, 15 frontend) before planning. Used Plan mode with two `AskUserQuestion` forks resolved directly with the user before writing the plan: **current-attempt lifecycle only** (not a new append-only retry/cancel history table — out of scope, named explicitly as a risk) and **no new date library** (a small local `formatTime.ts`, native `Date`/`Intl` only). Re-reading `job_store.py` and `storyboard.py::_build_job_status_response` in full confirmed this was a pure exposure gap: every timestamp was already persisted and already read into the row dicts, just never copied into the response models — so **zero additional backend data was required**.

- **Backend:** `backend/models/schemas.py` — `JobStatusResponse` gained `created_at`/`started_at`/`finished_at`, `ShotStatusResponse` gained `created_at`/`submitted_at`/`finished_at`. `backend/api/routes/storyboard.py::_build_job_status_response` (shared by all four job routes) now passes them through. Tests: `tests/test_job_routes.py` +2 (job-level and shot-level round-trip through `get_job_status`) — `python -m pytest tests/ -v` → 80 passed.
- **Frontend:** new `frontend/src/formatTime.ts` (`formatAbsolute`/`formatRelative`) and `frontend/src/components/JobTimeline.tsx` (pure `buildTimelineEvents(job)`, tested in isolation, plus the rendering component). Rendered on `JobDetailPage.tsx` below the existing `<JobRow>`. `types.ts` mirrors the six new fields; updated three existing test fixtures (`api.test.ts`, `useJob.test.tsx`, `useJobList.test.tsx`) since the new fields are non-optional on the TS interface. Tests: `formatTime.test.ts` (6) + `JobTimeline.test.ts` (6, including a chronological-interleaving case with mixed job/shot timestamps) — `npm run test` → 27 passed (15 prior + 12 new). `npm run build` clean.

**Live validation:** started a real backend + real Vite dev server, drove both with Playwright. Submitted a `generate_script` job: timeline showed only "Job created" while queued, then correctly added "Job started" and "Job done" with sensible absolute/relative timestamps once it finished. Submitted a `storyboard` job with ComfyUI intentionally not running: timeline showed "Job created → Job started → Job failed", the real "ComfyUI is not reachable..." error surfaced on the row, and the Retry button was present and correctly enabled. Zero console errors in both runs (screenshots in the session scratchpad). Per-shot submitted/finished interleaving with a _real_ ComfyUI run wasn't exercised live, since ComfyUI wasn't running this session — that path is covered instead by the backend round-trip test and `JobTimeline.test.ts`'s interleaving case (explicitly noted as a scope boundary, not silently skipped).

**Files changed:** `backend/models/schemas.py`, `backend/api/routes/storyboard.py`, `tests/test_job_routes.py`, `frontend/src/types.ts`, `frontend/src/formatTime.ts` (new), `frontend/src/formatTime.test.ts` (new), `frontend/src/components/JobTimeline.tsx` (new), `frontend/src/components/JobTimeline.test.ts` (new), `frontend/src/pages/JobDetailPage.tsx`, `frontend/src/App.css`, `frontend/src/api.test.ts`, `frontend/src/useJob.test.tsx`, `frontend/src/useJobList.test.tsx`, `TODO.md`, `SESSION_STATE.md`.

**Remaining problems / blockers:** None blocking.

- CI still never observed running on a real GitHub Actions job — this repo still has no git remote. The only remaining backlog item with any real weight.
- Retry/cancel attempt history isn't shown anywhere (new, named explicitly as a Job Timeline scope limit, tracked in `TODO.md`'s "Missing features").
- `storyboard.py`'s pure functions still have no dedicated direct tests; BUG-5 (cosmetic) and a dependency lockfile still open — all low priority, unchanged.
- No live services running — backend (:8000) and Vite dev server (:5173) both stopped cleanly at the end; confirmed via `Get-NetTCPConnection`.

**Committed:**
53aa165 — Add job timeline with backend timestamps

**Exact next task:** Push this repo to a GitHub remote and confirm `.github/workflows/ci.yml` actually fires — the last standing item with any real weight on the backlog.

---

### 2026-07-17 ~23:15 — Job Detail page added (first real routing in this frontend)

**What was completed:** Continued from the prior session's "Exact next task" (push to GitHub) — but the user again asked to plan the next feature first, this time naming it directly: a Job Detail page. Confirmed HEAD (`d7a7f35`), clean working tree, and both test suites passing (78 backend, 10 frontend) per the user's explicit checklist before analyzing architecture. Re-read the current frontend files fresh (no drift from memory) and found the key fact that shaped the whole plan: `GET /api/jobs/{id}` already returns everything a detail view needs (zero backend changes required), but `JobStatusResponse` has never exposed `created_at`/`started_at`/`finished_at` even though `JobStore` persists them. Used Plan mode with two `AskUserQuestion` forks resolved directly with the user before writing the plan: real URL-based routing (`react-router-dom`) over an in-page toggle with no new dependency, and explicitly deferring timestamps to a later pass rather than bundling a backend change into this one.

- **`frontend/src/api.ts`**: new `ApiError` class (`extends Error`, carries `.status`) — `request()` now throws this instead of a plain `Error`, so callers can check the actual HTTP status instead of string-matching the message. First time this distinction was needed; existing catch sites (`ScriptForm`/`StoryboardForm`/`JobRow`) are unaffected since they only ever read `.message`.
- **`frontend/src/useJob.ts`** (new): single-job counterpart to `useJobList.ts` — same 3s-poll shape and same "keep last-known state on a transient failure" resilience, plus a `notFound` flag: a confirmed 404 (checked via `ApiError.status`) stops issuing further polls via a ref-guarded early return in `refresh` (a job that doesn't exist can never start existing, since nothing deletes jobs).
- **Routing shell**: `frontend/src/main.tsx` wraps `<App />` in `<BrowserRouter>`; `frontend/src/App.tsx` stopped being the dashboard itself and became route definitions (`/` and `/jobs/:id`) using the classic `<Routes>/<Route>` API, not the newer data-router/loader API — keeps all data-fetching exactly as it already worked via hooks.
- **`frontend/src/pages/DashboardPage.tsx`** (new): the old `App.tsx` body, moved verbatim.
- **`frontend/src/pages/JobDetailPage.tsx`** (new): reads `:id` via `useParams()`, calls `useJob`. Reuses `<JobRow>` directly (wrapped in `<ul className="job-list">` so the bare `<li>` it renders doesn't show a bullet marker outside its usual list context) rather than duplicating JobRow's status/error/script/shots/retry/cancel/download rendering — the detail page's actual value is the real per-job URL, not a different visual treatment of the same data.
- **`frontend/src/components/JobRow.tsx`**: the job id is now a `<Link to="/jobs/:id">` — the list's entry point into the detail page. Small CSS addition (`text-decoration: none` + `:hover` underline) so it doesn't look like a default blue browser link.
- Tests: `useJob.test.tsx` (new, 4 cases) and `api.test.ts` (+1, `ApiError.status` on a 404) — `npm run test` → 15 passed (10 prior + 5 new). `npm run build` clean with the new router.

**Live validation:** started a real backend + real Vite dev server, drove it with Playwright. Clicked a real job's id from the dashboard — URL changed to `/jobs/<real-id>`, confirmed via `page.url()`; browser back button returned to `/`; **hard-refreshed while on `/jobs/<id>`** (a direct-navigation, not an in-app click) and confirmed it still rendered the correct job — this was the one risk in the whole plan that depended on tooling behavior rather than app code (Vite's dev server SPA-fallback), now confirmed rather than assumed; navigated directly to a fabricated job id and got "Job not found." instead of an infinite loading state; clicked Retry on a real `failed` job (`732f843d-...`) from the detail page and confirmed its status flipped to `running` — proving the reused `JobRow`/`useJob.updateJob` wiring works correctly standalone, not just inside the list.

**Files changed:** `frontend/package.json`/`package-lock.json` (new `react-router-dom` dependency), `frontend/src/main.tsx`, `frontend/src/App.tsx`, `frontend/src/pages/DashboardPage.tsx` (new), `frontend/src/pages/JobDetailPage.tsx` (new), `frontend/src/useJob.ts` (new), `frontend/src/useJob.test.tsx` (new), `frontend/src/api.ts`, `frontend/src/api.test.ts`, `frontend/src/components/JobRow.tsx`, `frontend/src/App.css`, `TODO.md`, `SESSION_STATE.md`.

**Remaining problems / blockers:** None blocking.

- CI still never observed running on a real GitHub Actions job — this repo still has no git remote. The only remaining backlog item with any real weight.
- Job timestamps deferred (new, small backend+frontend item, tracked in `TODO.md`).
- `storyboard.py`'s pure functions still have no dedicated direct tests; BUG-5 (cosmetic) and a dependency lockfile still open — all low priority, unchanged.
- No live services running — backend (:8000) and Vite dev server (:5173) both stopped cleanly at the end; confirmed via `Get-NetTCPConnection`.
- **This session's changes are not yet committed.**

**Exact next task:** Push this repo to a GitHub remote and confirm `.github/workflows/ci.yml` actually fires — the last standing item with any real weight on the backlog. Commit this session's Job Detail page changes first (not yet done).

---

### 2026-07-17 ~22:45 — Status-filter UI added to the dashboard

**What was completed:** Continued from the prior session's "Exact next task," which named pushing to GitHub as the standing item — but the user instead asked to "plan the next feature" with a full architecture/design/risk review first. Confirmed HEAD (`ec2243b`) and both test suites passing (78 backend, 7 frontend) per the user's explicit checklist before planning anything. `TODO.md`'s remaining backlog (push to GitHub, two low-priority test items, an optional filter UI) didn't have an obvious single "next feature" — used `AskUserQuestion` to confirm it was the status-filter UI, the only remaining item with real design surface. Used Plan mode (overwriting the previous session's now-completed plan file) to write up context/architecture/design/affected-files/risks/tests before touching any code, per the user's explicit request.

- **`frontend/src/useJobList.ts`**: added `status`/`setStatus` state. `refresh` (a `useCallback`) now depends on `[status]` and calls `listJobs(status || undefined)` — since the existing mount/interval `useEffect` already depends on `refresh`'s identity, a filter change automatically triggers an immediate re-fetch with the new filter and a fresh interval continuing to poll with it. No other wiring needed.
- **`frontend/src/components/StatusFilter.tsx`** (new): a `<select>` with "All" plus the 7 known job statuses (`queued`/`running`/`resuming`/`cancelling`/`cancelled`/`done`/`failed`) as a local array, matching `JobRow.tsx`'s existing convention of small local status lists rather than a shared enum module.
- **`frontend/src/App.tsx`**: renders the new control next to the "Jobs" heading, wired to the hook's `status`/`setStatus`.
- **`frontend/src/components/JobList.tsx`**: fixed a stale UX string noticed while touching this file — the empty-state message still read "No jobs submitted from this browser yet." (a leftover from the deleted `localStorage`-tracking design, factually wrong since job history became server-side two sessions ago). Now shows "No jobs match this filter." vs. "No jobs yet." depending on whether a filter is active, via a new `filtered` prop.
- **`frontend/src/App.css`**: small additions (`.jobs-header`, `.status-filter`) reusing existing input styling conventions, no new patterns invented.
- Tests: `frontend/src/useJobList.test.tsx` +3 cases (`setStatus` triggers an immediate re-fetch with the right query arg, switching back to `''` re-fetches with no filter, the poll interval keeps using whatever filter is currently active after a change, verified with the same fake-timer pattern the existing interval test already used). `npm run test` → 10 passed (7 prior + 3 new). `npm run build` still compiles clean.

**Live validation:** started a real backend + real Vite dev server, confirmed via direct `curl` that the backend's actual accumulated history was 24 jobs total with 6 real `failed` ones (ComfyUI-unreachable failures from many past live-test sessions). Drove the real UI with Playwright: unfiltered dashboard showed all 24; selecting "Failed" in the live dropdown correctly narrowed the list to exactly 6, every visible row read `failed`; switching back to "All" restored all 24. Separately selected a status with zero real matches (`resuming`) and confirmed the new "No jobs match this filter." copy renders correctly. Zero console errors throughout both checks.

**Files changed:** `frontend/src/useJobList.ts`, `frontend/src/components/StatusFilter.tsx` (new), `frontend/src/App.tsx`, `frontend/src/components/JobList.tsx`, `frontend/src/App.css`, `frontend/src/useJobList.test.tsx`, `TODO.md`, `SESSION_STATE.md`.

**Remaining problems / blockers:** None blocking.

- CI still never observed running on a real GitHub Actions job — this repo still has no git remote. Now the only remaining backlog item with any real weight.
- `storyboard.py`'s `_naive_shot_split`/`_apply_shot_to_workflow` still have no dedicated direct tests (low priority).
- BUG-5 (cosmetic) and a dependency lockfile still open (low priority).
- No live services running — backend (:8000) and Vite dev server (:5173) both stopped cleanly at the end; confirmed via `Get-NetTCPConnection`.

**Committed:** `b9a9923` — "Add job status filtering to dashboard" (8 files, +157/-20). Working tree clean immediately after.

**Exact next task:** Push this repo to a GitHub remote and confirm `.github/workflows/ci.yml` actually fires — the only remaining backlog item with any real weight.

---

### 2026-07-17 ~22:15 — Frontend wired to GET /api/jobs; first automated frontend tests

**What was completed:** User asked for the two prior sessions' work to be committed first (done — see the commit note at the end of this entry), then asked for the next feature. Left as `<TBD>` in their milestone-status message, so confirmed via `AskUserQuestion`: wiring the frontend to the new `GET /api/jobs` endpoint (top of `TODO.md`'s backlog) over the other option (pushing to a GitHub remote). User's message also explicitly asked for "Review architecture / Add feature plan / Define tests" before starting, so used Plan mode (overwriting the previous session's now-unrelated plan file) rather than just diving in.

- **`frontend/src/api.ts`**: added `listJobs(status?: string)` → `GET /api/jobs` or `GET /api/jobs?status=...`.
- **`frontend/src/useJobList.ts`** (new, replaces `useTrackedJobs.ts` which was deleted outright — not left as dead code): polls `listJobs()` every 3s, holds `jobs: JobStatusResponse[]` directly. `refresh()` fires on mount, on each interval tick, and immediately after a new job is submitted (so it appears without waiting for the next tick). `updateJob(job)` still merges a single already-known-updated job (from a retry/cancel response) in place, same as before.
- **`frontend/src/App.tsx`**, **`ScriptForm.tsx`**, **`StoryboardForm.tsx`**: `onSubmitted` prop simplified from `(jobId: string) => void` to `() => void` — the id is no longer needed by the caller now that there's no client-side tracking to add it to.
- No backend changes at all — `GET /api/jobs` already returned exactly the shape needed.

**First automated frontend tests** (none existed before this — the initial frontend milestone was verified via TypeScript compilation + one live Playwright session only): added `vitest` + `@testing-library/react` + `jsdom` as devDependencies, config folded into the existing `vite.config.ts` (`test: { environment: 'jsdom', globals: true }`, no second config file), new `"test": "vitest run"` script. `frontend/src/api.test.ts` (3 cases — unfiltered call, `?status=` query building, error-detail propagation) and `frontend/src/useJobList.test.tsx` (4 cases — initial-mount fetch, interval re-fetch, in-place `updateJob`, resilience to a failed poll). One real debugging note worth keeping: mixing `@testing-library/react`'s `waitFor` (which polls via _real_ timers internally) with `vi.useFakeTimers()` hangs every assertion until vitest's own 5s test timeout — fixed by either not using fake timers at all (most tests) or engaging fake timers _before_ the hook mounts and flushing the initial effect with `vi.advanceTimersByTimeAsync(0)` instead of `waitFor` (the interval test). `npm run test` → 7 passed. `npm run build` still compiles clean.

**Live validation, the real point of this session:** started a real backend + real Vite dev server, then used the same throwaway Playwright setup from an earlier session (still cached in the scratchpad) to drive **two independent browser contexts** (separate `localStorage`, standing in for two different browsers/machines) against the live dashboard. Browser A submitted a new job; polled browser A specifically for the top row's id to _change_ from what it was before submitting (closes a real timing race an earlier, cruder version of this check had — comparing raw job-row counts between the two browsers is inherently racy right now anyway, since this backend still has genuinely queued/running jobs left over from earlier sessions' live tests, so total counts can legitimately drift between two measurements taken seconds apart regardless of anything this feature does). Once browser A's new job id was confirmed, polled browser B — which never submitted anything itself, in a fully separate context — and confirmed it showed that exact same job id within one poll interval, purely from `GET /api/jobs` server-side history. Zero console errors on a plain page load.

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
- **This live pass caught a real bug the 71 mocked tests had not** (BUG-6, now in `TODO.md`): the first run showed browser console errors that looked like a CORS misconfiguration (`No 'Access-Control-Allow-Origin' header`). Chased it down via the actual backend log rather than trusting the browser's framing of the error, and found the real cause: `sqlite3.InterfaceError: bad parameter or other API misuse` — `JobStore`'s single `sqlite3.Connection`, used via `asyncio.to_thread` from multiple concurrent requests (exactly what the frontend's polling does, and nothing before this session ever did), was never actually thread-safe despite `check_same_thread=False` (that flag only disables Python's _check_, not real concurrency safety). Fixed with a `threading.Lock` in `JobStore._run()`. Confirmed the fix's regression test reliably reproduces the exact real error 3/3 times when the lock is removed, and passes clean with it restored. Re-ran the full Playwright session against the restarted, fixed backend: zero console errors.
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

**Live validation (of what could be validated without a remote):** built a completely fresh, throwaway venv (outside the repo, in the session scratchpad) and ran the exact install+test sequence CI will run: `pip install -r requirements.txt` (confirmed no `torch` in the resolved set, ~1m49s, all from PyPI) then `pytest tests/ -v` → **35 passed in 12.5s**, torch entirely absent from that venv. This proves the workflow's steps are correct and self-sufficient. **What this does _not_ prove:** that GitHub Actions itself will actually run the workflow — this repo still has no git remote, so the YAML has never executed on a real GitHub-hosted runner. That's an explicit, named gap, not an oversight.

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

- BUG-3 (P1) — Wildcard CORS + zero authentication on every endpoint. Still open. (Incidentally, this session's Windows loopback-exemption finding is a mild point _in favor_ of the current localhost-only deployment being less exposed than a naive read of BUG-3 might suggest for same-machine threats specifically — though it does nothing to mitigate the actual documented risk, which is any browser tab on this machine making a same-origin-exempt request to the API.)
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

BUG-1 through BUG-6 are all closed; job persistence + resume, CORS/auth hardening, CI, job retry/cancellation, a first frontend, a list-jobs endpoint, the frontend using that endpoint, a status-filter UI, a Job Detail page, a Job Timeline, Job Execution Logs, Job Output Artifacts, a full migration from frontend polling to Server-Sent Events, and (as of this session) direct test coverage for `storyboard.py`'s pure helper functions are all done (see the `2026-07-18 (yet later)` Session Log entry above for full detail — that entry, plus the ones below it, supersede the "Repo state"/"Open questions" sections further down, which are historical snapshots and no longer current). Current state in brief:

- **This repo has a real GitHub remote**: `origin` → `https://github.com/hokphaymany2025-oss/manytv-ai.git`. The SSE migration commit (`cb090d7`) still hasn't been pushed there. Whether `.github/workflows/ci.yml` has actually fired on a real runner isn't directly confirmed yet (no `gh`/web access this session) — worth a quick check next time there's a reason to be in the GitHub UI.
- See `git log --oneline -5` / `git status` for the actual current HEAD and working-tree state rather than trusting this file — **this session's one new test file was not yet committed** as of this entry, deliberately waiting for explicit approval per the user's instruction.
- `output/jobs.db` (SQLite, gitignored) holds real persisted job history spanning many sessions' live tests, including two real jobs with genuine ComfyUI-produced `.mp4` files (`7ec76e91...`, `bc89fff6...`) useful for future live checks of anything artifact-related.
- **No live services running** — this session was pure unit-test work, nothing was started.
- Backend: 128 tests passing (`python -m pytest tests/ -v`). Frontend: 53 tests passing (`cd frontend && npm run test`, unchanged this session).

**Exact next task:** Get approval, then commit this session's `tests/test_storyboard_helpers.py` addition. Independently: push `cb090d7` to `origin`, and confirm the GitHub Actions workflow has actually fired for real on the now-remoted repo.

**Commands to resume:**

```powershell
cd D:\NewProjects\ManyTV
git log --oneline -5              # confirm what's actually committed
git status                        # confirm working tree state
python -m pytest tests/ -v        # confirm still 128 passed
cd frontend && npm run test       # confirm still 53 passed
```
