"""Tests for backend/core/job_store.py -- the SQLite persistence layer
backing job/shot state (see TODO.md's Job Manager + Queue + Resume System
milestone). Each test gets its own temp DB file via pytest's tmp_path, so
these never touch the real output/jobs.db.

Uses asyncio.run() directly in plain `def test_...` functions, matching
tests/test_comfyui_client.py's existing style -- no async test plugin.
"""

import asyncio
import concurrent.futures

from backend.core.config import Settings
from backend.core.job_store import JobStore


def _store(tmp_path) -> JobStore:
    return JobStore(Settings(db_path=str(tmp_path / "jobs.db")))


def test_create_and_get_job_round_trips_fields(tmp_path):
    store = _store(tmp_path)

    async def scenario():
        await store.create_job(
            "job-1", "storyboard", "queued", {"script": "hello"},
            project_id="proj-1", workflow_name="default_t2v", created_at=123.0,
        )
        return await store.get_job("job-1")

    row = asyncio.run(scenario())

    assert row["id"] == "job-1"
    assert row["kind"] == "storyboard"
    assert row["status"] == "queued"
    assert row["project_id"] == "proj-1"
    assert row["workflow_name"] == "default_t2v"
    assert row["created_at"] == 123.0
    assert row["started_at"] is None


def test_get_job_missing_returns_none(tmp_path):
    store = _store(tmp_path)
    assert asyncio.run(store.get_job("does-not-exist")) is None


def test_update_job_status_partial_fields_dont_clobber_prior_values(tmp_path):
    """A status-only update (e.g. QUEUED -> RUNNING) must not blank out
    fields set by an earlier write -- this COALESCE behavior is what makes
    the worker's 3 separate job-level writes (create, RUNNING, DONE/FAILED)
    safe to do independently rather than needing to resend everything."""
    store = _store(tmp_path)

    async def scenario():
        await store.create_job("job-1", "storyboard", "queued", {"a": 1}, created_at=1.0)
        await store.update_job_status("job-1", "running", started_at=2.0)
        await store.update_job_status("job-1", "done", finished_at=3.0, result={"shots": []})
        return await store.get_job("job-1")

    row = asyncio.run(scenario())

    assert row["status"] == "done"
    assert row["started_at"] == 2.0  # preserved from the RUNNING write
    assert row["finished_at"] == 3.0
    assert row["result"] == '{"shots": []}'
    assert row["error"] is None


def test_list_jobs_filters_by_status(tmp_path):
    store = _store(tmp_path)

    async def scenario():
        await store.create_job("q1", "storyboard", "queued", {}, created_at=1.0)
        await store.create_job("r1", "storyboard", "running", {}, created_at=1.0)
        await store.create_job("d1", "storyboard", "done", {}, created_at=1.0)
        return await store.list_jobs(status_in=["queued", "running"])

    rows = asyncio.run(scenario())
    ids = {r["id"] for r in rows}

    assert ids == {"q1", "r1"}


def test_list_jobs_with_no_filter_returns_every_job(tmp_path):
    """No status_in (or an empty list) means the full-history query GET
    /api/jobs needs -- distinct from recovery.py's always-filtered calls."""
    store = _store(tmp_path)

    async def scenario():
        await store.create_job("q1", "storyboard", "queued", {}, created_at=1.0)
        await store.create_job("d1", "storyboard", "done", {}, created_at=2.0)
        return await store.list_jobs()

    rows = asyncio.run(scenario())
    ids = {r["id"] for r in rows}

    assert ids == {"q1", "d1"}


def test_list_jobs_orders_newest_first(tmp_path):
    store = _store(tmp_path)

    async def scenario():
        await store.create_job("oldest", "storyboard", "done", {}, created_at=1.0)
        await store.create_job("newest", "storyboard", "done", {}, created_at=3.0)
        await store.create_job("middle", "storyboard", "done", {}, created_at=2.0)
        return await store.list_jobs()

    rows = asyncio.run(scenario())

    assert [r["id"] for r in rows] == ["newest", "middle", "oldest"]


def test_upsert_shot_merges_fields_across_status_transitions(tmp_path):
    """Mirrors the real sequence: PENDING at bootstrap -> SUBMITTED with a
    prompt_id -> DONE with files. Each write only supplies the fields it
    knows about; prompt_id set at SUBMITTED must survive into the DONE row
    even though the DONE write never repeats it."""
    store = _store(tmp_path)

    async def scenario():
        await store.upsert_shot("job-1", 0, "pending")
        await store.upsert_shot("job-1", 0, "submitted", prompt_id="prompt-abc", submitted_at=10.0)
        await store.upsert_shot("job-1", 0, "done", files=["output/job-1/shot_000/a.mp4"], finished_at=20.0)
        rows = await store.get_shots("job-1")
        return rows[0]

    row = asyncio.run(scenario())

    assert row["status"] == "done"
    assert row["prompt_id"] == "prompt-abc"  # preserved from the SUBMITTED write
    assert row["submitted_at"] == 10.0
    assert row["finished_at"] == 20.0
    assert row["files"] == '["output/job-1/shot_000/a.mp4"]'


def test_get_shots_ordered_by_shot_index_regardless_of_insert_order(tmp_path):
    store = _store(tmp_path)

    async def scenario():
        await store.upsert_shot("job-1", 2, "pending")
        await store.upsert_shot("job-1", 0, "pending")
        await store.upsert_shot("job-1", 1, "pending")
        return await store.get_shots("job-1")

    rows = asyncio.run(scenario())

    assert [r["shot_index"] for r in rows] == [0, 1, 2]


