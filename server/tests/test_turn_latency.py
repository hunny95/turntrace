"""Gate C tests: per-turn user-to-bot latency capture and persistence.

All tests are deterministic and offline: no Deepgram/Gemini/Cartesia
services are constructed anywhere in this file (see test_no_provider_services_constructed).

Two layers are tested:
  - TurnTracker (server/turn_tracker.py) in pure isolation: it is
    framework-free, fed plain ints/None, so its turn-correlation state
    machine is tested directly with no Pipecat pipeline at all.
  - SessionClockAnchor (server/bot.py), which taps VADUserStoppedSpeakingFrame
    / UserStartedSpeakingFrame / BotStartedSpeakingFrame downstream of
    transport.output() and feeds TurnTracker: tested by driving a real
    pipecat.processors.audio.audio_buffer_processor pipeline harness via
    pipecat.tests.utils.run_test, exactly as the Gate B recording tests do.

See .claude/tasks/004-gate-c-turn-latency.md for the full spec these tests
implement (REQUIRED AUTOMATED TESTS, numbered 1-11).
"""

from __future__ import annotations

import asyncio
import json

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    Frame,
    InputAudioRawFrame,
    OutputAudioRawFrame,
    UserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.processors.audio.audio_buffer_processor import AudioBufferProcessor
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.tests.utils import run_test

from bot import SessionClockAnchor
from session import Session
from turn_tracker import LatencyResult, TurnTracker

SAMPLE_RATE = 24000


# ---------------------------------------------------------------------------
# 1. Normal turn (TurnTracker, pure state machine)
# ---------------------------------------------------------------------------


def test_normal_turn_exact_latency():
    tracker = TurnTracker()

    tracker.on_vad_stop(4200)
    turn_id = tracker.on_user_turn_finalized(4300)
    assert turn_id == 1

    tracker.on_assistant_turn_started()
    assert tracker.response_turn_id == 1

    result = tracker.on_bot_audio_started(6150)
    assert result == LatencyResult(turn_id=1, user_stop_ms=4200, bot_start_ms=6150)


# ---------------------------------------------------------------------------
# Repair cycle 1: VAD-stop-vs-finalization async ordering (turn id must
# never be lost, and a late VAD stop must attribute correctly).
# ---------------------------------------------------------------------------


def test_finalize_with_no_vad_stop_still_gets_turn_id_no_latency():
    """(a) Turn finalizes before any VAD-stop frame reaches the tap (or one
    never arrives at all). response_turn_id must still be the real turn id
    -- not None/greeting-shaped -- and bot audio must produce no fabricated
    latency.
    """
    tracker = TurnTracker()

    turn_id = tracker.on_user_turn_finalized(1100)  # no on_vad_stop() call at all
    assert turn_id == 1

    tracker.on_assistant_turn_started()
    assert tracker.response_turn_id == 1  # not None: must not look like a greeting

    result = tracker.on_bot_audio_started(1800)
    assert result is None  # no measurable start: never fabricate a latency


def test_late_vad_stop_before_finalization_attaches_and_measures():
    """(b) The VAD-stop frame for a turn arrives at the tap *after*
    on_user_turn_stopped already finalized that turn (on_vad_stop observed
    after on_user_turn_finalized). Because the VAD stop's session time
    precedes the turn's finalized_ms, it is filled in on the pending turn,
    and the eventual bot audio produces the correct exact latency.
    """
    tracker = TurnTracker()

    # Finalized at session ms 1100, before the VAD-stop frame has arrived.
    turn_id = tracker.on_user_turn_finalized(1100)
    tracker.on_assistant_turn_started()
    assert tracker.response_turn_id == turn_id

    # The VAD-stop frame reaches the tap late: its physical stop time
    # (1000ms) precedes finalization (1100ms), so it belongs to this turn.
    tracker.on_vad_stop(1000)

    result = tracker.on_bot_audio_started(1800)
    assert result == LatencyResult(turn_id=turn_id, user_stop_ms=1000, bot_start_ms=1800)


def test_late_vad_stop_after_finalization_is_not_attached():
    """(c) A VAD-stop frame whose session time is *after* the pending turn's
    finalized_ms cannot physically belong to that turn (it's the next
    turn's own VAD stop arriving early relative to something else, or a
    stray blip) -- it must not attach, and the pending turn still gets no
    latency.
    """
    tracker = TurnTracker()

    turn_id = tracker.on_user_turn_finalized(1100)  # no VAD stop yet
    tracker.on_assistant_turn_started()

    # This VAD stop's session time (1200) is AFTER finalized_ms (1100): it
    # cannot be turn_id's physical stop, so it must not fill in `pending`.
    tracker.on_vad_stop(1200)

    result = tracker.on_bot_audio_started(1800)
    assert result is None  # turn_id still has no measurable start

    # And that VAD stop is available to whatever turn finalizes next.
    turn2 = tracker.on_user_turn_finalized(1300)
    tracker.on_assistant_turn_started()
    result2 = tracker.on_bot_audio_started(2000)
    assert result2 == LatencyResult(turn_id=turn2, user_stop_ms=1200, bot_start_ms=2000)


