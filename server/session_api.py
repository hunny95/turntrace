"""Gate F: read-only session review API.

Serves the already-persisted artifacts from Gates B/C/E (``session.json``,
``analysis.json``, ``recording.wav``) to the Next.js review UI. Never
mutates anything, never re-runs analysis, never returns a filesystem path
or the recording's on-disk filename.

Deliberately does NOT import ``config`` or any provider/bot module, so it
can be imported and exercised (FastAPI ``TestClient``) without provider
credentials or ``check_required_env_vars()`` -- see server/config.py and
server/server.py. The sessions root mirrors ``config.SESSIONS_DIR``
independently (same pattern as freeze_detector.py's ``_SESSIONS_DIR``) and
is overridable per-request via a FastAPI dependency for tests.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from path_safety import InvalidSessionId, resolve_session_dir

# Independent re-derivation of server/config.py's SESSIONS_DIR -- see
# module docstring. Not imported from config.py/session.py.
_DEFAULT_SESSIONS_DIR = Path(__file__).resolve().parent.parent / "data" / "sessions"

SESSION_JSON_NAME = "session.json"
RECORDING_WAV_NAME = "recording.wav"
ANALYSIS_JSON_NAME = "analysis.json"

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


def get_sessions_dir() -> Path:
    """FastAPI dependency returning the sessions root. Overridden in tests
    via ``app.dependency_overrides[get_sessions_dir]`` to point at a
    tmp_path fixture.
    """
    return _DEFAULT_SESSIONS_DIR


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_session_dir(session_id: str, sessions_dir: Path) -> Path:
    try:
        return resolve_session_dir(session_id, sessions_dir)
    except InvalidSessionId:
        raise HTTPException(status_code=400, detail="invalid session id") from None


def _load_session_json(session_dir: Path) -> dict[str, Any] | None:
    """Return the parsed session.json, or None if it doesn't exist / isn't
    a readable, valid dict with a matching id. Does not raise for
    malformed content -- callers decide how to react.
    """
    path = session_dir / SESSION_JSON_NAME
    try:
        with open(path, "r") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def _load_analysis_json(session_dir: Path) -> dict[str, Any]:
    """Return the analysis envelope for the detail response. Never negative
    on failure: unreadable/malformed analysis.json is reported as
    status "error", not "missing" and never a fabricated negative result.
    """
    path = session_dir / ANALYSIS_JSON_NAME
    if not path.is_file():
        return {"status": "missing"}
    try:
        with open(path, "r") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {"status": "error", "error": "invalid_analysis_json"}
    if not isinstance(data, dict) or "status" not in data:
        return {"status": "error", "error": "invalid_analysis_json"}
    return data


def _turn_count(transcript: list[dict[str, Any]]) -> int:
    """Distinct user turns. Modern sessions (Gate C+) tag every entry with
    a "turnId" key; count distinct non-null turnId values among user
    entries. Legacy sessions have no "turnId" key at all: count user
    entries directly (each user entry is one turn there -- see
    freeze_detector.py's `_build_turns` legacy branch, which this mirrors
    for consistency).
    """
    user_entries = [e for e in transcript if isinstance(e, dict) and e.get("role") == "user"]
    is_modern = any("turnId" in e for e in transcript if isinstance(e, dict))
    if is_modern:
        turn_ids = {e.get("turnId") for e in user_entries if e.get("turnId") is not None}
        return len(turn_ids)
    return len(user_entries)


def _analysis_summary_fields(analysis: dict[str, Any]) -> tuple[str, bool | None, int | None]:
    """Return (analysisStatus, freezeDetected, freezeStartMs) for the list
    summary, from a detail-shaped analysis envelope."""
    status = analysis.get("status")
    if status == "ok":
        freeze = analysis.get("freeze") or {}
        detected = freeze.get("detected")
        start_ms = freeze.get("startMs") if detected is True else None
        return "ok", (detected if isinstance(detected, bool) else None), start_ms
    if status == "error":
        return "error", None, None
    return "missing", None, None


def _recording_metadata(session: dict[str, Any]) -> dict[str, Any] | None:
    """The `recording` block minus the on-disk filename."""
    recording = session.get("recording")
    if not isinstance(recording, dict):
        return None
    return {
        "sampleRate": recording.get("sampleRate"),
        "channels": recording.get("channels"),
        "channelLayout": recording.get("channelLayout"),
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("")
async def list_sessions(sessions_dir: Path = Depends(get_sessions_dir)) -> list[dict[str, Any]]:
    root = Path(sessions_dir).resolve()
    if not root.is_dir():
        return []

    summaries: list[dict[str, Any]] = []
    for entry in root.iterdir():
        if not entry.is_dir():
            continue
        try:
            session_dir = resolve_session_dir(entry.name, root)
        except InvalidSessionId:
            continue  # non-UUID directory name: silently skipped

        session = _load_session_json(session_dir)
        if session is None:
            continue
        if session.get("id") != entry.name:
            continue  # id mismatch: silently skipped
        started_at = session.get("startedAt")
        if not isinstance(started_at, str):
            continue
        transcript = session.get("transcript")
        if not isinstance(transcript, list):
            continue
        latencies = session.get("latencies")
        duration_ms = session.get("durationMs")

        analysis = _load_analysis_json(session_dir)
        analysis_status, freeze_detected, freeze_start_ms = _analysis_summary_fields(analysis)

        summaries.append(
            {
                "id": entry.name,
                "startedAt": started_at,
                "durationMs": duration_ms if isinstance(duration_ms, int) else None,
                "turnCount": _turn_count(transcript),
                "latencyCount": len(latencies) if isinstance(latencies, list) else 0,
                "hasRecording": (session_dir / RECORDING_WAV_NAME).is_file(),
                "analysisStatus": analysis_status,
                "freezeDetected": freeze_detected,
                "freezeStartMs": freeze_start_ms,
            }
        )

    summaries.sort(key=lambda s: (s["startedAt"], s["id"]), reverse=True)
    return summaries


@router.get("/{session_id}")
async def get_session(
    session_id: str, sessions_dir: Path = Depends(get_sessions_dir)
) -> dict[str, Any]:
    session_dir = _safe_session_dir(session_id, sessions_dir)
    if not session_dir.is_dir():
        raise HTTPException(status_code=404, detail="session not found")

    session = _load_session_json(session_dir)
    if session is None:
        raise HTTPException(status_code=500, detail="invalid_session_json")
    if session.get("id") != session_id:
        raise HTTPException(status_code=500, detail="invalid_session_json")

    session_out = dict(session)
    session_out["recording"] = _recording_metadata(session)
    # Pre-Gate-C sessions have no "latencies" key; the API always returns a list.
    latencies = session.get("latencies")
    session_out["latencies"] = latencies if isinstance(latencies, list) else []

    analysis = _load_analysis_json(session_dir)

    return {"session": session_out, "analysis": analysis}


@router.get("/{session_id}/recording")
async def get_recording(
    session_id: str, sessions_dir: Path = Depends(get_sessions_dir)
) -> FileResponse:
    session_dir = _safe_session_dir(session_id, sessions_dir)
    wav_path = session_dir / RECORDING_WAV_NAME
    if not wav_path.is_file():
        raise HTTPException(status_code=404, detail="recording not found")
    return FileResponse(wav_path, media_type="audio/wav", filename=None)
