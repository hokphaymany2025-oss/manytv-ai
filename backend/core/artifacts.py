"""Display-metadata derivation for persisted job output files.

Split out of backend/api/routes/storyboard.py (v1.2 Phase 3).
"""

import mimetypes
from pathlib import Path
from typing import Optional

from backend.models.schemas import ArtifactResponse

# _artifact_from_path used to re-run a filesystem stat() on every call, i.e.
# on every job-status poll (GET /api/jobs, GET /api/jobs/{id}, retry,
# cancel, and the job_updated SSE snapshot all funnel through
# _build_job_status_response). A DONE shot's files entry, once set, is never
# rewritten (see job_store.py: only a DONE shot ever gets `files`, and
# nothing re-marks a DONE shot to re-run it) -- so the metadata for a given
# path is immutable for as long as that path is ever looked up again, and
# safe to cache indefinitely rather than re-stat()ing it every poll. A
# missing file's resolved size_bytes=None is cached the same way: nothing
# in this codebase ever writes a new file at a path already recorded in
# shots.files, so a stat() failure now will still be a failure later.
_artifact_cache: dict[str, ArtifactResponse] = {}


def _artifact_from_path(raw_path: str) -> ArtifactResponse:
    """Derives display metadata for one persisted output file.

    Result is cached by raw path (see module docstring above) -- computed
    once per distinct path, not re-derived from disk on every job-status
    read. The try/except here is real, not reflexive: this is a confirmed
    single-user localhost deployment with unmediated filesystem access to
    output/, so a file can genuinely disappear between being recorded and
    being read on a later poll -- letting stat() raise would turn one
    deleted file into a permanently-broken job status response.
    """
    cached = _artifact_cache.get(raw_path)
    if cached is not None:
        return cached

    path = Path(raw_path)
    content_type, _ = mimetypes.guess_type(path.name)
    try:
        size_bytes: Optional[int] = path.stat().st_size
    except OSError:
        size_bytes = None
    artifact = ArtifactResponse(filename=path.name, size_bytes=size_bytes, content_type=content_type)
    _artifact_cache[raw_path] = artifact
    return artifact
