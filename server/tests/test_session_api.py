"""Gate F: read-only session review API tests.

Everything here runs against a tmp_path sessions root via FastAPI
dependency override -- no real session data, no provider credentials, no
import of server.py/bot.py/config.py (session_api.py must not require
them; see its module docstring).
"""

from __future__ import annotations

import json
import uuid
import wave
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import session_api


def make_app(sessions_dir: Path) -> TestClient:
    app = FastAPI()
    app.include_router(session_api.router)
    app.dependency_overrides[session_api.get_sessions_dir] = lambda: sessions_dir
    return TestClient(app)


def write_wav(path: Path, num_channels: int = 2, sample_rate: int = 8000, n_frames: int = 80) -> bytes:
    frame = b"\x00\x00" * num_channels
    audio = frame * n_frames
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(num_channels)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(audio)
    return audio


def make_session(
    tmp_path: Path,
    *,
    session_id: str | None = None,
    started_at: str = "2026-01-01T00:00:00.000Z",
    duration_ms: int = 10000,
    transcript: list | None = None,
    latencies: list | None = None,
    with_recording: bool = True,
    with_analysis: dict | str | None = "unset",
    id_mismatch: bool = False,
) -> tuple[str, Path]:
    session_id = session_id or str(uuid.uuid4())
    session_dir = tmp_path / session_id
    session_dir.mkdir(parents=True)

    if transcript is None:
        transcript = [
            {"turnId": 1, "role": "user", "text": "hi", "timestampMs": 100},
            {"turnId": 1, "role": "assistant", "text": "hello", "timestampMs": 500},
        ]
    if latencies is None:
        latencies = [{"turnId": 1, "userStopMs": 100, "botStartMs": 400, "latencyMs": 300}]

    recording = None
    if with_recording:
        write_wav(session_dir / "recording.wav")
        recording = {
            "file": "recording.wav",
            "sampleRate": 8000,
            "channels": 2,
            "channelLayout": ["user", "bot"],
        }

    payload = {
        "id": session_id if not id_mismatch else str(uuid.uuid4()),
        "startedAt": started_at,
        "durationMs": duration_ms,
        "recording": recording,
        "transcript": transcript,
        "latencies": latencies,
    }
    with open(session_dir / "session.json", "w") as f:
        json.dump(payload, f)

    if with_analysis == "unset":
        with_analysis = {
            "version": 1,
            "status": "ok",
            "freeze": {"detected": False, "reason": "no_persistent_bot_audio_freeze", "detail": "x"},
        }
    if with_analysis is not None:
        with open(session_dir / "analysis.json", "w") as f:
            json.dump(with_analysis, f)

    return session_id, session_dir


# ---------------------------------------------------------------------------
# GET /api/sessions -- list
# ---------------------------------------------------------------------------


def test_list_empty(tmp_path):
    client = make_app(tmp_path)
    resp = client.get("/api/sessions")
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_newest_first(tmp_path):
    id_a, _ = make_session(tmp_path, started_at="2026-01-01T00:00:00.000Z")
    id_b, _ = make_session(tmp_path, started_at="2026-01-02T00:00:00.000Z")
    client = make_app(tmp_path)
    resp = client.get("/api/sessions")
    ids = [s["id"] for s in resp.json()]
    assert ids == [id_b, id_a]


def test_list_tie_break_by_id(tmp_path):
    ts = "2026-01-01T00:00:00.000Z"
    id_a, _ = make_session(tmp_path, started_at=ts)
    id_b, _ = make_session(tmp_path, started_at=ts)
    client = make_app(tmp_path)
    resp = client.get("/api/sessions")
    ids = [s["id"] for s in resp.json()]
    assert ids == sorted([id_a, id_b], reverse=True)


def test_list_fields(tmp_path):
    session_id, _ = make_session(tmp_path)
    client = make_app(tmp_path)
    row = client.get("/api/sessions").json()[0]
    assert set(row.keys()) == {
        "id",
        "startedAt",
        "durationMs",
        "turnCount",
        "latencyCount",
        "hasRecording",
        "analysisStatus",
        "freezeDetected",
        "freezeStartMs",
    }
    assert row["turnCount"] == 1
    assert row["latencyCount"] == 1
    assert row["hasRecording"] is True
    assert row["analysisStatus"] == "ok"
    assert row["freezeDetected"] is False
    assert row["freezeStartMs"] is None


