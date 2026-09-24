"""Gate E: independent, deterministic, post-call bot-audio freeze detector.

Reads ONLY the ordinary artifacts a completed session already writes --
``recording.wav`` and ``session.json`` (see server/session.py) -- and
decides whether the assistant's spoken audio disappeared persistently
during an otherwise continuing conversation, while assistant *text*
continued to exist. This is an observation about evidence
("bot_audio_freeze"), not a claim that any particular simulator ran.

DETECTOR INDEPENDENCE (mandatory, see .claude/tasks/006-gate-e-freeze-
detection.md "DETECTOR INDEPENDENCE"): this module must never import,
read or reference ``freeze_gate``/``FreezeGate``, ``config`` (which reads
``FREEZE_AFTER_ASSISTANT_TURNS`` and other env vars), backend logs, or
``TurnTracker`` runtime state. It must produce the same decision if
server/freeze_gate.py were deleted after the recording was written. This
module therefore does NOT import server/session.py or server/config.py --
even though session.py's ``SESSIONS_DIR`` would be convenient, importing
it would transitively import config.py, which is exactly the coupling
this gate forbids. ``_SESSIONS_DIR`` below is a plain, independent
re-derivation of the same path.

Standard library only: ``wave``, ``array``, ``math``, ``json``. No new
dependencies, no ML, no network, and this module never imports any
provider SDK (Deepgram/Gemini/Cartesia) directly or transitively.

See .claude/tasks/006-gate-e-freeze-detection.md for the full spec this
implements ("OBSERVATIONAL DEFINITION", "AUDIO ANALYSIS", "TURN RESPONSE
WINDOWS", "EARLIER AUDIBLE BASELINE", "PERSISTENCE RULE", "CONTINUATION
RULE", "TRAILING-SILENCE RULE", "FREEZE REGION SEMANTICS", "ANALYSIS
SCHEMA").
"""

from __future__ import annotations

import argparse
import array
import json
import math
import os
import sys
import uuid
import wave
from pathlib import Path
from typing import Any

from loguru import logger

# Independent re-derivation of server/config.py's SESSIONS_DIR. Deliberately
# NOT imported from config.py/session.py -- see module docstring.
_SESSIONS_DIR = Path(__file__).resolve().parent.parent / "data" / "sessions"

SESSION_JSON_NAME = "session.json"
RECORDING_WAV_NAME = "recording.wav"
ANALYSIS_JSON_NAME = "analysis.json"

# 16-bit PCM everywhere in this pipeline (matches session.py's
# SAMPLE_WIDTH_BYTES / AudioBufferProcessor's output format).
SAMPLE_WIDTH_BYTES = 2

# -- Audio-analysis constants (documented rationale per constant) -----------

# 20ms frames: short enough to localize a response's audio precisely,
# long enough for a stable RMS estimate; divides 24000 Hz evenly (480
# samples/frame) with no remainder.
FRAME_MS = 20

# Absolute RMS floor (~-47 dBFS for 16-bit PCM) below which a frame is
# silence regardless of the recording's measured noise floor -- protects
# against a pathologically quiet/all-noise recording producing a
# near-zero adaptive threshold.
ABS_MIN_RMS = 150.0

# How far above the recording's own noise floor a frame's RMS must sit
# (+12 dB) before counting as voiced/audible, so ordinary electrical/room
# noise never registers as bot speech.
NOISE_MULTIPLIER = 4.0

# Low percentile of all bot-channel frame RMS values approximates the
# recording's silence/noise floor without being skewed by the (typically
# shorter) speech segments.
NOISE_FLOOR_PERCENTILE = 20

# Ceiling on the measured noise floor before it's multiplied into the
# speech threshold (~-42 dBFS for 16-bit PCM). In this pipeline the bot
# channel is TTS output plus transport idle silence -- there is no
# legitimate "background noise" louder than this. Without this clamp, a
# call where the bot speaks in most frames (a plausible, even typical,
# call shape) pushes the 20th-percentile noise floor up to speech level
# itself, inflating speech_threshold to ~4x real speech and misclassifying
# genuine bot audio -- including the audible baseline turn -- as silent
# (a false negative). Real TTS speech frames measure in the thousands of
# RMS, comfortably above the resulting <=1000 RMS ceiling on the threshold
# (NOISE_FLOOR_MAX_RMS * NOISE_MULTIPLIER), so this never suppresses
# detection of actual speech.
NOISE_FLOOR_MAX_RMS = 250.0