# ---------------------------------------------------------------------------
# 2. Greeting
# ---------------------------------------------------------------------------


def test_greeting_produces_no_latency_and_null_turn_id():
    tracker = TurnTracker()

    # No user turn has happened yet.
    tracker.on_assistant_turn_started()
    assert tracker.response_turn_id is None

    result = tracker.on_bot_audio_started(900)
    assert result is None


# ---------------------------------------------------------------------------
# 3. Failure then success (mandatory), plus the no-ErrorFrame variant
# ---------------------------------------------------------------------------


def test_failed_turn_then_successful_turn_via_llm_error():
    tracker = TurnTracker()

    # Turn 1: finalized, then its LLM request fails.
    tracker.on_vad_stop(1000)
    turn1 = tracker.on_user_turn_finalized(1100)
    assert turn1 == 1
    tracker.on_llm_failed()
    assert tracker.on_bot_audio_started(1500) is None  # no audio for turn 1 anyway

    # Turn 2: succeeds.
    tracker.on_vad_stop(3000)
    turn2 = tracker.on_user_turn_finalized(3100)
    assert turn2 == 2
    tracker.on_assistant_turn_started()
    assert tracker.response_turn_id == 2
    result = tracker.on_bot_audio_started(3800)
    assert result == LatencyResult(turn_id=2, user_stop_ms=3000, bot_start_ms=3800)


def test_superseded_without_error_frame_only_new_turn_supersedes():
    """Variant: no ErrorFrame at all -- only the new user turn supersedes."""
    tracker = TurnTracker()

    tracker.on_vad_stop(1000)
    tracker.on_user_turn_finalized(1100)  # turn 1, never gets an ErrorFrame

    # User starts speaking again before turn 1 got any audio.
    tracker.on_user_turn_started()

    tracker.on_vad_stop(3000)
    turn2 = tracker.on_user_turn_finalized(3100)
    tracker.on_assistant_turn_started()
    result = tracker.on_bot_audio_started(3900)
    assert result == LatencyResult(turn_id=turn2, user_stop_ms=3000, bot_start_ms=3900)


# ---------------------------------------------------------------------------
# 4. Superseded turn
# ---------------------------------------------------------------------------


def test_superseded_turn_only_b_is_measured():
    tracker = TurnTracker()

    tracker.on_vad_stop(1000)
    turn_a = tracker.on_user_turn_finalized(1100)

    tracker.on_user_turn_started()  # B interrupts A: A abandoned

    tracker.on_vad_stop(5000)
    turn_b = tracker.on_user_turn_finalized(5100)
    tracker.on_assistant_turn_started()
    assert tracker.response_turn_id == turn_b

    result = tracker.on_bot_audio_started(5700)
    assert result.turn_id == turn_b
    assert result.turn_id != turn_a


# ---------------------------------------------------------------------------
# 5. Duplicate audio
# ---------------------------------------------------------------------------


def test_duplicate_bot_audio_produces_one_record_first_frame_time():
    tracker = TurnTracker()

    tracker.on_vad_stop(1000)
    tracker.on_user_turn_finalized(1100)
    tracker.on_assistant_turn_started()

    first = tracker.on_bot_audio_started(1800)
    assert first is not None
    assert first.bot_start_ms == 1800

    # A second BotStartedSpeakingFrame / more audio frames in the same turn.
    second = tracker.on_bot_audio_started(1850)
    assert second is None


# ---------------------------------------------------------------------------
# 6. Endpointing: wall-clock -> session-clock VAD conversion + full chain
# ---------------------------------------------------------------------------