def test_list_turn_count_legacy_no_turn_ids(tmp_path):
    transcript = [
        {"role": "user", "text": "a", "timestampMs": 0},
        {"role": "assistant", "text": "b", "timestampMs": 100},
        {"role": "user", "text": "c", "timestampMs": 200},
    ]
    make_session(tmp_path, transcript=transcript, latencies=[])
    client = make_app(tmp_path)
    row = client.get("/api/sessions").json()[0]
    assert row["turnCount"] == 2


def test_list_freeze_detected_row(tmp_path):
    analysis = {
        "version": 1,
        "status": "ok",
        "freeze": {"detected": True, "startMs": 4000, "endMs": 10000, "startTurnId": 2},
    }
    make_session(tmp_path, with_analysis=analysis)
    client = make_app(tmp_path)
    row = client.get("/api/sessions").json()[0]
    assert row["analysisStatus"] == "ok"
    assert row["freezeDetected"] is True
    assert row["freezeStartMs"] == 4000


def test_list_ignores_non_uuid_directory(tmp_path):
    (tmp_path / "not-a-uuid").mkdir()
    (tmp_path / "not-a-uuid" / "session.json").write_text("{}")
    client = make_app(tmp_path)
    assert client.get("/api/sessions").json() == []


def test_list_ignores_missing_session_json(tmp_path):
    (tmp_path / str(uuid.uuid4())).mkdir()
    client = make_app(tmp_path)
    assert client.get("/api/sessions").json() == []


def test_list_ignores_invalid_session_json(tmp_path):
    d = tmp_path / str(uuid.uuid4())
    d.mkdir()
    (d / "session.json").write_text("not json")
    client = make_app(tmp_path)
    assert client.get("/api/sessions").json() == []


def test_list_ignores_id_mismatch(tmp_path):
    make_session(tmp_path, id_mismatch=True)
    client = make_app(tmp_path)
    assert client.get("/api/sessions").json() == []


def test_list_ignores_file_not_directory(tmp_path):
    (tmp_path / f"{uuid.uuid4()}.txt").write_text("x")
    client = make_app(tmp_path)
    assert client.get("/api/sessions").json() == []


def test_list_no_paths_in_body(tmp_path):
    make_session(tmp_path)
    client = make_app(tmp_path)
    body = client.get("/api/sessions").text
    assert str(tmp_path) not in body
    assert "recording.wav" not in body


# ---------------------------------------------------------------------------
# GET /api/sessions/{id} -- detail
# ---------------------------------------------------------------------------


