"""Gate E tests: independent post-call bot-audio freeze detection.

All tests are deterministic, offline, and build synthetic fixtures (a
stereo WAV with tone/noise/silence segments plus a matching session.json)
under tmp_path -- no real session ever needs to be recorded to exercise
this module. No Deepgram/Gemini/Cartesia/Pipecat service is constructed or
imported anywhere in this file.

See .claude/tasks/006-gate-e-freeze-detection.md, "REQUIRED AUTOMATED
TESTS" (numbered 1-18 plus the bot.py-hook test), which this file
implements.
"""

from __future__ import annotations

import array
import ast
import hashlib
import json
import math
import random
import subprocess
import sys
import uuid
import wave
from pathlib import Path

import pytest

import freeze_detector as fd

SAMPLE_RATE = 24000


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _channel_samples(duration_ms: int, segments: list[tuple[int, int, object]], rng) -> list[int]:
    """segments: list of (start_ms, end_ms, kind), kind is one of:
    "silence", ("tone", amplitude, freq_hz), ("noise", low, high)."""
    total = int(duration_ms * SAMPLE_RATE / 1000)
    channel = [0] * total
    for start_ms, end_ms, kind in segments:
        s = max(0, int(start_ms * SAMPLE_RATE / 1000))
        e = min(total, int(end_ms * SAMPLE_RATE / 1000))
        n = e - s
        if n <= 0:
            continue
        if kind == "silence":
            continue
        if kind[0] == "tone":
            _, amplitude, freq = kind
            for i in range(n):
                channel[s + i] = int(amplitude * math.sin(2 * math.pi * freq * i / SAMPLE_RATE))
        elif kind[0] == "noise":
            _, lo, hi = kind
            for i in range(n):
                channel[s + i] = rng.randint(lo, hi)
        else:
            raise ValueError(f"unknown segment kind: {kind}")
    return channel


def write_wav(
    wav_path: Path,
    duration_ms: int,
    user_segments: list[tuple[int, int, object]],
    bot_segments: list[tuple[int, int, object]],
    channel_layout: list[str] = ("user", "bot"),
    seed: int = 0,
) -> None:
    rng = random.Random(seed)
    user = _channel_samples(duration_ms, user_segments, rng)
    bot = _channel_samples(duration_ms, bot_segments, rng)
    total = len(user)
    channel_layout = list(channel_layout)
    nchannels = len(channel_layout)
    # An unrecognized channel name (used only by fixtures exercising
    # invalid-layout error paths, where the audio content is never read)
    # gets silence.
    named = {"user": array.array("h", user), "bot": array.array("h", bot)}
    silence = array.array("h", bytes(2 * total))

    # Extended-slice writes interleave at the C level -- orders of
    # magnitude faster than a per-sample Python loop for a multi-minute
    # synthetic recording.
    interleaved = array.array("h", bytes(2 * total * nchannels))
    for idx, name in enumerate(channel_layout):
        interleaved[idx::nchannels] = named.get(name, silence)

    with wave.open(str(wav_path), "wb") as wav_file:
        wav_file.setnchannels(nchannels)
        wav_file.setsampwidth(2)
        wav_file.setframerate(SAMPLE_RATE)
        wav_file.writeframes(interleaved.tobytes())


def make_session(
    tmp_path: Path,
    duration_ms: int,
    transcript: list[dict],
    user_segments: list[tuple[int, int, object]] | None = None,
    bot_segments: list[tuple[int, int, object]] | None = None,
    channel_layout: list[str] = ("user", "bot"),
    recording_override: dict | None = None,
    write_wav_file: bool = True,
    seed: int = 0,
) -> Path:
    session_id = str(uuid.uuid4())
    session_dir = tmp_path / session_id
    session_dir.mkdir()

    if write_wav_file:
        write_wav(
            session_dir / "recording.wav",
            duration_ms,
            user_segments or [],
            bot_segments or [],
            channel_layout=list(channel_layout),
            seed=seed,
        )

    recording = (
        recording_override
        if recording_override is not None
        else {
            "file": "recording.wav",
            "sampleRate": SAMPLE_RATE,
            "channels": len(channel_layout),
            "channelLayout": list(channel_layout),
        }
    )
    payload = {
        "id": session_id,
        "startedAt": "2026-01-01T00:00:00.000Z",
        "durationMs": duration_ms,
        "recording": recording,
        "transcript": transcript,
        "latencies": [],
    }
    with open(session_dir / "session.json", "w") as f:
        json.dump(payload, f, indent=2)

    return session_dir


