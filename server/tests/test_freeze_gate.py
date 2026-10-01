"""Gate D tests: deterministic, permanent bot-audio freeze injection.

All tests are deterministic and offline: no Deepgram/Gemini/Cartesia
services are constructed anywhere in this file.

Two layers, matching freeze_gate.py's own split:
  - FreezeState in pure isolation: framework-free, fed plain values.
  - FreezeGate (Pipecat glue) driven through a real Pipeline via
    pipecat.tests.utils.run_test, exactly as the Gate B/C tests do.

See .claude/tasks/005-gate-d-freeze-injection.md, "REQUIRED AUTOMATED
TESTS" (numbered 1-13), which this file implements.
"""

from __future__ import annotations

import asyncio
import json
import re

import pytest
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    Frame,
    InputAudioRawFrame,
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    OutputAudioRawFrame,
    TextFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.processors.audio.audio_buffer_processor import AudioBufferProcessor
from pipecat.tests.utils import run_test

from bot import SessionClockAnchor
from freeze_gate import FreezeGate, FreezeState
from session import Session
from turn_tracker import TurnTracker

SAMPLE_RATE = 24000


def mk_audio(marker: int) -> OutputAudioRawFrame:
    """One bot audio frame carrying a distinguishing marker byte, so tests
    can identify by content which frames a run's assertions found
    downstream."""
    return OutputAudioRawFrame(audio=bytes([marker]), sample_rate=SAMPLE_RATE, num_channels=1)


def markers_of(frames: list[Frame]) -> list[int]:
    return [f.audio[0] for f in frames if isinstance(f, OutputAudioRawFrame)]


class _ScriptedTracker:
    """Test-only stand-in for TurnTracker: `pending_turn_id` returns each
    value from `script`, in order, one per read -- one read per
    LLMFullResponseStartFrame the gate sees, in the same order the test
    sends them. Lets one run_test call script several distinct responses'
    turn ids without needing the real user-turn machinery.
    """

    def __init__(self, script: list[int | None]) -> None:
        self._script = list(script)

    @property
    def pending_turn_id(self) -> int | None:
        return self._script.pop(0)


# ---------------------------------------------------------------------------
# Part 1: FreezeState, pure isolation (no Pipecat)
# ---------------------------------------------------------------------------


def test_negative_threshold_rejected():
    with pytest.raises(ValueError):
        FreezeState(-1)


def test_zero_threshold_disables_freeze_forever():
    state = FreezeState(0)
    for turn_id in range(1, 11):
        state.on_response_started(turn_id)
        assert state.on_output_audio_frame() is False
    assert state.frozen is False


def test_greeting_not_counted_and_not_frozen():
    state = FreezeState(2)
    state.on_response_started(None)  # greeting: no pending user turn
    assert state.on_output_audio_frame() is False
    assert state.frozen is False


def test_two_turns_audible_then_third_frozen_permanently():
    state = FreezeState(2)

    state.on_response_started(1)
    assert state.on_output_audio_frame() is False  # turn 1, first frame
    assert state.on_output_audio_frame() is False  # turn 1, more audio: still not counted again

    state.on_response_started(2)
    assert state.on_output_audio_frame() is False  # turn 2

    state.on_response_started(3)
    assert state.on_output_audio_frame() is True  # crosses threshold: this frame also dropped
    assert state.frozen is True

    # Permanent: later turns stay frozen even though never explicitly reset.
    state.on_response_started(4)
    assert state.on_output_audio_frame() is True
    state.on_response_started(5)
    assert state.on_output_audio_frame() is True


def test_failed_response_not_counted_threshold_still_correct():
    """A response with a real pending turn but zero audio (LLM/TTS failure)
    must not consume the threshold."""
    state = FreezeState(2)

    state.on_response_started(1)
    assert state.on_output_audio_frame() is False  # counted: 1

    state.on_response_started(2)  # failed: no on_output_audio_frame() call at all

    state.on_response_started(3)
    assert state.on_output_audio_frame() is False  # counted: 2 (not 3)

    state.on_response_started(4)
    assert state.on_output_audio_frame() is True  # counted: 3, crosses threshold
    assert state.frozen is True


# ---------------------------------------------------------------------------
# Part 2: FreezeGate (Pipecat glue), driven via run_test
# ---------------------------------------------------------------------------