def test_detail_happy_path(tmp_path):
    session_id, _ = make_session(tmp_path)
    client = make_app(tmp_path)
    resp = client.get(f"/api/sessions/{session_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["session"]["id"] == session_id
    assert body["analysis"]["status"] == "ok"


def test_detail_legacy_session_without_latencies_key(tmp_path):
    """Pre-Gate-C session.json has no "latencies" key and no turnIds."""
    session_id, session_dir = make_session(
        tmp_path,
        transcript=[
            {"role": "user", "text": "hi", "timestampMs": 100},
            {"role": "assistant", "text": "hello", "timestampMs": 500},
        ],
    )
    payload = json.loads((session_dir / "session.json").read_text())
    del payload["latencies"]
    (session_dir / "session.json").write_text(json.dumps(payload))

    client = make_app(tmp_path)
    resp = client.get(f"/api/sessions/{session_id}")
    assert resp.status_code == 200
    assert resp.json()["session"]["latencies"] == []

    summary = client.get("/api/sessions").json()[0]
    assert summary["latencyCount"] == 0
    assert summary["turnCount"] == 1


def test_detail_recording_block_has_no_file_field(tmp_path):
    session_id, _ = make_session(tmp_path)
    client = make_app(tmp_path)
    body = client.get(f"/api/sessions/{session_id}").json()
    recording = body["session"]["recording"]
    assert "file" not in recording
    assert recording == {"sampleRate": 8000, "channels": 2, "channelLayout": ["user", "bot"]}


def test_detail_no_recording_gives_null_block(tmp_path):
    session_id, _ = make_session(tmp_path, with_recording=False)
    client = make_app(tmp_path)
    body = client.get(f"/api/sessions/{session_id}").json()
    assert body["session"]["recording"] is None


def test_detail_analysis_missing(tmp_path):
    session_id, _ = make_session(tmp_path, with_analysis=None)
    client = make_app(tmp_path)
    body = client.get(f"/api/sessions/{session_id}").json()
    assert body["analysis"] == {"status": "missing"}


def test_detail_analysis_negative(tmp_path):
    analysis = {"version": 1, "status": "ok", "freeze": {"detected": False, "reason": "x", "detail": "y"}}
    session_id, _ = make_session(tmp_path, with_analysis=analysis)
    client = make_app(tmp_path)
    body = client.get(f"/api/sessions/{session_id}").json()
    assert body["analysis"]["status"] == "ok"
    assert body["analysis"]["freeze"]["detected"] is False


def test_detail_analysis_error_status(tmp_path):
    analysis = {"version": 1, "status": "error", "error": "missing_recording"}
    session_id, _ = make_session(tmp_path, with_analysis=analysis)
    client = make_app(tmp_path)
    body = client.get(f"/api/sessions/{session_id}").json()
    assert body["analysis"] == analysis


def test_detail_malformed_analysis_json_is_error_not_negative(tmp_path):
    session_id, session_dir = make_session(tmp_path, with_analysis=None)
    (session_dir / "analysis.json").write_text("{not json")
    client = make_app(tmp_path)
    body = client.get(f"/api/sessions/{session_id}").json()
    assert body["analysis"] == {"status": "error", "error": "invalid_analysis_json"}


def test_detail_invalid_uuid_rejected(tmp_path):
    client = make_app(tmp_path)
    resp = client.get("/api/sessions/not-a-uuid")
    assert resp.status_code in (400, 422)


@pytest.mark.parametrize(
    "malformed",
    [
        "{11111111-1111-1111-1111-111111111111}",  # braces
        "urn:uuid:11111111-1111-1111-1111-111111111111",  # urn prefix
        "11111111111111111111111111111111",  # uppercase-without-hyphens form (32 hex, no dashes but lower ok normally)
        "AAAAAAAA-1111-1111-1111-111111111111",  # uppercase hyphenated form
    ],
)
def test_detail_non_canonical_uuid_rejected(tmp_path, malformed):
    client = make_app(tmp_path)
    resp = client.get(f"/api/sessions/{malformed}")
    assert resp.status_code in (400, 422, 404)


def test_detail_unknown_uuid_404(tmp_path):
    client = make_app(tmp_path)
    resp = client.get(f"/api/sessions/{uuid.uuid4()}")
    assert resp.status_code == 404


def test_detail_malformed_session_json_is_500_class(tmp_path):
    session_id = str(uuid.uuid4())
    d = tmp_path / session_id
    d.mkdir()
    (d / "session.json").write_text("not json")
    client = make_app(tmp_path)
    resp = client.get(f"/api/sessions/{session_id}")
    assert 500 <= resp.status_code < 600


def test_detail_no_absolute_path_anywhere(tmp_path):
    session_id, _ = make_session(tmp_path)
    client = make_app(tmp_path)
    body = client.get(f"/api/sessions/{session_id}").text
    assert str(tmp_path) not in body


# ---------------------------------------------------------------------------
# GET /api/sessions/{id}/recording
# ---------------------------------------------------------------------------


def test_recording_returns_exact_bytes(tmp_path):
    session_id, session_dir = make_session(tmp_path)
    expected = (session_dir / "recording.wav").read_bytes()
    client = make_app(tmp_path)
    resp = client.get(f"/api/sessions/{session_id}/recording")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "audio/wav"
    assert resp.content == expected


def _write_large_recording(session_dir: Path) -> bytes:
    """~3 MiB stereo 16-bit WAV with position-dependent bytes (not all zeros)."""
    n_frames = 800_000
    audio = bytes((i * 7) % 256 for i in range(n_frames * 4))
    with wave.open(str(session_dir / "recording.wav"), "wb") as wav_file:
        wav_file.setnchannels(2)
        wav_file.setsampwidth(2)
        wav_file.setframerate(24000)
        wav_file.writeframes(audio)
    return (session_dir / "recording.wav").read_bytes()


@pytest.mark.parametrize(
    "start,end",
    [
        (0, 1048575),  # first MiB
        (1048576, 2097151),  # second MiB (media players fetch this next)
        (2500000, 2600000),  # later range
    ],
)
def test_recording_byte_ranges(tmp_path, start, end):
    session_id, session_dir = make_session(tmp_path)
    data = _write_large_recording(session_dir)
    client = make_app(tmp_path)
    resp = client.get(f"/api/sessions/{session_id}/recording", headers={"Range": f"bytes={start}-{end}"})
    assert resp.status_code == 206
    assert resp.headers["content-type"] == "audio/wav"
    assert resp.headers["content-range"] == f"bytes {start}-{end}/{len(data)}"
    assert int(resp.headers["content-length"]) == end - start + 1
    assert resp.content == data[start : end + 1]


def test_recording_open_ended_range_to_eof(tmp_path):
    session_id, session_dir = make_session(tmp_path)
    data = _write_large_recording(session_dir)
    client = make_app(tmp_path)
    start = len(data) - 1000
    resp = client.get(f"/api/sessions/{session_id}/recording", headers={"Range": f"bytes={start}-"})
    assert resp.status_code == 206
    assert resp.headers["content-range"] == f"bytes {start}-{len(data) - 1}/{len(data)}"
    assert resp.content == data[start:]


def test_recording_full_response_advertises_ranges(tmp_path):
    session_id, session_dir = make_session(tmp_path)
    data = _write_large_recording(session_dir)
    client = make_app(tmp_path)
    resp = client.get(f"/api/sessions/{session_id}/recording")
    assert resp.status_code == 200
    assert resp.headers["accept-ranges"] == "bytes"
    assert resp.content == data


def test_recording_unsatisfiable_range_416(tmp_path):
    session_id, session_dir = make_session(tmp_path)
    data = _write_large_recording(session_dir)
    client = make_app(tmp_path)
    resp = client.get(
        f"/api/sessions/{session_id}/recording", headers={"Range": f"bytes={len(data) + 10}-"}
    )
    assert resp.status_code == 416
    assert resp.headers["content-range"] == f"bytes */{len(data)}"


def test_recording_missing_404(tmp_path):
    session_id, _ = make_session(tmp_path, with_recording=False)
    client = make_app(tmp_path)
    resp = client.get(f"/api/sessions/{session_id}/recording")
    assert resp.status_code == 404


def test_recording_unknown_session_404(tmp_path):
    client = make_app(tmp_path)
    resp = client.get(f"/api/sessions/{uuid.uuid4()}/recording")
    assert resp.status_code == 404


def test_recording_ignores_filename_query_param(tmp_path):
    session_id, session_dir = make_session(tmp_path)
    expected = (session_dir / "recording.wav").read_bytes()
    client = make_app(tmp_path)
    resp = client.get(f"/api/sessions/{session_id}/recording?file=session.json")
    assert resp.status_code == 200
    assert resp.content == expected


def test_no_filename_path_route_exists(tmp_path):
    session_id, _ = make_session(tmp_path)
    client = make_app(tmp_path)
    resp = client.get(f"/api/sessions/{session_id}/session.json")
    assert resp.status_code in (404, 405)


# ---------------------------------------------------------------------------
# Traversal / mutation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "traversal",
    ["..", "../..", "%2e%2e%2f", "..%2f..%2fetc", "a%2Fb"],
)
def test_traversal_strings_never_reach_files(tmp_path, traversal):
    client = make_app(tmp_path)
    resp = client.get(f"/api/sessions/{traversal}")
    assert resp.status_code in (400, 404, 422)
    resp2 = client.get(f"/api/sessions/{traversal}/recording")
    assert resp2.status_code in (400, 404, 422)


