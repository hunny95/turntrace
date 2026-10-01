"""Shared session-id validation and session-directory resolution.

Single place that turns a caller-supplied session id string into a
filesystem path under a sessions root. Used by server/session.py,
server/freeze_detector.py, and server/session_api.py so there is exactly
one (not several, weaker) implementation of this check.

Framework-free: no config/provider imports. This matters for
freeze_detector.py's independence guarantee (see its module docstring) --
importing this module must never transitively import server/config.py.
"""

from __future__ import annotations

import uuid
from pathlib import Path


class InvalidSessionId(ValueError):
    """Raised for any session id that is not a canonical UUID, or whose
    resolved directory would not live directly under the sessions root.
    Subclasses ValueError so existing `pytest.raises(ValueError)` callers
    keep working.
    """


def resolve_session_dir(session_id: str, sessions_dir: Path | str) -> Path:
    """Validate session_id and return its directory under sessions_dir.

    Only canonical ``uuid.UUID(x)`` round-trips are accepted: non-canonical
    forms (braces, urn prefix, uppercase-without-hyphens, etc.) are
    rejected, as are uppercase forms, i.e.
    ``str(uuid.UUID(session_id)) == session_id`` must hold exactly. The resolved path must be a direct child of the resolved
    sessions_dir -- this also rejects traversal strings.
    """
    try:
        parsed = uuid.UUID(session_id)
    except (ValueError, AttributeError, TypeError) as exc:
        raise InvalidSessionId("invalid session id") from exc

    if str(parsed) != session_id:
        raise InvalidSessionId("non-canonical session id")

    base = Path(sessions_dir).resolve()
    resolved = (base / session_id).resolve()
    if resolved.parent != base:
        raise InvalidSessionId("resolved session dir escapes sessions root")

    return resolved