def test_endpointing_latency_measured_from_physical_stop_not_finalization(tmp_path, monkeypatch):
    """Physical VAD stop at T, turn finalized at T+3s (Smart Turn fallback),
    audio at T+4.5s -> latencyMs == 4500. Exercises the real
    SessionClockAnchor._vad_stop_session_ms conversion and Session.rel_ms()
    directly (no pipeline linking needed: _vad_stop_session_ms is a plain
    method, and the pipeline-frame-flow path is covered separately by
    test_session_clock_anchor_passes_every_frame_unmodified below).
    """
    session = Session(sessions_dir=tmp_path)
    tracker = TurnTracker()
    anchor = SessionClockAnchor(session, tracker)

    # Anchor t0 at monotonic T=100.0 (first audio frame reaches the recorder).
    monkeypatch.setattr("session.time.monotonic", lambda: 100.0)
    session.mark_first_audio()
    assert session._t0 == 100.0

    # Physical VAD stop occurs at wall-clock W, which is exactly the instant
    # session clock t0 (session time 0). The VAD determination fires
    # stop_secs=0.6s later; we *observe*/convert the frame (session.rel_ms()
    # computed at that moment) a further 0.1s after that -- session
    # monotonic 100.7 (700ms after t0).
    wall_t0 = 1_000_000.0
    stop_secs = 0.6
    vad_frame = VADUserStoppedSpeakingFrame(stop_secs=stop_secs, timestamp=wall_t0 + stop_secs)

    monkeypatch.setattr("session.time.monotonic", lambda: 100.7)
    monkeypatch.setattr("bot.time.time", lambda: wall_t0 + stop_secs + 0.1)
    user_stop_ms = anchor._vad_stop_session_ms(vad_frame)
    assert user_stop_ms == 0  # physical stop coincides with t0
    tracker.on_vad_stop(user_stop_ms)

    # Turn finalized 3s later (Smart Turn fallback): session monotonic 103.0.
    monkeypatch.setattr("session.time.monotonic", lambda: 103.0)
    turn_id = tracker.on_user_turn_finalized(session.rel_ms())
    tracker.on_assistant_turn_started()

    # Bot audio at T+4.5s: session monotonic 104.5. It is NOT measured from
    # finalization (T+3s) -- latencyMs must be 4500, not 1500.
    monkeypatch.setattr("session.time.monotonic", lambda: 104.5)
    bot_start_ms = session.rel_ms()
    result = tracker.on_bot_audio_started(bot_start_ms)
    assert result is not None
    session.add_latency(result.turn_id, result.user_stop_ms, result.bot_start_ms)

    assert bot_start_ms == 4500
    entry = session.latencies[0]
    assert entry.turn_id == turn_id
    assert entry.user_stop_ms == 0
    assert entry.bot_start_ms == 4500
    assert entry.to_json()["latencyMs"] == 4500


# ---------------------------------------------------------------------------
# 7. Persisted JSON
# ---------------------------------------------------------------------------


def test_persisted_json_has_turn_ids_and_latency_invariants(tmp_path):
    asyncio.run(_persisted_json_has_turn_ids_and_latency_invariants(tmp_path))


async def _persisted_json_has_turn_ids_and_latency_invariants(tmp_path):
    session = Session(sessions_dir=tmp_path)

    session.add_assistant_turn("Hi! How can I help?", None)  # greeting
    session.add_user_turn("What's the weather?", 1)
    session.add_assistant_turn("Sunny today.", 1)
    session.add_latency(1, user_stop_ms=100, bot_start_ms=350)

    await session.finalize()

    payload = json.loads((session.dir / "session.json").read_text())

    assert payload["transcript"][0]["turnId"] is None
    assert payload["transcript"][1]["turnId"] == 1
    assert payload["transcript"][2]["turnId"] == 1

    assert payload["latencies"] == [
        {"turnId": 1, "userStopMs": 100, "botStartMs": 350, "latencyMs": 250}
    ]
    # userStopMs <= botStartMs holds by construction (add_latency stores the
    # values as given; the 0 <= userStopMs <= botStartMs <= durationMs
    # invariant over real session-clock values is exercised end to end by
    # test_endpointing_latency_measured_from_physical_stop_not_finalization).
    for entry in payload["latencies"]:
        assert 0 <= entry["userStopMs"] <= entry["botStartMs"]


def test_latencies_key_present_and_empty_when_no_latencies(tmp_path):
    asyncio.run(_latencies_key_present_and_empty(tmp_path))


async def _latencies_key_present_and_empty(tmp_path):
    session = Session(sessions_dir=tmp_path)
    await session.finalize()
    payload = json.loads((session.dir / "session.json").read_text())
    assert payload["latencies"] == []


# ---------------------------------------------------------------------------
# 8. Recording regression: post-output processor still passes every frame
#    unmodified, through a real pipeline harness.
# ---------------------------------------------------------------------------


def test_session_clock_anchor_passes_every_frame_unmodified(tmp_path):
    asyncio.run(_session_clock_anchor_passes_every_frame_unmodified(tmp_path))