def test_get_shots_scoped_to_job_id(tmp_path):
    store = _store(tmp_path)

    async def scenario():
        await store.upsert_shot("job-1", 0, "pending")
        await store.upsert_shot("job-2", 0, "pending")
        return await store.get_shots("job-1")

    rows = asyncio.run(scenario())

    assert len(rows) == 1
    assert rows[0]["job_id"] == "job-1"


def test_reset_job_for_retry_clears_error_result_started_finished_and_sets_status(tmp_path):
    """update_job_status's COALESCE semantics can never blank a previously-
    set error/result -- reset_job_for_retry exists specifically to do that
    ahead of a /retry re-enqueue, so a stale failure message or a completed
    result from the last run doesn't linger on a job about to run again."""
    store = _store(tmp_path)

    async def scenario():
        await store.create_job("job-1", "storyboard", "queued", {"a": 1}, created_at=1.0)
        await store.update_job_status(
            "job-1", "failed", started_at=2.0, finished_at=3.0, error="boom",
        )
        await store.reset_job_for_retry("job-1", "queued")
        return await store.get_job("job-1")

    row = asyncio.run(scenario())

    assert row["status"] == "queued"
    assert row["error"] is None
    assert row["result"] is None
    assert row["started_at"] is None
    assert row["finished_at"] is None


def test_reset_job_for_retry_leaves_kind_payload_project_id_workflow_name_created_at_untouched(tmp_path):
    store = _store(tmp_path)

    async def scenario():
        await store.create_job(
            "job-1", "storyboard", "failed", {"shots": [1, 2]},
            project_id="proj-1", workflow_name="default_t2v", created_at=123.0,
        )
        await store.reset_job_for_retry("job-1", "queued")
        return await store.get_job("job-1")

    row = asyncio.run(scenario())

    assert row["kind"] == "storyboard"
    assert row["payload"] == '{"shots": [1, 2]}'
    assert row["project_id"] == "proj-1"
    assert row["workflow_name"] == "default_t2v"
    assert row["created_at"] == 123.0


def test_reset_shots_by_status_only_touches_matching_rows(tmp_path):
    """A job with one DONE, one SUBMITTED, one FAILED shot: only the FAILED
    one should transition, with error/prompt_id/submitted_at/finished_at all
    cleared, while the other two rows stay byte-for-byte unchanged."""
    store = _store(tmp_path)

    async def scenario():
        await store.upsert_shot("job-1", 0, "done", prompt_id="p0", files=["a.mp4"], finished_at=10.0)
        await store.upsert_shot("job-1", 1, "submitted", prompt_id="p1", submitted_at=20.0)
        await store.upsert_shot(
            "job-1", 2, "failed", prompt_id="p2", error="timed out", submitted_at=30.0, finished_at=31.0,
        )
        await store.reset_shots_by_status("job-1", "failed", "pending")
        return await store.get_shots("job-1")

    rows = asyncio.run(scenario())
    by_index = {r["shot_index"]: r for r in rows}

    assert by_index[0]["status"] == "done"
    assert by_index[0]["prompt_id"] == "p0"
    assert by_index[0]["finished_at"] == 10.0

    assert by_index[1]["status"] == "submitted"
    assert by_index[1]["prompt_id"] == "p1"
    assert by_index[1]["submitted_at"] == 20.0

    assert by_index[2]["status"] == "pending"
    assert by_index[2]["prompt_id"] is None
    assert by_index[2]["error"] is None
    assert by_index[2]["submitted_at"] is None
    assert by_index[2]["finished_at"] is None


def test_reset_shots_by_status_is_noop_when_none_match(tmp_path):
    store = _store(tmp_path)

    async def scenario():
        await store.upsert_shot("job-1", 0, "done", files=["a.mp4"])
        await store.reset_shots_by_status("job-1", "failed", "pending")
        return await store.get_shots("job-1")

    rows = asyncio.run(scenario())

    assert rows[0]["status"] == "done"
    assert rows[0]["files"] == '["a.mp4"]'


def test_concurrent_access_does_not_raise_sqlite_interface_error(tmp_path):
    """Regression test: a real browser polling several jobs' GET /api/jobs/{id}
    at once genuinely produced `sqlite3.InterfaceError: bad parameter or
    other API misuse` before _run() serialized access with a lock --
    asyncio.to_thread dispatches to a real thread pool, and
    sqlite3.connect(check_same_thread=False) only disables Python's
    same-thread *check*, it doesn't make the single shared Connection object
    safe for concurrent use from multiple threads at once. This hammers the
    store with a large batch of concurrent reads/writes across many
    threads (via asyncio.gather, which asyncio.to_thread backs with
    concurrent.futures.ThreadPoolExecutor) and asserts nothing raises.
    """
    store = _store(tmp_path)

    async def scenario():
        for i in range(20):
            await store.create_job(f"job-{i}", "storyboard", "queued", {"i": i}, created_at=float(i))

        async def hammer(i: int) -> None:
            job_id = f"job-{i % 20}"
            await store.update_job_status(job_id, "running", started_at=1.0)
            await store.get_job(job_id)
            await store.upsert_shot(job_id, 0, "pending")
            await store.get_shots(job_id)
            await store.list_jobs(status_in=["queued", "running"])

        await asyncio.gather(*(hammer(i) for i in range(200)))

    # concurrent.futures is imported so a failure here shows the real
    # underlying thread-pool exception, not just an opaque gather() failure.
    try:
        asyncio.run(scenario())
    except concurrent.futures.thread.BrokenThreadPool:
        raise AssertionError("Thread pool broke under concurrent JobStore access")