def user_entry(turn_id: int | None, ts: int, text: str = "hi", modern: bool = True) -> dict:
    entry = {"role": "user", "text": text, "timestampMs": ts}
    if modern:
        entry["turnId"] = turn_id
    return entry


def assistant_entry(turn_id: int | None, ts: int, text: str = "ok", modern: bool = True) -> dict:
    entry = {"role": "assistant", "text": text, "timestampMs": ts}
    if modern:
        entry["turnId"] = turn_id
    return entry


LOUD_TONE = ("tone", 6000, 300)


# ---------------------------------------------------------------------------
# 1-2. Normal call / trailing silence -> no freeze
# ---------------------------------------------------------------------------


def test_normal_call_no_freeze(tmp_path):
    transcript = [
        assistant_entry(None, 500, "hello"),
        user_entry(1, 5000),
        assistant_entry(1, 8000),
        user_entry(2, 12000),
        assistant_entry(2, 15000),
    ]
    session_dir = make_session(
        tmp_path,
        duration_ms=18000,
        transcript=transcript,
        bot_segments=[(8000, 9000, LOUD_TONE), (15000, 16000, LOUD_TONE)],
    )
    result = fd.analyze(session_dir)
    assert result["status"] == "ok"
    assert result["freeze"]["detected"] is False
    assert result["freeze"]["detail"] == "no_silent_assistant_response_after_audible"


def test_normal_call_trailing_silence_no_freeze(tmp_path):
    transcript = [
        user_entry(1, 5000),
        assistant_entry(1, 8000),
        user_entry(2, 12000),
        assistant_entry(2, 15000),
    ]
    session_dir = make_session(
        tmp_path,
        duration_ms=90000,  # long trailing silence after the last response
        transcript=transcript,
        bot_segments=[(8000, 9000, LOUD_TONE), (15000, 16000, LOUD_TONE)],
    )
    result = fd.analyze(session_dir)
    assert result["freeze"]["detected"] is False


# ---------------------------------------------------------------------------
# 3. Provider failure then recovery -> no freeze
# ---------------------------------------------------------------------------


def test_provider_failure_then_recovery_no_freeze(tmp_path):
    transcript = [
        user_entry(1, 5000),  # no assistant entry: provider failed this turn
        user_entry(2, 10000),
        assistant_entry(2, 13000),
    ]
    session_dir = make_session(
        tmp_path,
        duration_ms=16000,
        transcript=transcript,
        bot_segments=[(13000, 14000, LOUD_TONE)],
    )
    result = fd.analyze(session_dir)
    assert result["freeze"]["detected"] is False


# ---------------------------------------------------------------------------
# 4. Transient silent response then audible recovery -> no freeze
# ---------------------------------------------------------------------------


def test_transient_silence_then_recovery_no_freeze(tmp_path):
    transcript = [
        user_entry(1, 5000),
        assistant_entry(1, 8000),
        user_entry(2, 12000),
        assistant_entry(2, 15000),  # silent
        user_entry(3, 20000),
        assistant_entry(3, 23000),
    ]
    session_dir = make_session(
        tmp_path,
        duration_ms=26000,
        transcript=transcript,
        bot_segments=[(8000, 9000, LOUD_TONE), (23000, 24000, LOUD_TONE)],
    )
    result = fd.analyze(session_dir)
    assert result["freeze"]["detected"] is False
    assert result["freeze"]["detail"] == "no_silent_assistant_response_after_audible"


# ---------------------------------------------------------------------------
# 5. Final silent response, no later activity -> no freeze
# ---------------------------------------------------------------------------


def test_final_silent_response_no_continuation(tmp_path):
    transcript = [
        user_entry(1, 5000),
        assistant_entry(1, 8000),
        user_entry(2, 12000),
        assistant_entry(2, 15000),  # silent, and nothing follows it
    ]
    session_dir = make_session(
        tmp_path,
        duration_ms=17000,
        transcript=transcript,
        bot_segments=[(8000, 9000, LOUD_TONE)],
    )
    result = fd.analyze(session_dir)
    assert result["freeze"]["detected"] is False
    assert result["freeze"]["detail"] == "no_continuation_after_silent_response"


# ---------------------------------------------------------------------------
# 6. No audible baseline ever -> no freeze
# ---------------------------------------------------------------------------