@pytest.mark.parametrize("method", ["post", "put", "delete", "patch"])
def test_no_mutation_methods(tmp_path, method):
    session_id, _ = make_session(tmp_path)
    client = make_app(tmp_path)
    resp = getattr(client, method)(f"/api/sessions/{session_id}")
    assert resp.status_code == 405
    resp_list = getattr(client, method)("/api/sessions")
    assert resp_list.status_code == 405


# ---------------------------------------------------------------------------
# server.py wiring (import-only smoke check; no app construction, since
# server.py requires provider env vars at import time)
# ---------------------------------------------------------------------------


def test_server_py_includes_session_router():
    server_src = Path(__file__).resolve().parent.parent / "server.py"
    text = server_src.read_text()
    assert "session_api" in text
    assert "include_router" in text


def test_uppercase_uuid_cannot_reach_existing_session(tmp_path):
    session_id = "aaaaaaaa-1111-4111-8111-111111111111"
    session_dir = tmp_path / session_id
    session_dir.mkdir()
    (session_dir / "session.json").write_text(json.dumps({"id": session_id}))
    (session_dir / "recording.wav").write_bytes(b"RIFF")
    client = make_app(tmp_path)
    upper = session_id.upper()
    assert client.get(f"/api/sessions/{upper}").status_code == 400
    assert client.get(f"/api/sessions/{upper}/recording").status_code == 400