async def _session_clock_anchor_passes_every_frame_unmodified(tmp_path):
    session = Session(sessions_dir=tmp_path)
    tracker = TurnTracker()
    anchor = SessionClockAnchor(session, tracker)
    audio_buffer = AudioBufferProcessor(
        sample_rate=SAMPLE_RATE, num_channels=2, auto_start_recording=True
    )

    stage = Pipeline([anchor, audio_buffer])

    frames_to_send: list[Frame] = [
        InputAudioRawFrame(audio=b"\x01\x00" * 10, sample_rate=SAMPLE_RATE, num_channels=1),
        VADUserStoppedSpeakingFrame(stop_secs=0.5),
        UserStartedSpeakingFrame(),
        BotStartedSpeakingFrame(),
        OutputAudioRawFrame(audio=b"\x02\x00" * 10, sample_rate=SAMPLE_RATE, num_channels=1),
    ]

    down_frames, _ = await run_test(stage, frames_to_send=frames_to_send, ignore_start=True)

    # Every non-system-internal frame we sent shows up downstream, unmodified
    # in type and count -- the anchor only observes, never drops or mutates.
    assert sum(isinstance(f, InputAudioRawFrame) for f in down_frames) == 1
    assert sum(isinstance(f, VADUserStoppedSpeakingFrame) for f in down_frames) == 1
    assert sum(isinstance(f, UserStartedSpeakingFrame) for f in down_frames) == 1
    assert sum(isinstance(f, BotStartedSpeakingFrame) for f in down_frames) == 1
    assert sum(isinstance(f, OutputAudioRawFrame) for f in down_frames) == 1

    # And the Gate C tap still fired: a VAD stop + a bot-started + a bot audio
    # frame with no pending user turn is the greeting shape -> no latency,
    # but mark_first_audio and the bot-audio arming both ran without error.
    assert session.latencies == []


# ---------------------------------------------------------------------------
# 9. Failed-turn transcript
# ---------------------------------------------------------------------------


def test_failed_turn_keeps_transcript_entry_with_turn_id_no_latency(tmp_path):
    asyncio.run(_failed_turn_keeps_transcript_entry(tmp_path))


async def _failed_turn_keeps_transcript_entry(tmp_path):
    session = Session(sessions_dir=tmp_path)
    tracker = TurnTracker()

    tracker.on_vad_stop(1000)
    turn_id = tracker.on_user_turn_finalized(1100)
    session.add_user_turn("what's the weather", turn_id)

    tracker.on_llm_failed()  # LLM ErrorFrame: no assistant entry ever added

    await session.finalize()
    payload = json.loads((session.dir / "session.json").read_text())

    assert payload["transcript"] == [
        {
            "turnId": 1,
            "role": "user",
            "text": "what's the weather",
            "timestampMs": payload["transcript"][0]["timestampMs"],
        }
    ]
    assert payload["latencies"] == []


# ---------------------------------------------------------------------------
# 10. No external APIs
# ---------------------------------------------------------------------------


def test_no_provider_services_constructed():
    """This test file constructs no Deepgram/Gemini/Cartesia service objects.
    Parses the AST (rather than substring search) so the forbidden-names
    tuple below doesn't trip over itself, and guards against a future edit
    silently adding a real provider call.
    """
    import ast
    import pathlib

    tree = ast.parse(pathlib.Path(__file__).read_text())
    called_names = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    forbidden = {"DeepgramSTTService", "GoogleLLMService", "CartesiaTTSService"}
    assert not (called_names & forbidden)


# ---------------------------------------------------------------------------
# 11. Gate D precursor
# ---------------------------------------------------------------------------


def test_gate_d_precursor_no_audio_turn_has_no_latency_but_has_turn_id():
    """Assistant response start + text for turn N, no bot audio; turn N+1
    has audio. N has no latency; N's assistant entry carries turnId N.
    """
    tracker = TurnTracker()

    tracker.on_vad_stop(1000)
    turn_n = tracker.on_user_turn_finalized(1100)
    tracker.on_assistant_turn_started()
    assert tracker.response_turn_id == turn_n  # assistant entry for N -> turnId N

    # No bot audio ever arrives for N (Gate D freeze). Next user turn starts.
    tracker.on_user_turn_started()  # abandons N's pending measurement

    tracker.on_vad_stop(9000)
    turn_n1 = tracker.on_user_turn_finalized(9100)
    tracker.on_assistant_turn_started()
    assert tracker.response_turn_id == turn_n1

    result = tracker.on_bot_audio_started(9800)
    assert result == LatencyResult(turn_id=turn_n1, user_stop_ms=9000, bot_start_ms=9800)