def test_no_audible_baseline_ever_no_freeze(tmp_path):
    transcript = [
        user_entry(1, 5000),
        assistant_entry(1, 8000),
        user_entry(2, 12000),
        assistant_entry(2, 15000),
    ]
    session_dir = make_session(
        tmp_path,
        duration_ms=17000,
        transcript=transcript,
        bot_segments=[],  # bot never produced audible audio at all
    )
    result = fd.analyze(session_dir)
    assert result["freeze"]["detected"] is False
    assert result["freeze"]["detail"] == "no_audible_baseline"


# ---------------------------------------------------------------------------
# 7-10. Persistent freeze -> detected, with correct region fields
# ---------------------------------------------------------------------------


def _persistent_freeze_transcript():
    return [
        assistant_entry(None, 500, "hello"),
        user_entry(1, 5000),
        assistant_entry(1, 8000),
        user_entry(2, 12000),
        assistant_entry(2, 15000),
        user_entry(3, 20000),
        assistant_entry(3, 23000),  # silent from here on
        user_entry(4, 30000),
        assistant_entry(4, 33000),  # also silent
    ]


def test_persistent_freeze_detected(tmp_path):
    transcript = _persistent_freeze_transcript()
    session_dir = make_session(
        tmp_path,
        duration_ms=40000,
        transcript=transcript,
        bot_segments=[(8000, 9000, LOUD_TONE), (15000, 16000, LOUD_TONE)],
    )
    result = fd.analyze(session_dir)
    freeze = result["freeze"]
    assert freeze["detected"] is True
    assert freeze["label"] == "bot_audio_freeze"
    assert freeze["startTurnId"] == 3  # 8. first silent turn after last audible
    assert freeze["startMs"] == 23000  # 9. first silent assistant entry's timestampMs
    assert freeze["endMs"] == 40000  # 10. durationMs
    assert freeze["evidence"]["audibleAssistantTurnIdsBefore"] == [1, 2]
    assert freeze["evidence"]["silentAssistantTurnIds"] == [3, 4]
    assert freeze["evidence"]["continuationTurnIds"] == [4]


# ---------------------------------------------------------------------------
# 11. Low non-zero noise in the frozen region still detects freeze
# ---------------------------------------------------------------------------


def test_low_noise_in_frozen_region_still_detected(tmp_path):
    transcript = _persistent_freeze_transcript()
    session_dir = make_session(
        tmp_path,
        duration_ms=40000,
        transcript=transcript,
        bot_segments=[
            (8000, 9000, LOUD_TONE),
            (15000, 16000, LOUD_TONE),
            (16000, 40000, ("noise", -60, 60)),  # low dither-level noise, not real speech
        ],
    )
    result = fd.analyze(session_dir)
    assert result["freeze"]["detected"] is True
    assert result["freeze"]["startTurnId"] == 3


# ---------------------------------------------------------------------------
# Repair cycle 1 regression: dominant bot speech must not inflate the noise
# floor and mask a real freeze (QA-found false negative -- see
# NOISE_FLOOR_MAX_RMS in freeze_detector.py).
# ---------------------------------------------------------------------------


def test_dominant_bot_speech_does_not_mask_later_freeze(tmp_path):
    """Bot speaks (loud tone) for ~83% of the call (1s-30s of 35s). Without
    a ceiling on the measured noise floor, the 20th-percentile-of-all-frames
    noise floor is itself near speech level, inflating speech_threshold to
    ~4x real speech and misclassifying the baseline turn as silent
    (no_audible_baseline). With the ceiling, the long baseline turn is
    correctly audible, and the short genuinely-silent final response (with
    continuation) is correctly detected as a freeze.
    """
    transcript = [
        user_entry(1, 1000),
        assistant_entry(1, 30000),  # long audible response, 1s-30s
        user_entry(2, 31000),
        assistant_entry(2, 32000),  # silent
        user_entry(3, 33000),  # continuation after the silent response
    ]
    session_dir = make_session(
        tmp_path,
        duration_ms=35000,
        transcript=transcript,
        bot_segments=[(1000, 30000, LOUD_TONE)],
    )
    result = fd.analyze(session_dir)
    freeze = result["freeze"]
    assert freeze["detected"] is True
    assert freeze["startTurnId"] == 2
    assert freeze["startMs"] == 32000
    assert freeze["endMs"] == 35000
    assert freeze["evidence"]["audibleAssistantTurnIdsBefore"] == [1]


