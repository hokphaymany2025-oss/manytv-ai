"""SQLite-backed persistence for job and shot state.

Owns the only database this project has -- a single small SQLite file at
Settings.db_path, storing enough state to survive a backend restart and
resume interrupted storyboard jobs (see backend/core/recovery.py). Kept
separate from SingleSlotWorker (backend/core/worker.py) on purpose:
worker.py is scoped to concurrency (exactly one job running at a time) and
has zero knowledge of ComfyUI, shots, or workflows -- persistence is an
orthogonal concern, kept here so both stay independently testable.

Every public method wraps its synchronous sqlite3 call in asyncio.to_thread
-- no aiosqlite dependency, since the actual concurrency need here is
minimal. That does mean genuinely concurrent readers are possible (e.g. a
frontend polling several jobs' GET /api/jobs/{id} at once): asyncio.to_thread
dispatches to Python's default thread pool, so two calls can land on two
different OS threads at the same moment. sqlite3.connect(check_same_thread=
False) only disables Python's same-thread *check* -- it does not make the
single shared Connection object itself safe for concurrent use from
multiple threads at once (confirmed the hard way: concurrent polling from
a real browser produced `sqlite3.InterfaceError: bad parameter or other API
misuse`). _run() below serializes every call through one lock so only one
thread ever touches self._conn at a time; WAL mode is unrelated to this --
it's about crash-safety/reader-writer isolation at the file level, not
in-process thread-safety of one Python object.
"""

import asyncio
import json
import sqlite3
import threading
import time
from functools import lru_cache
from typing import Any, Optional

from backend.core.config import Settings, get_settings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id            TEXT PRIMARY KEY,
    kind          TEXT NOT NULL,
    status        TEXT NOT NULL,
    project_id    TEXT,
    workflow_name TEXT,
    payload       TEXT NOT NULL,
    result        TEXT,
    error         TEXT,
    created_at    REAL NOT NULL,
    started_at    REAL,
    finished_at   REAL
);

