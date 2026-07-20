# Job cancellation & ComfyUI interrupt support

How `POST /api/jobs/{job_id}/cancel` actually stops a running job, for both
job kinds. Written up as a permanent reference (previously this design only
lived in code comments and `TODO.md`/`SESSION_STATE.md` session notes).

## Two independent mechanisms, one route

`POST /api/jobs/{job_id}/cancel` (`backend/api/routes/storyboard.py`) is the
single entry point for cancelling a `running`/`resuming` job. It always sets
`status = CANCELLING` first — persisted, so it survives a crash (a stale
`CANCELLING` row is finalized to `CANCELLED` at startup by
`backend/core/recovery.py`, never resubmitted) — then dispatches by kind:

| Kind             | Immediate path                                                              | Cooperative fallback |
|------------------|------------------------------------------------------------------------------|-----------------------|
| `storyboard`      | `request_shot_interrupt()` → ComfyUI's own cancel-by-id endpoint            | Per-shot `CANCELLING` check at the top of every shot-loop iteration, bounded by `GENERATION_TIMEOUT_SECONDS` |
| `generate_script` | `worker.cancel_current_job()` → `asyncio.Task.cancel()` on the handler's own task | None needed — the immediate path *is* the only mechanism, since the handler is one uninterruptible blocking LLM call |

These are unrelated implementations solving different problems.
`generate_script`'s cancellation never touches ComfyUI — it's pure asyncio
task-level cancellation inside `backend/core/worker.py`. Only `storyboard`
integrates with ComfyUI.

## ComfyUI interrupt integration

**Endpoint:** `POST /api/jobs/{prompt_id}/cancel` on ComfyUI itself — not the
legacy global `POST /interrupt`. Verified against the actual installed
ComfyUI source (`server.py`, `comfy_execution/jobs.py`), not general docs.
The newer endpoint's interrupt path (`PromptQueue.interrupt_if_running`) is
signalled under the same mutex that moves a prompt from running to done,
closing a race the legacy endpoint has: its own prompt_id check is a plain
scan followed by an *unconditional* global interrupt, not atomic with the
check itself.

**Detection:** `ComfyUIClient.wait_for_completion`'s WebSocket loop
(`backend/core/comfyui_client.py`) raises a distinct `ComfyUIInterrupted`
(subclass of `ComfyUIError`) on an `execution_interrupted` message, separate
from `execution_error`. This distinction matters because ComfyUI's own
`/history` does **not** preserve it after the fact — a finished prompt's
history shows `status_str="error"`, `completed=False` identically whether it
genuinely failed or was interrupted (confirmed against the installed
`execution.py`). Only the live WS message carries the real reason.

**Disambiguating "our cancel" from "something else interrupted it":**
catching `ComfyUIInterrupted` alone doesn't say *why* — it could be this
job's own `request_shot_interrupt()` call, or something external (e.g.
someone clicking ComfyUI's own UI stop button on a prompt ManyTV didn't ask
to cancel). `_run_storyboard_job` (`backend/core/storyboard_engine.py`)
resolves this by re-checking the job's own persisted status at the moment of
the exception:

- Job is `CANCELLING` → the shot is marked `FAILED`, `JobCancelled` is
  raised → the job ends `CANCELLED`.
- Job is anything else → the shot is marked `FAILED`, the exception is
  re-raised → the job ends `FAILED` (same as any other genuine failure,
  unchanged behavior).

This is also why the legacy global `/interrupt` couldn't have been used even
setting the atomicity issue aside — a global interrupt gives no way to know
it was *this* job's cancel request versus an unrelated one.

**A second, related fix folded into the same change:** `_raise_if_history_incomplete()`
in `comfyui_client.py` is wired into the two call sites that only ever see
`/history` and never the live WS stream — `_poll_history_until_done` (the
WS-drop-to-polling fallback) and the SUBMITTED-shot resume-reconciliation
branch (on backend restart, in `storyboard_engine.py`). Without it, both
would have treated a present-but-interrupted history entry as a silent
success. Both now treat it the same as "no history found" — resubmit the
shot, since there's nothing valid to reuse either way.

## Database / state impact

None to the schema. No new tables or columns. An interrupted shot reuses the
existing `ShotStatus.FAILED` value rather than a new enum member,
specifically so `/retry`'s existing `reset_shots_by_status(FAILED →
PENDING)` requires no changes — retry/cancel attempt history
(`job_attempts`, `job_logs`) stays consistent across an interruption exactly
as it would for any other shot failure.

## Known trade-off

`request_shot_interrupt()` is deliberately best-effort: if ComfyUI is
unreachable or rejects the cancel call, the between-shot cooperative check
is still the correctness guarantee underneath it — only the *immediate*
property is lost, not cancellation itself. Worst case, a job still stops,
just bounded by `GENERATION_TIMEOUT_SECONDS` (default 600s) instead of
near-instant.

## Verification

- **Unit tests:** `tests/test_cancellation.py` (16 cases) — the real
  ComfyUI endpoint call, `execution_interrupted` → `ComfyUIInterrupted`,
  `_raise_if_history_incomplete` via all three call sites, and the
  CANCELLING-vs-not branch (built by flipping the job's status *during* a
  mocked `wait_for_completion` call, to genuinely simulate the race).
- **Live-validated** against a real Arc A750 ComfyUI instance: a genuinely
  mid-KSampler shot (interrupted at step 7/20) confirmed via both the
  backend log (~620ms cancel-to-terminal) and ComfyUI's own `/history` for
  the prompt (`execution_interrupted` at the KSampler node). The rest of
  the job stopped too (the next shot was never submitted), and a follow-up
  job submitted immediately after completed normally — confirming no
  stuck worker state from the interrupt path.
- **Not yet live-tested:** the "external interrupt" branch (something other
  than ManyTV's own cancel request interrupting a prompt, e.g. ComfyUI's own
  UI stop button) — covered by a unit test but never triggered against a
  real instance. This exercises the *pre-existing* generic-failure code
  path with no new logic of its own, so the risk is low, but it remains an
  open item if full end-to-end confidence is wanted.