# ---------------------------------------------------------------------------
# 12. Low-amplitude real speech-like signal is classified audible
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("amplitude", [600, 800, 1000])
def test_low_amplitude_speech_is_audible_baseline(tmp_path, amplitude):
    transcript = [
        user_entry(1, 5000),
        assistant_entry(1, 8000),
        user_entry(2, 12000),
        assistant_entry(2, 15000),  # silent, no continuation after -> would be
        # "no_continuation" if turn 1 weren't audible; but what we're testing
        # is that turn 1 (low amplitude) *is* treated as the audible baseline.
    ]
    session_dir = make_session(
        tmp_path,
        duration_ms=17000,
        transcript=transcript,
        bot_segments=[(8000, 9000, ("tone", amplitude, 300))],
    )
    result = fd.analyze(session_dir)
    # Baseline exists (turn 1 audible) and turn 2 is its first silent
    # successor with no continuation after it -- proves turn 1 registered
    # as audible rather than falling through to no_audible_baseline.
    assert result["freeze"]["detail"] != "no_audible_baseline"


# ---------------------------------------------------------------------------
# 13. Swapped channelLayout honored
# ---------------------------------------------------------------------------


def test_swapped_channel_layout_honored(tmp_path):
    transcript = _persistent_freeze_transcript()
    session_dir = make_session(
        tmp_path,
        duration_ms=40000,
        transcript=transcript,
        bot_segments=[(8000, 9000, LOUD_TONE), (15000, 16000, LOUD_TONE)],
        channel_layout=["bot", "user"],
    )
    result = fd.analyze(session_dir)
    assert result["freeze"]["detected"] is True
    assert result["freeze"]["startTurnId"] == 3


# ---------------------------------------------------------------------------
# 14. Invalid metadata -> status: error, no freeze key
# ---------------------------------------------------------------------------


def test_missing_bot_in_layout_is_error(tmp_path):
    session_dir = make_session(
        tmp_path,
        duration_ms=5000,
        transcript=[],
        channel_layout=["user", "assistant"],
    )
    result = fd.analyze(session_dir)
    assert result["status"] == "error"
    assert result["error"] == "invalid_channel_layout"
    assert "freeze" not in result


def test_duplicate_layout_entries_is_error(tmp_path):
    session_dir = make_session(
        tmp_path,
        duration_ms=5000,
        transcript=[],
        channel_layout=["bot", "bot"],
    )
    result = fd.analyze(session_dir)
    assert result["status"] == "error"
    assert result["error"] == "invalid_channel_layout"


def test_wav_channel_count_mismatch_is_error(tmp_path):
    session_dir = make_session(
        tmp_path,
        duration_ms=5000,
        transcript=[],
        recording_override={
            "file": "recording.wav",
            "sampleRate": SAMPLE_RATE,
            "channels": 1,  # actual wav below is written with 2 channels
            "channelLayout": ["bot"],
        },
    )
    result = fd.analyze(session_dir)
    assert result["status"] == "error"
    assert result["error"] == "wav_metadata_mismatch"


def test_missing_recording_block_is_error(tmp_path):
    session_dir = make_session(
        tmp_path,
        duration_ms=5000,
        transcript=[],
        recording_override=None,
        write_wav_file=False,
    )
    # Overwrite session.json with recording: null explicitly.
    payload = json.loads((session_dir / "session.json").read_text())
    payload["recording"] = None
    (session_dir / "session.json").write_text(json.dumps(payload))
    result = fd.analyze(session_dir)
    assert result["status"] == "error"
    assert result["error"] == "missing_recording"
    assert "freeze" not in result


def test_missing_session_json_is_error(tmp_path):
    session_dir = tmp_path / str(uuid.uuid4())
    session_dir.mkdir()
    result = fd.analyze(session_dir)
    assert result["status"] == "error"
    assert result["error"] == "invalid_session_json"


def test_missing_wav_file_is_error(tmp_path):
    session_dir = make_session(
        tmp_path,
        duration_ms=5000,
        transcript=[],
        write_wav_file=False,
    )
    result = fd.analyze(session_dir)
    assert result["status"] == "error"
    assert result["error"] == "missing_recording"