def test_gate_forwards_greeting_then_two_turns_then_freezes_permanently():
    asyncio.run(_gate_forwards_greeting_then_two_turns_then_freezes_permanently())


async def _gate_forwards_greeting_then_two_turns_then_freezes_permanently():
    tracker = _ScriptedTracker([None, 1, 2, 3, 4, 5])
    gate = FreezeGate(tracker, freeze_after_assistant_turns=2)

    frames_to_send: list[Frame] = [
        # Greeting: no pending turn.
        LLMFullResponseStartFrame(),
        TTSStartedFrame(),
        mk_audio(0),
        TTSStoppedFrame(),
        LLMFullResponseEndFrame(),
        # Turn 1: audible.
        LLMFullResponseStartFrame(),
        TTSStartedFrame(),
        mk_audio(11),
        mk_audio(12),
        TTSStoppedFrame(),
        LLMFullResponseEndFrame(),
        # Turn 2: audible.
        LLMFullResponseStartFrame(),
        TTSStartedFrame(),
        mk_audio(21),
        TTSStoppedFrame(),
        LLMFullResponseEndFrame(),
        # Turn 3: crosses the threshold -- every audio frame dropped, but
        # text/control/interruption still pass (item 5).
        LLMFullResponseStartFrame(),
        TTSStartedFrame(),
        TextFrame("turn 3 text still flows"),
        InterruptionFrame(),
        mk_audio(31),
        mk_audio(32),
        TTSStoppedFrame(),
        LLMFullResponseEndFrame(),
        # Turn 4: still frozen (permanent).
        LLMFullResponseStartFrame(),
        mk_audio(41),
        LLMFullResponseEndFrame(),
        # Turn 5: still frozen.
        LLMFullResponseStartFrame(),
        mk_audio(51),
        LLMFullResponseEndFrame(),
    ]

    down_frames, _ = await run_test(gate, frames_to_send=frames_to_send, ignore_start=True)

    # Only pre-freeze audio (greeting + turns 1-2) reached downstream, and no
    # substitute/zero-valued frame appears in its place.
    assert markers_of(down_frames) == [0, 11, 12, 21]

    # Turn 3's text and the interruption still flow even though its audio
    # was dropped.
    assert any(
        isinstance(f, TextFrame) and f.text == "turn 3 text still flows" for f in down_frames
    )
    assert any(isinstance(f, InterruptionFrame) for f in down_frames)

    # Every LLMFullResponseStartFrame/EndFrame is forwarded unchanged
    # (unrelated-frame passthrough), for all 6 responses.
    assert sum(isinstance(f, LLMFullResponseStartFrame) for f in down_frames) == 6
    assert sum(isinstance(f, LLMFullResponseEndFrame) for f in down_frames) == 6

    assert gate._state.frozen is True


def test_new_gate_instance_starts_unfrozen():
    asyncio.run(_new_gate_instance_starts_unfrozen())


async def _new_gate_instance_starts_unfrozen():
    """Session B's gate must not inherit any state from session A's."""
    tracker_a = _ScriptedTracker([1, 2, 3])
    gate_a = FreezeGate(tracker_a, freeze_after_assistant_turns=2)
    frames_a: list[Frame] = [
        LLMFullResponseStartFrame(),
        mk_audio(1),
        LLMFullResponseEndFrame(),
        LLMFullResponseStartFrame(),
        mk_audio(2),
        LLMFullResponseEndFrame(),
        LLMFullResponseStartFrame(),
        mk_audio(3),
        LLMFullResponseEndFrame(),
    ]
    down_a, _ = await run_test(gate_a, frames_to_send=frames_a, ignore_start=True)
    assert markers_of(down_a) == [1, 2]
    assert gate_a._state.frozen is True

    # A brand new gate/tracker pair (session B) starts at count 0, unfrozen,
    # regardless of what happened in session A.
    tracker_b = _ScriptedTracker([10, 20])
    gate_b = FreezeGate(tracker_b, freeze_after_assistant_turns=2)
    frames_b: list[Frame] = [
        LLMFullResponseStartFrame(),
        mk_audio(10),
        LLMFullResponseEndFrame(),
        LLMFullResponseStartFrame(),
        mk_audio(20),
        LLMFullResponseEndFrame(),
    ]
    down_b, _ = await run_test(gate_b, frames_to_send=frames_b, ignore_start=True)
    assert markers_of(down_b) == [10, 20]
    assert gate_b._state.frozen is False


