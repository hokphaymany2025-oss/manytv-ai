"""Tests for backend/core/retention.py -- startup pruning of old terminal
job history and its output/<job_id>/ files. Uses a real JobStore (tmp_path
DB) and real filesystem directories under tmp_path, matching
tests/test_recovery.py's convention for this kind of orchestration module
rather than mocking JobStore or shutil.
"""

import asyncio
import time

from backend.core.config import Settings
from backend.core.job_store import JobStore
from backend.core.retention import prune_old_jobs


def _store(tmp_path) -> JobStore:
    return JobStore(Settings(db_path=str(tmp_path / "jobs.db")))


def test_prune_old_jobs_does_nothing_when_retention_disabled(tmp_path):
    settings = Settings(
        output_dir=str(tmp_path / "output"), db_path=str(tmp_path / "jobs.db"), job_retention_days=0,
    )
    store = _store(tmp_path)
    old = time.time() - 40 * 86400
    job_dir = tmp_path / "output" / "job-1"
    job_dir.mkdir(parents=True)

    async def scenario():
        await store.create_job("job-1", "storyboard", "done", {}, created_at=old)
        await prune_old_jobs(settings, store)

    asyncio.run(scenario())

    assert asyncio.run(store.get_job("job-1")) is not None
    assert job_dir.exists()


def test_prune_old_jobs_deletes_db_rows_and_output_directory(tmp_path):
    settings = Settings(
        output_dir=str(tmp_path / "output"), db_path=str(tmp_path / "jobs.db"), job_retention_days=30,
    )
    store = _store(tmp_path)
    old = time.time() - 40 * 86400
    job_dir = tmp_path / "output" / "job-1"
    job_dir.mkdir(parents=True)
    (job_dir / "shot_000.mp4").write_bytes(b"fake video")

    async def scenario():
        await store.create_job("job-1", "storyboard", "done", {}, created_at=old)
        await prune_old_jobs(settings, store)

    asyncio.run(scenario())

    assert asyncio.run(store.get_job("job-1")) is None
    assert not job_dir.exists()


def test_prune_old_jobs_leaves_recent_and_non_terminal_jobs_and_their_dirs_alone(tmp_path):
    settings = Settings(
        output_dir=str(tmp_path / "output"), db_path=str(tmp_path / "jobs.db"), job_retention_days=30,
    )
    store = _store(tmp_path)
    old = time.time() - 40 * 86400
    recent = time.time() - 5 * 86400
    recent_dir = tmp_path / "output" / "recent-done"
    recent_dir.mkdir(parents=True)
    running_dir = tmp_path / "output" / "old-running"
    running_dir.mkdir(parents=True)

    async def scenario():
        await store.create_job("recent-done", "storyboard", "done", {}, created_at=recent)
        await store.create_job("old-running", "storyboard", "running", {}, created_at=old)
        await prune_old_jobs(settings, store)

    asyncio.run(scenario())

    assert asyncio.run(store.get_job("recent-done")) is not None
    assert asyncio.run(store.get_job("old-running")) is not None
    assert recent_dir.exists()
    assert running_dir.exists()


def test_prune_old_jobs_tolerates_a_missing_output_directory(tmp_path):
    """A job row with no corresponding output/<job_id>/ dir (already
    deleted by hand, or a generate_script job that never wrote files)
    must not raise -- shutil.rmtree(ignore_errors=True) covers this."""
    settings = Settings(
        output_dir=str(tmp_path / "output"), db_path=str(tmp_path / "jobs.db"), job_retention_days=30,
    )
    store = _store(tmp_path)
    old = time.time() - 40 * 86400

    async def scenario():
        await store.create_job("job-1", "generate_script", "done", {}, created_at=old)
        await prune_old_jobs(settings, store)

    asyncio.run(scenario())

    assert asyncio.run(store.get_job("job-1")) is None