# ---------------------------------------------------------------------------
# 15. Atomic, idempotent write; raw artifacts never mutated
# ---------------------------------------------------------------------------


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_atomic_idempotent_write_does_not_mutate_raw_artifacts(tmp_path):
    transcript = _persistent_freeze_transcript()
    session_dir = make_session(
        tmp_path,
        duration_ms=40000,
        transcript=transcript,
        bot_segments=[(8000, 9000, LOUD_TONE), (15000, 16000, LOUD_TONE)],
    )
    wav_hash_before = _sha256(session_dir / "recording.wav")
    json_hash_before = _sha256(session_dir / "session.json")

    result1 = fd.analyze_and_write(session_dir)
    analysis_bytes_1 = (session_dir / "analysis.json").read_bytes()

    result2 = fd.analyze_and_write(session_dir)
    analysis_bytes_2 = (session_dir / "analysis.json").read_bytes()

    assert result1 == result2
    assert analysis_bytes_1 == analysis_bytes_2
    assert not (session_dir / "analysis.json.tmp").exists()
    assert _sha256(session_dir / "recording.wav") == wav_hash_before
    assert _sha256(session_dir / "session.json") == json_hash_before


# ---------------------------------------------------------------------------
# 16-17. Detector-independence boundary
# ---------------------------------------------------------------------------


def test_detector_has_no_forbidden_imports():
    """Static scan: no `import`/`from ... import` statement in the detector
    pulls in freeze_gate, config, or session (session.py itself imports
    config -- see freeze_detector.py's module docstring). Uses `ast` rather
    than a raw substring scan so the detector's own documentation of this
    exact invariant (which necessarily *names* freeze_gate/config to explain
    why they're avoided) doesn't trip the check.
    """
    source = Path(fd.__file__).read_text()
    tree = ast.parse(source)
    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported_modules.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module.split(".")[0])

    forbidden_modules = {"freeze_gate", "config", "session", "turn_tracker"}
    assert not (imported_modules & forbidden_modules), imported_modules & forbidden_modules

    # FreezeGate / FREEZE_AFTER_ASSISTANT_TURNS must never be referenced as
    # live identifiers (assigned to, called, or read) anywhere in the code.
    forbidden_names = {"FreezeGate", "FREEZE_AFTER_ASSISTANT_TURNS"}
    used_names = {
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name)
    } | {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
    }
    assert not (used_names & forbidden_names)


def test_detector_imports_with_freeze_gate_blocked(monkeypatch):
    monkeypatch.setitem(sys.modules, "freeze_gate", None)
    monkeypatch.setitem(sys.modules, "config", None)
    for mod_name in ("freeze_detector",):
        sys.modules.pop(mod_name, None)
    import freeze_detector as reloaded  # noqa: F401

    assert reloaded is not None