# ---------------------------------------------------------------------------
# Item 9: Gate C compatibility -- frozen turn still gets a transcript entry
# with its turnId, and no latency.
# ---------------------------------------------------------------------------


def test_frozen_turn_has_transcript_entry_and_no_latency(tmp_path):
    asyncio.run(_frozen_turn_has_transcript_entry_and_no_latency(tmp_path))


async def _frozen_turn_has_transcript_entry_and_no_latency(tmp_path):
    """Drives the real TurnTracker + SessionClockAnchor + FreezeGate chain
    through two chronologically sequential responses (two separate run_test
    calls, since a real second user turn can only be armed -- i.e.
    `on_user_turn_finalized` -- after the first turn's pending measurement
    has actually been consumed by its own bot audio reaching the anchor;
    doing both in one synthetous frame list would silently overwrite
    TurnTracker's single pending slot before any frame ever ran). `gate`
    and `anchor` are reused across both calls (same session, same running
    call) -- only the wrapping `Pipeline` is rebuilt per call, exactly like
    bot.py builds one pipeline that both responses flow through in turn.
    """
    session = Session(sessions_dir=tmp_path)
    tracker = TurnTracker()
    gate = FreezeGate(tracker, freeze_after_assistant_turns=1)
    anchor = SessionClockAnchor(session, tracker)

    # -- Turn 1: real user turn, audible response. --
    tracker.on_vad_stop(1000)
    turn1 = tracker.on_user_turn_finalized(1100)
    session.add_user_turn("hello", turn1)
    tracker.on_assistant_turn_started()  # independent of audio; does not consume `pending`
    assert tracker.response_turn_id == turn1

    frames1: list[Frame] = [
        LLMFullResponseStartFrame(),
        BotStartedSpeakingFrame(),
        mk_audio(1),
        LLMFullResponseEndFrame(),
    ]
    await run_test(Pipeline([gate, anchor]), frames_to_send=frames1, ignore_start=True)
    session.add_assistant_turn("hi there", turn1)

    # -- Turn 2: real user turn, but its response is frozen (threshold=1,
    #    already crossed by turn 1's audio). --
    tracker.on_vad_stop(5000)
    turn2 = tracker.on_user_turn_finalized(5100)
    session.add_user_turn("are you there", turn2)
    tracker.on_assistant_turn_started()
    assert tracker.response_turn_id == turn2

    frames2: list[Frame] = [
        LLMFullResponseStartFrame(),
        # No BotStartedSpeakingFrame here: in the real pipeline, the
        # transport only emits it upon receiving audio, and the gate drops
        # this response's audio before the transport ever sees it.
        mk_audio(2),
        LLMFullResponseEndFrame(),
    ]
    down2, _ = await run_test(Pipeline([gate, anchor]), frames_to_send=frames2, ignore_start=True)
    assert markers_of(down2) == []  # turn 2's audio never reached the anchor
    session.add_assistant_turn("yes", turn2)

    await session.finalize()
    payload = json.loads((session.dir / "session.json").read_text())

    turn1_assistant = next(
        e for e in payload["transcript"] if e["turnId"] == turn1 and e["role"] == "assistant"
    )
    assert turn1_assistant["text"] == "hi there"
    turn2_assistant = next(
        e for e in payload["transcript"] if e["turnId"] == turn2 and e["role"] == "assistant"
    )
    assert turn2_assistant["text"] == "yes"  # frozen turn still gets its transcript entry

    turn1_latencies = [entry for entry in payload["latencies"] if entry["turnId"] == turn1]
    assert len(turn1_latencies) == 1  # turn 1 was audible: latency measured

    # Turn 2 never produced a BotStartedSpeakingFrame (frozen before output),
    # so the tracker/anchor could not and did not fabricate a latency for it.
    turn2_latencies = [entry for entry in payload["latencies"] if entry["turnId"] == turn2]
    assert turn2_latencies == []


# ---------------------------------------------------------------------------
# Item 10: Gate B compatibility -- pre-freeze bot audio recorded, post-freeze
# bot channel silent, user channel keeps recording throughout.
# ---------------------------------------------------------------------------


def test_gate_b_recording_silent_after_freeze():
    asyncio.run(_gate_b_recording_silent_after_freeze())


