"""Display-metadata derivation for persisted job output files.

Split out of backend/api/routes/storyboard.py (v1.2 Phase 3).
"""

import mimetypes
from pathlib import Path
from typing import Optional

from backend.models.schemas import ArtifactResponse


def _artifact_from_path(raw_path: str) -> ArtifactResponse:
    """Derives display metadata for one persisted output file.

    Deliberately not persisted -- computed fresh from the raw path stored in
    shots.files every time a job's status is read (see TODO.md's Job Output
    Artifacts milestone). The try/except here is real, not reflexive: this is
    a confirmed single-user localhost deployment with unmediated filesystem
    access to output/, so a file can genuinely disappear between being
    recorded and being read on a later poll -- letting stat() raise would
    turn one deleted file into a permanently-broken job status response.
    """
    path = Path(raw_path)
    content_type, _ = mimetypes.guess_type(path.name)
    try:
        size_bytes: Optional[int] = path.stat().st_size
    except OSError:
        size_bytes = None
    return ArtifactResponse(filename=path.name, size_bytes=size_bytes, content_type=content_type)