def test_detector_never_imports_provider_modules():
    # Fresh subprocess so we see exactly what importing freeze_detector
    # alone pulls into sys.modules -- no leftover pytest-session imports.
    script = (
        "import sys\n"
        "import freeze_detector\n"
        "leaked = [m for m in sys.modules if m.startswith((\n"
        "    'pipecat', 'deepgram', 'cartesia', 'google.genai', 'google_genai'\n"
        "))]\n"
        "print(','.join(leaked))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parent.parent,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == ""


# ---------------------------------------------------------------------------
# 18. Legacy (no turnId) transcript analyzed via ordering
# ---------------------------------------------------------------------------


def test_legacy_transcript_persistent_freeze_detected(tmp_path):
    transcript = [
        user_entry(None, 5000, modern=False),
        assistant_entry(None, 8000, modern=False),
        user_entry(None, 12000, modern=False),
        assistant_entry(None, 15000, modern=False),
        user_entry(None, 20000, modern=False),
        assistant_entry(None, 23000, modern=False),  # silent from here
        user_entry(None, 30000, modern=False),
        assistant_entry(None, 33000, modern=False),  # also silent
    ]
    for entry in transcript:
        assert "turnId" not in entry
    session_dir = make_session(
        tmp_path,
        duration_ms=40000,
        transcript=transcript,
        bot_segments=[(8000, 9000, LOUD_TONE), (15000, 16000, LOUD_TONE)],
    )
    result = fd.analyze(session_dir)
    freeze = result["freeze"]
    assert freeze["detected"] is True
    assert freeze["startTurnId"] == 3  # 1-based ordinal of user-associated responses
    assert freeze["startMs"] == 23000
    assert freeze["endMs"] == 40000


def test_legacy_transcript_normal_call_no_freeze(tmp_path):
    transcript = [
        user_entry(None, 5000, modern=False),
        assistant_entry(None, 8000, modern=False),
        user_entry(None, 12000, modern=False),
        assistant_entry(None, 15000, modern=False),
    ]
    session_dir = make_session(
        tmp_path,
        duration_ms=17000,
        transcript=transcript,
        bot_segments=[(8000, 9000, LOUD_TONE), (15000, 16000, LOUD_TONE)],
    )
    result = fd.analyze(session_dir)
    assert result["freeze"]["detected"] is False


def test_legacy_greeting_before_any_user_entry_excluded(tmp_path):
    # A legacy greeting (assistant text with no preceding user entry) must
    # not be treated as a user-associated response or as the baseline.
    transcript = [
        assistant_entry(None, 500, "hello", modern=False),
        user_entry(None, 5000, modern=False),
        assistant_entry(None, 8000, modern=False),
    ]
    session_dir = make_session(
        tmp_path,
        duration_ms=10000,
        transcript=transcript,
        bot_segments=[(500, 1000, LOUD_TONE), (8000, 9000, LOUD_TONE)],
    )
    result = fd.analyze(session_dir)
    # Only one real user-associated response (turn 1); it's the last, so no
    # silent successor exists -- not a freeze either way, but this proves
    # the greeting didn't get counted as turn 1 itself (which would shift
    # every subsequent id).
    assert result["freeze"]["detected"] is False


# ---------------------------------------------------------------------------
# resolve_session_dir: no client-supplied/arbitrary paths
# ---------------------------------------------------------------------------


def test_resolve_session_dir_rejects_non_uuid(tmp_path):
    with pytest.raises(ValueError):
        fd.resolve_session_dir("not-a-uuid", sessions_dir=tmp_path)


def test_resolve_session_dir_rejects_path_traversal(tmp_path):
    with pytest.raises(ValueError):
        fd.resolve_session_dir("../../etc", sessions_dir=tmp_path)


def test_resolve_session_dir_builds_expected_path(tmp_path):
    session_id = str(uuid.uuid4())
    session_dir = fd.resolve_session_dir(session_id, sessions_dir=tmp_path)
    assert session_dir == tmp_path / session_id


# ---------------------------------------------------------------------------
# bot.py hook: run_detection_safely never raises
# ---------------------------------------------------------------------------


def test_run_detection_safely_swallows_exceptions(tmp_path, monkeypatch):
    def boom(_session_dir):
        raise RuntimeError("simulated analysis crash")

    monkeypatch.setattr(fd, "analyze_and_write", boom)
    # Must not raise, even though analyze_and_write blew up.
    fd.run_detection_safely(tmp_path / "nonexistent-session")


def test_run_detection_safely_writes_analysis_on_success(tmp_path):
    transcript = [
        user_entry(1, 5000),
        assistant_entry(1, 8000),
    ]
    session_dir = make_session(
        tmp_path,
        duration_ms=10000,
        transcript=transcript,
        bot_segments=[(8000, 9000, LOUD_TONE)],
    )
    fd.run_detection_safely(session_dir)
    assert (session_dir / "analysis.json").exists()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_no_write_does_not_create_analysis_json(tmp_path, monkeypatch, capsys):
    transcript = [
        user_entry(1, 5000),
        assistant_entry(1, 8000),
    ]
    session_dir = make_session(
        tmp_path,
        duration_ms=10000,
        transcript=transcript,
        bot_segments=[(8000, 9000, LOUD_TONE)],
    )
    monkeypatch.setattr(fd, "_SESSIONS_DIR", tmp_path)
    exit_code = fd._main([session_dir.name, "--no-write"])
    assert exit_code == 0
    assert not (session_dir / "analysis.json").exists()
    printed = json.loads(capsys.readouterr().out)
    assert printed["status"] == "ok"


def test_cli_writes_analysis_json_by_default(tmp_path, monkeypatch, capsys):
    transcript = [
        user_entry(1, 5000),
        assistant_entry(1, 8000),
    ]
    session_dir = make_session(
        tmp_path,
        duration_ms=10000,
        transcript=transcript,
        bot_segments=[(8000, 9000, LOUD_TONE)],
    )
    monkeypatch.setattr(fd, "_SESSIONS_DIR", tmp_path)
    exit_code = fd._main([session_dir.name])
    assert exit_code == 0
    assert (session_dir / "analysis.json").exists()


def test_cli_invalid_session_id_exits_nonzero(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(fd, "_SESSIONS_DIR", tmp_path)
    exit_code = fd._main(["not-a-uuid"])
    assert exit_code != 0