# A response window must contain at least this much voiced audio to be
# classified audible -- tolerates brief clicks/residual energy and
# leading/trailing silence inside the window without misclassifying it.
MIN_VOICED_MS = 200


class AnalysisError(Exception):
    """Raised for a short, safe error code (see ANALYSIS SCHEMA in the task
    spec). Never carries secrets or absolute paths beyond the session dir.
    """

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# ---------------------------------------------------------------------------
# Session-dir / path helpers (no client-supplied paths; UUID-validated only)
# ---------------------------------------------------------------------------


def resolve_session_dir(session_id: str, sessions_dir: Path | str = _SESSIONS_DIR) -> Path:
    """Validate session_id as a UUID and build a path guaranteed to live
    directly under sessions_dir. Mirrors session.py's own validation so the
    detector never opens an arbitrary filesystem path.
    """
    uuid.UUID(session_id)  # raises ValueError for anything that isn't a UUID
    base = Path(sessions_dir).resolve()
    session_dir = (base / session_id).resolve()
    if session_dir.parent != base:
        raise ValueError(f"resolved session dir escapes {base}")
    return session_dir


# ---------------------------------------------------------------------------
# Artifact loading / validation
# ---------------------------------------------------------------------------


def _load_session_json(session_dir: Path) -> dict[str, Any]:
    path = session_dir / SESSION_JSON_NAME
    try:
        with open(path, "r") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        raise AnalysisError("invalid_session_json") from exc

    if not isinstance(data, dict):
        raise AnalysisError("invalid_session_json")
    if "durationMs" not in data or not isinstance(data["durationMs"], int):
        raise AnalysisError("invalid_session_json")
    if "transcript" not in data or not isinstance(data["transcript"], list):
        raise AnalysisError("invalid_session_json")
    for entry in data["transcript"]:
        if (
            not isinstance(entry, dict)
            or entry.get("role") not in ("user", "assistant")
            or not isinstance(entry.get("text"), str)
            or not isinstance(entry.get("timestampMs"), int)
        ):
            raise AnalysisError("invalid_session_json")

    return data


def _validate_recording_block(session_json: dict[str, Any]) -> dict[str, Any]:
    recording = session_json.get("recording")
    if recording is None or not isinstance(recording, dict):
        raise AnalysisError("missing_recording")
    if recording.get("file") != RECORDING_WAV_NAME:
        # Never open a filename the session didn't advertise as its own
        # recording; this also blocks a tampered/arbitrary "file" value.
        raise AnalysisError("missing_recording")

    channels = recording.get("channels")
    sample_rate = recording.get("sampleRate")
    layout = recording.get("channelLayout")
    if not isinstance(channels, int) or channels < 1:
        raise AnalysisError("invalid_channel_layout")
    if not isinstance(sample_rate, int) or sample_rate < 1:
        raise AnalysisError("invalid_channel_layout")
    if (
        not isinstance(layout, list)
        or len(layout) != channels
        or len(set(layout)) != len(layout)
        or "bot" not in layout
    ):
        raise AnalysisError("invalid_channel_layout")

    return recording


