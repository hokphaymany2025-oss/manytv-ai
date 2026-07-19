"""Startup pruning of old, terminal job history and its output files.

Runs once at startup, alongside backend/core/recovery.py's crash-resume
pass -- disjoint by construction (recovery only ever touches non-terminal
jobs, pruning only ever touches terminal ones), so ordering between the two
doesn't matter. Disabled by default (Settings.job_retention_days == 0):
this permanently deletes job history and generated output files, so it's
opt-in rather than silently active.
"""

import logging
import shutil

from backend.core.config import Settings
from backend.core.job_store import JobStore

logger = logging.getLogger("manytv.retention")


async def prune_old_jobs(settings: Settings, job_store: JobStore) -> None:
    if settings.job_retention_days <= 0:
        return

    pruned_ids = await job_store.prune_old_jobs(settings.job_retention_days)
    if not pruned_ids:
        return

    for job_id in pruned_ids:
        job_dir = settings.output_path / job_id
        shutil.rmtree(job_dir, ignore_errors=True)

    logger.info(
        "Pruned %d job(s) older than %d day(s): %s",
        len(pruned_ids), settings.job_retention_days, ", ".join(pruned_ids),
    )