CREATE TABLE IF NOT EXISTS shots (
    job_id        TEXT NOT NULL,
    shot_index    INTEGER NOT NULL,
    status        TEXT NOT NULL,
    prompt_id     TEXT,
    files         TEXT,
    error         TEXT,
    created_at    REAL NOT NULL,
    submitted_at  REAL,
    finished_at   REAL,
    PRIMARY KEY (job_id, shot_index)
);
"""


class JobStore:
    def __init__(self, settings: Settings):
        db_path = settings.db_file_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self._lock = threading.Lock()

    async def _run(self, fn, /, *args: Any) -> Any:
        def locked_call() -> Any:
            with self._lock:
                return fn(*args)

        return await asyncio.to_thread(locked_call)

    # ---- jobs ----

    def _create_job_sync(
        self, job_id: str, kind: str, status: str, project_id: Optional[str],
        workflow_name: Optional[str], payload: dict[str, Any], created_at: float,
    ) -> None:
        self._conn.execute(
            "INSERT INTO jobs (id, kind, status, project_id, workflow_name, payload, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (job_id, kind, status, project_id, workflow_name, json.dumps(payload), created_at),
        )
        self._conn.commit()

    async def create_job(
        self,
        job_id: str,
        kind: str,
        status: str,
        payload: dict[str, Any],
        project_id: Optional[str] = None,
        workflow_name: Optional[str] = None,
        created_at: Optional[float] = None,
    ) -> None:
        await self._run(
            self._create_job_sync,
            job_id, kind, status, project_id, workflow_name, payload, created_at or time.time(),
        )

    def _update_job_status_sync(
        self, job_id: str, status: str, started_at: Optional[float],
        finished_at: Optional[float], result: Optional[str], error: Optional[str],
    ) -> None:
        self._conn.execute(
            "UPDATE jobs SET status = ?, "
            "started_at = COALESCE(?, started_at), "
            "finished_at = COALESCE(?, finished_at), "
            "result = COALESCE(?, result), "
            "error = COALESCE(?, error) "
            "WHERE id = ?",
            (status, started_at, finished_at, result, error, job_id),
        )
        self._conn.commit()

    async def update_job_status(
        self,
        job_id: str,
        status: str,
        started_at: Optional[float] = None,
        finished_at: Optional[float] = None,
        result: Optional[dict[str, Any]] = None,
        error: Optional[str] = None,
    ) -> None:
        await self._run(
            self._update_job_status_sync,
            job_id, status, started_at, finished_at,
            json.dumps(result) if result is not None else None,
            error,
        )

    def _get_job_sync(self, job_id: str) -> Optional[dict[str, Any]]:
        row = self._conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return dict(row) if row else None

    async def get_job(self, job_id: str) -> Optional[dict[str, Any]]:
        return await self._run(self._get_job_sync, job_id)

    def _list_jobs_sync(self, status_in: Optional[list[str]]) -> list[dict[str, Any]]:
        if status_in:
            placeholders = ",".join("?" for _ in status_in)
            rows = self._conn.execute(
                f"SELECT * FROM jobs WHERE status IN ({placeholders}) ORDER BY created_at DESC",
                tuple(status_in),
            ).fetchall()
        else:
            rows = self._conn.execute("SELECT * FROM jobs ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]

    async def list_jobs(self, status_in: Optional[list[str]] = None) -> list[dict[str, Any]]:
        """Lists jobs, newest first. `status_in` omitted or empty means no
        filter -- every job. `backend/core/recovery.py` always passes an
        explicit filter (only QUEUED/RUNNING/CANCELLING matter at startup);
        the no-filter path exists for GET /api/jobs (see storyboard.py),
        which needs full history.
        """
        return await self._run(self._list_jobs_sync, status_in)

    def _reset_job_for_retry_sync(self, job_id: str, status: str) -> None:
        self._conn.execute(
            "UPDATE jobs SET status = ?, error = NULL, result = NULL, "
            "started_at = NULL, finished_at = NULL WHERE id = ?",
            (status, job_id),
        )
        self._conn.commit()

    async def reset_job_for_retry(self, job_id: str, status: str) -> None:
        """Explicitly blanks error/result/started_at/finished_at and sets a
        fresh status -- distinct from update_job_status(), whose COALESCE
        semantics for those same fields mean passing None can never clear a
        previously-set value. Used ahead of a /retry re-enqueue so a stale
        error message or a completed result from the last run doesn't linger
        on a job about to run again from a clean slate.
        """
        await self._run(self._reset_job_for_retry_sync, job_id, status)

    # ---- shots ----

    def _upsert_shot_sync(
        self, job_id: str, shot_index: int, status: str, prompt_id: Optional[str],
        files: Optional[str], error: Optional[str],
        submitted_at: Optional[float], finished_at: Optional[float],
    ) -> None:
        self._conn.execute(
            "INSERT INTO shots "
            "(job_id, shot_index, status, prompt_id, files, error, created_at, submitted_at, finished_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(job_id, shot_index) DO UPDATE SET "
            "status = excluded.status, "
            "prompt_id = COALESCE(excluded.prompt_id, shots.prompt_id), "
            "files = COALESCE(excluded.files, shots.files), "
            "error = COALESCE(excluded.error, shots.error), "
            "submitted_at = COALESCE(excluded.submitted_at, shots.submitted_at), "
            "finished_at = COALESCE(excluded.finished_at, shots.finished_at)",
            (job_id, shot_index, status, prompt_id, files, error, time.time(), submitted_at, finished_at),
        )
        self._conn.commit()

    async def upsert_shot(
        self,
        job_id: str,
        shot_index: int,
        status: str,
        prompt_id: Optional[str] = None,
        files: Optional[list[str]] = None,
        error: Optional[str] = None,
        submitted_at: Optional[float] = None,
        finished_at: Optional[float] = None,
    ) -> None:
        await self._run(
            self._upsert_shot_sync,
            job_id, shot_index, status, prompt_id,
            json.dumps(files) if files is not None else None,
            error, submitted_at, finished_at,
        )

    def _get_shots_sync(self, job_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM shots WHERE job_id = ? ORDER BY shot_index", (job_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    async def get_shots(self, job_id: str) -> list[dict[str, Any]]:
        return await self._run(self._get_shots_sync, job_id)

    def _reset_shots_by_status_sync(self, job_id: str, from_status: str, to_status: str) -> None:
        self._conn.execute(
            "UPDATE shots SET status = ?, error = NULL, prompt_id = NULL, "
            "submitted_at = NULL, finished_at = NULL "
            "WHERE job_id = ? AND status = ?",
            (to_status, job_id, from_status),
        )
        self._conn.commit()

    async def reset_shots_by_status(self, job_id: str, from_status: str, to_status: str) -> None:
        """Bulk-transitions every shot row currently at `from_status` to
        `to_status`, clearing error/prompt_id/submitted_at/finished_at so the
        reset row looks like a freshly bootstrapped one. Rows at any other
        status (e.g. SUBMITTED, DONE) are untouched. Used by /retry to reset
        only genuinely FAILED shots back to PENDING -- SUBMITTED rows
        deliberately keep going through _run_storyboard_job's existing
        history-reconcile-or-resubmit branch unmodified.
        """
        await self._run(self._reset_shots_by_status_sync, job_id, from_status, to_status)


@lru_cache
def get_job_store() -> JobStore:
    return JobStore(get_settings())