def _read_bot_channel(
    session_dir: Path, recording: dict[str, Any]
) -> tuple[array.array, int]:
    """Open recording.wav read-only, validate its metadata against the
    session.json recording block, and return (bot_channel_samples,
    sample_rate). Never mutates recording.wav.
    """
    wav_path = session_dir / RECORDING_WAV_NAME
    if not wav_path.is_file():
        raise AnalysisError("missing_recording")

    try:
        with wave.open(str(wav_path), "rb") as wav_file:
            nchannels = wav_file.getnchannels()
            sample_rate = wav_file.getframerate()
            sampwidth = wav_file.getsampwidth()
            nframes = wav_file.getnframes()
            raw = wav_file.readframes(nframes)
    except (OSError, wave.Error) as exc:
        raise AnalysisError("wav_metadata_mismatch") from exc

    if (
        sampwidth != SAMPLE_WIDTH_BYTES
        or nchannels != recording["channels"]
        or sample_rate != recording["sampleRate"]
    ):
        raise AnalysisError("wav_metadata_mismatch")

    samples = array.array("h")
    try:
        samples.frombytes(raw)
    except ValueError as exc:  # odd byte count -- corrupt/truncated WAV
        raise AnalysisError("wav_metadata_mismatch") from exc
    if sys.byteorder == "big":
        samples.byteswap()

    bot_index = recording["channelLayout"].index("bot")
    bot_channel = samples[bot_index::nchannels]
    return bot_channel, sample_rate


# ---------------------------------------------------------------------------
# RMS framing / adaptive threshold
# ---------------------------------------------------------------------------


def _frame_rms_series(channel: array.array, sample_rate: int) -> tuple[list[float], int]:
    """Non-overlapping FRAME_MS-ms RMS values across the whole channel, plus
    the exact frame length in ms (which is FRAME_MS when sample_rate divides
    evenly; documented as an approximation otherwise)."""
    frame_len = max(1, int(round(sample_rate * FRAME_MS / 1000)))
    n_frames = len(channel) // frame_len
    rms_values: list[float] = []
    for i in range(n_frames):
        chunk = channel[i * frame_len : (i + 1) * frame_len]
        total = 0
        for sample in chunk:
            total += sample * sample
        rms_values.append(math.sqrt(total / len(chunk)))
    return rms_values, frame_len


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = int(round((pct / 100.0) * (len(ordered) - 1)))
    idx = max(0, min(idx, len(ordered) - 1))
    return ordered[idx]


def _speech_threshold(rms_values: list[float]) -> float:
    noise_floor = min(_percentile(rms_values, NOISE_FLOOR_PERCENTILE), NOISE_FLOOR_MAX_RMS)
    return max(ABS_MIN_RMS, noise_floor * NOISE_MULTIPLIER)


