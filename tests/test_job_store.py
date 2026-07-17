"""Tests for backend/core/job_store.py -- the SQLite persistence layer
backing job/shot state (see TODO.md's Job Manager + Queue + Resume System
milestone). Each test gets its own temp DB file via pytest's tmp_path, so
these never touch the real output/jobs.db.

Uses asyncio.run() directly in plain `def test_...` functions, matching
tests/test_comfyui_client.py's existing style -- no async test plugin.
"""

import asyncio

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