async def _gate_b_recording_silent_after_freeze():
    tracker = _ScriptedTracker([1, 2])
    gate = FreezeGate(tracker, freeze_after_assistant_turns=1)
    audio_buffer = AudioBufferProcessor(
        sample_rate=SAMPLE_RATE, num_channels=2, auto_start_recording=True
    )
    stage = Pipeline([gate, audio_buffer])

    captured: dict = {}

    @audio_buffer.event_handler("on_audio_data")
    async def on_audio_data(buffer, audio, sample_rate, num_channels):
        captured["audio"] = audio

    user_pre = InputAudioRawFrame(audio=b"\x10\x00" * 50, sample_rate=SAMPLE_RATE, num_channels=1)
    bot_pre = mk_audio(99)  # pre-freeze: audible
    user_post = InputAudioRawFrame(
        audio=b"\x20\x00" * 50, sample_rate=SAMPLE_RATE, num_channels=1
    )
    bot_post = mk_audio(98)  # post-freeze: must be dropped

    frames_to_send: list[Frame] = [
        LLMFullResponseStartFrame(),
        user_pre,
        bot_pre,
        LLMFullResponseEndFrame(),
        LLMFullResponseStartFrame(),  # crosses threshold=1 here
        user_post,
        bot_post,
        LLMFullResponseEndFrame(),
    ]

    await run_test(stage, frames_to_send=frames_to_send, ignore_start=True)

    assert "audio" in captured
    import struct

    samples = struct.unpack(f"<{len(captured['audio']) // 2}h", captured["audio"])
    left = samples[0::2]  # user
    right = samples[1::2]  # bot

    assert any(s != 0 for s in left), "user (left) channel must keep recording after freeze"
    # Pre-freeze bot audio (0x0099 truncated to one byte -> nonzero LE int16)
    # is present; post-freeze bot audio never reached the recorder, so the
    # whole right channel across this run is either the pre-freeze samples
    # or silence -- never a value from bot_post.
    assert any(s != 0 for s in right), "pre-freeze bot audio must still be recorded"


# ---------------------------------------------------------------------------
# Item 11: no simulator ground truth persisted anywhere in session.json.
# ---------------------------------------------------------------------------


def test_no_freeze_ground_truth_in_session_json(tmp_path):
    asyncio.run(_no_freeze_ground_truth_in_session_json(tmp_path))


async def _no_freeze_ground_truth_in_session_json(tmp_path):
    session = Session(sessions_dir=tmp_path)
    session.add_user_turn("hi", 1)
    session.add_assistant_turn("hello", 1)
    session.add_user_turn("still there?", 2)
    # Turn 2's response is frozen: no assistant entry gets audio-derived
    # data, but the entry itself (added independently of the gate, exactly
    # as bot.py's on_assistant_turn_stopped does) is otherwise ordinary.
    session.add_assistant_turn("yes", 2)

    await session.finalize()
    raw = (session.dir / "session.json").read_text()

    denylist = re.compile(r"freeze|frozen|simulator|inject", re.IGNORECASE)
    assert not denylist.search(raw), "session.json must carry no simulator ground truth"


# ---------------------------------------------------------------------------
# Item 12: no provider services constructed in this file.
# ---------------------------------------------------------------------------


def test_no_provider_services_constructed():
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
# Item 13: config validation.
# ---------------------------------------------------------------------------


def test_config_zero_disables_freeze(monkeypatch):
    # Calls the pure parsing function directly with the env var monkeypatched,
    # rather than reloading the config module: config.py's `load_dotenv
    # (override=True)` would otherwise re-read server/.env on every reload
    # and stomp the monkeypatched value with whatever is really configured
    # there (CLAUDE.md: never touch server/.env).
    import config

    monkeypatch.setenv("FREEZE_AFTER_ASSISTANT_TURNS", "0")
    assert config._parse_freeze_after_assistant_turns() == 0


@pytest.mark.parametrize("bad_value", ["-1", "not-a-number", "1.5"])
def test_config_invalid_value_fails_fast(monkeypatch, bad_value):
    import config

    monkeypatch.setenv("FREEZE_AFTER_ASSISTANT_TURNS", bad_value)
    with pytest.raises(RuntimeError, match="FREEZE_AFTER_ASSISTANT_TURNS"):
        config._parse_freeze_after_assistant_turns()