def _is_window_audible(
    rms_values: list[float],
    frame_len_ms: int,
    threshold: float,
    start_ms: int,
    end_ms: int,
    duration_ms: int,
) -> bool:
    start_ms = max(0, min(start_ms, duration_ms))
    end_ms = max(0, min(end_ms, duration_ms))
    if end_ms <= start_ms:
        return False
    start_idx = start_ms // frame_len_ms
    end_idx = -(-end_ms // frame_len_ms)  # ceil division
    start_idx = min(start_idx, len(rms_values))
    end_idx = min(end_idx, len(rms_values))
    voiced_frames = sum(1 for r in rms_values[start_idx:end_idx] if r > threshold)
    return voiced_frames * frame_len_ms >= MIN_VOICED_MS


# ---------------------------------------------------------------------------
# Turn-window construction (modern turnId-tagged + legacy fallback)
# ---------------------------------------------------------------------------


def _build_turns(
    transcript: list[dict[str, Any]],
) -> tuple[dict[int, dict[str, Any]], dict[int, dict[str, Any]]]:
    """Return (user_entries, assistant_by_turn), each keyed by an integer
    turn id.

    Modern sessions (Gate C+) tag every entry with a "turnId" key (None for
    the proactive greeting / any response with no pending user turn).
    Legacy Gate B sessions have no "turnId" key at all: each assistant
    entry is paired with the most recent preceding user entry; assistant
    entries with no preceding user entry are the greeting.
    """
    is_modern = any("turnId" in entry for entry in transcript)

    user_entries: dict[int, dict[str, Any]] = {}
    assistant_by_turn: dict[int, dict[str, Any]] = {}

    if is_modern:
        for entry in transcript:
            turn_id = entry.get("turnId")
            if turn_id is None:
                continue  # greeting, or a response with no pending user turn
            if entry["role"] == "user":
                user_entries.setdefault(turn_id, entry)
            elif entry["role"] == "assistant":
                assistant_by_turn[turn_id] = entry
        return user_entries, assistant_by_turn

    next_turn_id = 0
    current_turn_id: int | None = None
    for entry in transcript:
        if entry["role"] == "user":
            next_turn_id += 1
            current_turn_id = next_turn_id
            user_entries[current_turn_id] = entry
        elif entry["role"] == "assistant":
            if current_turn_id is None:
                continue  # before any user entry: the greeting
            assistant_by_turn[current_turn_id] = entry
    return user_entries, assistant_by_turn


def _build_response_windows(
    user_entries: dict[int, dict[str, Any]],
    assistant_by_turn: dict[int, dict[str, Any]],
    duration_ms: int,
) -> list[dict[str, Any]]:
    """One window per user turn, in ascending turn-id order: start = that
    turn's user timestamp, end = the next turn's user timestamp (or
    durationMs for the last turn). `assistant` is that turn's assistant
    entry, or None if the turn never got one (e.g. a provider failure, or a
    fragment merged into a later turn)."""
    sorted_ids = sorted(user_entries)
    windows = []
    for i, turn_id in enumerate(sorted_ids):
        start = user_entries[turn_id]["timestampMs"]
        end = (
            user_entries[sorted_ids[i + 1]]["timestampMs"]
            if i + 1 < len(sorted_ids)
            else duration_ms
        )
        windows.append(
            {
                "turnId": turn_id,
                "start": start,
                "end": end,
                "assistant": assistant_by_turn.get(turn_id),
            }
        )
    return windows


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def analyze_session(session_dir: Path) -> dict[str, Any]:
    """Pure function over the session's persisted files: returns the "freeze"
    sub-object (never mutates recording.wav or session.json). Raises
    AnalysisError on any artifact problem -- callers wrap this as
    {"status": "error", "error": exc.code}.
    """
    session_json = _load_session_json(session_dir)
    recording = _validate_recording_block(session_json)
    bot_channel, sample_rate = _read_bot_channel(session_dir, recording)

    duration_ms = session_json["durationMs"]
    transcript = session_json["transcript"]

    rms_values, frame_len_samples = _frame_rms_series(bot_channel, sample_rate)
    frame_len_ms = round(frame_len_samples / sample_rate * 1000) or FRAME_MS
    threshold = _speech_threshold(rms_values)

    user_entries, assistant_by_turn = _build_turns(transcript)
    windows = _build_response_windows(user_entries, assistant_by_turn, duration_ms)

    responses = [w for w in windows if w["assistant"] is not None]
    if not responses:
        return _negative("no_assistant_responses")

    for r in responses:
        r["audible"] = _is_window_audible(
            rms_values, frame_len_ms, threshold, r["start"], r["end"], duration_ms
        )

    last_audible_idx = None
    for i, r in enumerate(responses):
        if r["audible"]:
            last_audible_idx = i
    if last_audible_idx is None:
        return _negative("no_audible_baseline")

    if last_audible_idx == len(responses) - 1:
        return _negative("no_silent_assistant_response_after_audible")

    onset = responses[last_audible_idx + 1]
    onset_ts = onset["assistant"]["timestampMs"]

    has_continuation = any(entry["timestampMs"] > onset_ts for entry in transcript)
    if not has_continuation:
        return _negative("no_continuation_after_silent_response")

    audible_before = [responses[i]["turnId"] for i in range(0, last_audible_idx + 1) if responses[i]["audible"]]
    silent_from_onset = [r["turnId"] for r in responses[last_audible_idx + 1 :]]
    continuation_turn_ids = sorted(
        {
            entry.get("turnId") if "turnId" in entry else None
            for entry in transcript
            if entry["timestampMs"] > onset_ts
        }
        - {None}
    )
    # Legacy sessions have no turnId key: fall back to re-deriving turn ids
    # from the same window structure for continuation evidence.
    if not continuation_turn_ids:
        window_by_ts = {w["start"]: w["turnId"] for w in windows}
        window_by_ts.update(
            {w["assistant"]["timestampMs"]: w["turnId"] for w in windows if w["assistant"]}
        )
        continuation_turn_ids = sorted(
            {
                window_by_ts[entry["timestampMs"]]
                for entry in transcript
                if entry["timestampMs"] > onset_ts and entry["timestampMs"] in window_by_ts
            }
        )

    return {
        "detected": True,
        "label": "bot_audio_freeze",
        "reason": "persistent_assistant_text_without_bot_audio",
        "startMs": onset_ts,
        "endMs": duration_ms,
        "startTurnId": onset["turnId"],
        "evidence": {
            "audibleAssistantTurnIdsBefore": audible_before,
            "silentAssistantTurnIds": silent_from_onset,
            "continuationTurnIds": continuation_turn_ids,
        },
        "audio": {
            "speechThresholdRms": round(threshold, 1),
            "frameMs": frame_len_ms,
        },
    }


def _negative(detail: str) -> dict[str, Any]:
    return {
        "detected": False,
        "reason": "no_persistent_bot_audio_freeze",
        "detail": detail,
    }


def analyze(session_dir: Path) -> dict[str, Any]:
    """Full envelope: {"version": 1, "status": "ok", "freeze": {...}} or
    {"version": 1, "status": "error", "error": "<code>"}."""
    try:
        freeze = analyze_session(session_dir)
    except AnalysisError as exc:
        return {"version": 1, "status": "error", "error": exc.code}
    return {"version": 1, "status": "ok", "freeze": freeze}


# ---------------------------------------------------------------------------
# Atomic write (mirrors session.py's Session._write_json pattern)
# ---------------------------------------------------------------------------


def write_analysis(session_dir: Path, result: dict[str, Any]) -> None:
    """Write analysis.json atomically (tmp + fsync + replace). Idempotent:
    re-running with identical input bytes produces byte-identical output."""
    json_path = session_dir / ANALYSIS_JSON_NAME
    tmp_path = session_dir / f"{ANALYSIS_JSON_NAME}.tmp"
    try:
        with open(tmp_path, "w") as f:
            json.dump(result, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, json_path)
    except OSError:
        tmp_path.unlink(missing_ok=True)
        raise


def analyze_and_write(session_dir: Path) -> dict[str, Any]:
    result = analyze(session_dir)
    write_analysis(session_dir, result)
    return result


def run_detection_safely(session_dir: Path) -> None:
    """Post-call hook for bot.py: run analysis off the event loop and never
    raise into / corrupt the session, regardless of what goes wrong. Called
    only after session.finalize() has completed successfully -- see
    bot.py's try/finally.
    """
    try:
        result = analyze_and_write(session_dir)
        status = result.get("status")
        if status == "ok":
            detected = result.get("freeze", {}).get("detected")
            logger.info(f"Freeze detector: session {session_dir.name}: detected={detected}")
        else:
            logger.info(
                f"Freeze detector: session {session_dir.name}: "
                f"analysis error={result.get('error')}"
            )
    except Exception:
        logger.exception(f"Freeze detector: analysis failed for session {session_dir.name}")


# ---------------------------------------------------------------------------
# CLI: `uv run python -m freeze_detector <session-uuid>`
# ---------------------------------------------------------------------------


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="freeze_detector",
        description="Re-run the Gate E post-call bot-audio freeze detector for one session.",
    )
    parser.add_argument("session_id", help="Session UUID (must exist under data/sessions/)")
    parser.add_argument(
        "--no-write",
        action="store_true",
        help="Print the analysis without writing analysis.json",
    )
    args = parser.parse_args(argv)

    try:
        session_dir = resolve_session_dir(args.session_id, sessions_dir=_SESSIONS_DIR)
    except ValueError as exc:
        print(json.dumps({"version": 1, "status": "error", "error": "invalid_session_id"}))
        print(str(exc), file=sys.stderr)
        return 1

    result = analyze(session_dir)
    if not args.no_write and result.get("status") == "ok":
        write_analysis(session_dir, result)

    print(json.dumps(result, indent=2))
    return 0 if result.get("status") == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(_main())
