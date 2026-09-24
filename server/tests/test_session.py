"""Gate B tests: session artifact persistence.

All tests are deterministic and offline: no Deepgram/Gemini/Cartesia
services are instantiated. Recording tests drive the real, installed
pipecat.processors.audio.audio_buffer_processor.AudioBufferProcessor through
pipecat.tests.utils.run_test with synthetic tone frames -- no network.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import math
import struct
import uuid
import wave

import pytest
from pipecat.frames.frames import (
    Frame,
    InputAudioRawFrame,
    OutputAudioRawFrame,
    TextFrame,
)
from pipecat.processors.audio.audio_buffer_processor import AudioBufferProcessor
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.tests.utils import run_test

from session import Session

SAMPLE_RATE = 24000


def tone_bytes(freq: float, duration_s: float, amplitude: int = 8000) -> bytes:
    """Generate 16-bit PCM mono tone bytes at SAMPLE_RATE."""
    n_samples = int(SAMPLE_RATE * duration_s)
    samples = [
        int(amplitude * math.sin(2 * math.pi * freq * i / SAMPLE_RATE)) for i in range(n_samples)
    ]
    return struct.pack(f"<{n_samples}h", *samples)


# ---------------------------------------------------------------------------
# A. Session creation
# ---------------------------------------------------------------------------


def test_session_id_is_valid_uuid4(tmp_path):
    session = Session(sessions_dir=tmp_path)
    parsed = uuid.UUID(session.id)
    assert parsed.version == 4


def test_session_dir_is_sessions_dir_slash_id(tmp_path):
    session = Session(sessions_dir=tmp_path)
    assert session.dir == tmp_path / session.id
    assert session.dir.parent == tmp_path


def test_non_uuid_id_is_rejected(tmp_path):
    session = Session(sessions_dir=tmp_path)
    with pytest.raises(ValueError):
        session._resolve_session_dir("not-a-uuid")


def test_path_traversal_id_is_rejected(tmp_path):
    session = Session(sessions_dir=tmp_path)
    with pytest.raises(ValueError):
        session._resolve_session_dir("../../etc")


# ---------------------------------------------------------------------------
# B. Transcript
# ---------------------------------------------------------------------------


def test_final_user_text_is_appended(tmp_path):
    session = Session(sessions_dir=tmp_path)
    session.add_user_turn("hello there")
    assert len(session.transcript) == 1
    entry = session.transcript[0]
    assert entry.role == "user"
    assert entry.text == "hello there"
    assert isinstance(entry.timestamp_ms, int)


@pytest.mark.parametrize("content", [None, ""])
def test_empty_or_none_user_content_is_skipped(tmp_path, content):
    session = Session(sessions_dir=tmp_path)
    session.add_user_turn(content)
    assert session.transcript == []


def test_assistant_text_is_appended(tmp_path):
    session = Session(sessions_dir=tmp_path)
    session.add_assistant_turn("hi, how can I help?")
    assert len(session.transcript) == 1
    assert session.transcript[0].role == "assistant"


@pytest.mark.parametrize("content", [None, ""])
def test_empty_or_none_assistant_content_is_skipped(tmp_path, content):
    session = Session(sessions_dir=tmp_path)
    session.add_assistant_turn(content)
    assert session.transcript == []


def test_timestamps_are_nondecreasing_ints_relative_to_t0(tmp_path, monkeypatch):
    session = Session(sessions_dir=tmp_path)

    # Fixed, strictly increasing monotonic clock.
    ticks = itertools.count(start=100.0, step=0.5)
    monkeypatch.setattr("session.time.monotonic", lambda: next(ticks))

    # First call to monotonic() re-anchors t0 (still un-set-by-audio) to
    # 100.0; subsequent add_* calls use later ticks.
    session._t0 = 100.0
    session.add_user_turn("one")
    session.add_assistant_turn("two")
    session.add_user_turn("three")

    timestamps = [e.timestamp_ms for e in session.transcript]
    assert timestamps == sorted(timestamps)
    assert all(isinstance(t, int) for t in timestamps)


def test_mark_first_audio_anchors_once(monkeypatch, tmp_path):
    session = Session(sessions_dir=tmp_path)

    ticks = iter([10.0, 20.0, 30.0])
    monkeypatch.setattr("session.time.monotonic", lambda: next(ticks))

    session.mark_first_audio()  # t0 <- 10.0 (consumes first tick)
    assert session._t0 == 10.0

    session.mark_first_audio()  # no-op: second call must not re-anchor
    assert session._t0 == 10.0


def test_user_turn_stopped_content_contract():
    """Contract-level check for the on_user_turn_stopped handler wiring in
    bot.py, in place of driving the real LLMUserAggregator/UserTurnController
    end to end.

    Rationale: on_user_turn_stopped is fired by UserTurnController, whose
    turn-start/turn-stop strategies depend on VAD timing and internal
    asyncio-task scheduling (see
    pipecat/processors/aggregators/llm_response_universal.py). Reliably
    driving that through interim -> final transcription frames in a fast,
    deterministic unit test is impractical without flakiness. Per the Gate B
    task spec, we instead test the documented event contract:

    - message.content is the finalized aggregated text (verified by source
      inspection: UserTurnStoppedMessage.content, and that
      InterimTranscriptionFrame is never used to build it -- interim frames
      are a distinct frame class from the finalized aggregation).
    - our handler (session.add_user_turn) only ever receives this finalized
      string (or None in realtime mode, which we don't use), and skips
      falsy content by construction.
    """
    from pipecat.processors.aggregators.llm_response_universal import UserTurnStoppedMessage

    # InterimTranscriptionFrame is a distinct type from TranscriptionFrame;
    # Session.add_user_turn only ever takes str | None, never a Frame, so
    # there is no code path by which an interim fragment could be persisted.
    from pipecat.frames.frames import InterimTranscriptionFrame, TranscriptionFrame

    assert issubclass(InterimTranscriptionFrame, TextFrame)
    assert issubclass(TranscriptionFrame, TextFrame)
    assert InterimTranscriptionFrame is not TranscriptionFrame

    message = UserTurnStoppedMessage(content="final text", timestamp="2026-01-01T00:00:00Z")
    assert message.content == "final text"


def test_bot_handler_forwards_only_message_content(tmp_path):
    """Simulates bot.py's on_user_turn_stopped/on_assistant_turn_stopped
    handler bodies directly, proving they forward only message.content
    (never a raw frame) and that Session's own skip-empty logic applies.
    """
    from pipecat.processors.aggregators.llm_response_universal import (
        AssistantTurnStoppedMessage,
        UserTurnStoppedMessage,
    )

    session = Session(sessions_dir=tmp_path)

    # Mirrors: async def on_user_turn_stopped(aggregator, strategy, message)
    user_message = UserTurnStoppedMessage(content="hi", timestamp="t")
    session.add_user_turn(user_message.content)

    # Realtime-mode case: content is None; must be skipped, not crash.
    realtime_message = UserTurnStoppedMessage(content=None, timestamp="t")
    session.add_user_turn(realtime_message.content)

    # Mirrors: async def on_assistant_turn_stopped(aggregator, message)
    assistant_message = AssistantTurnStoppedMessage(content="hello!", interrupted=False, timestamp="t")
    session.add_assistant_turn(assistant_message.content)

    empty_assistant_message = AssistantTurnStoppedMessage(content="", interrupted=True, timestamp="t")
    session.add_assistant_turn(empty_assistant_message.content)

    assert [e.text for e in session.transcript] == ["hi", "hello!"]
    assert [e.role for e in session.transcript] == ["user", "assistant"]


# ---------------------------------------------------------------------------
# C. Recording
# ---------------------------------------------------------------------------


def test_recording_is_stereo_with_user_left_bot_right(tmp_path):
    asyncio.run(_recording_is_stereo_with_user_left_bot_right(tmp_path))


async def _recording_is_stereo_with_user_left_bot_right(tmp_path):
    audio_buffer = AudioBufferProcessor(
        sample_rate=SAMPLE_RATE,
        num_channels=2,
        auto_start_recording=True,
    )

    captured: dict = {}

    @audio_buffer.event_handler("on_audio_data")
    async def on_audio_data(buffer, audio, sample_rate, num_channels):
        captured["audio"] = audio
        captured["sample_rate"] = sample_rate
        captured["num_channels"] = num_channels

    user_tone = InputAudioRawFrame(
        audio=tone_bytes(440.0, 0.1), sample_rate=SAMPLE_RATE, num_channels=1
    )
    bot_tone = OutputAudioRawFrame(
        audio=tone_bytes(880.0, 0.1), sample_rate=SAMPLE_RATE, num_channels=1
    )

    await run_test(
        audio_buffer,
        frames_to_send=[user_tone, bot_tone],
    )

    assert "audio" in captured, "on_audio_data never fired"
    assert captured["sample_rate"] == SAMPLE_RATE
    assert captured["num_channels"] == 2

    session = Session(sessions_dir=tmp_path)
    session.set_recording(captured["audio"], captured["sample_rate"], captured["num_channels"])
    await session.finalize()

    wav_path = session.dir / "recording.wav"
    assert wav_path.exists()

    with wave.open(str(wav_path), "rb") as wav_file:
        assert wav_file.getnchannels() == 2
        assert wav_file.getframerate() == SAMPLE_RATE
        assert wav_file.getsampwidth() == 2
        frames = wav_file.readframes(wav_file.getnframes())

    samples = struct.unpack(f"<{len(frames) // 2}h", frames)
    left = samples[0::2]
    right = samples[1::2]

    assert any(s != 0 for s in left), "left (user) channel is all-zero"
    assert any(s != 0 for s in right), "right (bot) channel is all-zero"


# ---------------------------------------------------------------------------
# D. Freeze-invariant proof
# ---------------------------------------------------------------------------


class DropBotAudio(FrameProcessor):
    """Test-only stand-in for the future Gate D FreezeGate.

    Drops OutputAudioRawFrame (simulating suppressed bot audio) while
    passing every other frame -- including TextFrame, standing in for the
    TTS text the real assistant_aggregator would consume -- straight
    through unmodified. Lives only in this test.
    """

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, OutputAudioRawFrame):
            return
        await self.push_frame(frame, direction)


def test_freeze_invariant_bot_audio_dropped_before_recorder(tmp_path):
    asyncio.run(_freeze_invariant_bot_audio_dropped_before_recorder(tmp_path))


async def _freeze_invariant_bot_audio_dropped_before_recorder(tmp_path):
    """Proves: with a frame-dropping stage placed upstream of the recorder
    (standing in for Gate D's FreezeGate, which sits between tts and
    transport.output()), the bot (right) channel is silent while other
    frame types (standing in for the text the assistant transcript is
    built from) still reach downstream of the drop point.

    This does not simulate the real transport.output()/MediaSender
    push-after-write behavior (impractical without a live transport); that
    behavior is instead verified by source inspection of
    pipecat/processors/frame_processor... base_output.py (see
    .claude/tasks/003-gate-b-session-artifacts.md). What this test proves
    is narrower and precise: a processor placed before the recorder that
    drops OutputAudioRawFrame silences only the bot channel, while
    unrelated frames keep flowing past that same drop point.
    """
    audio_buffer = AudioBufferProcessor(
        sample_rate=SAMPLE_RATE,
        num_channels=2,
        auto_start_recording=True,
    )

    from pipecat.pipeline.pipeline import Pipeline

    stage = Pipeline([DropBotAudio(), audio_buffer])

    captured: dict = {}

    @audio_buffer.event_handler("on_audio_data")
    async def on_audio_data(buffer, audio, sample_rate, num_channels):
        captured["audio"] = audio

    user_tone = InputAudioRawFrame(
        audio=tone_bytes(440.0, 0.1), sample_rate=SAMPLE_RATE, num_channels=1
    )
    bot_tone = OutputAudioRawFrame(
        audio=tone_bytes(880.0, 0.1), sample_rate=SAMPLE_RATE, num_channels=1
    )
    assistant_text = TextFrame("this text still reaches downstream")

    down_frames, _ = await run_test(
        stage,
        frames_to_send=[user_tone, bot_tone, assistant_text],
        ignore_start=True,
    )

    # The stand-in "transcript" frame passed the drop point even though the
    # bot audio right next to it did not.
    assert any(isinstance(f, TextFrame) for f in down_frames)
    assert not any(isinstance(f, OutputAudioRawFrame) for f in down_frames)

    assert "audio" in captured
    frames = captured["audio"]
    samples = struct.unpack(f"<{len(frames) // 2}h", frames)
    right = samples[1::2]
    assert all(s == 0 for s in right), "bot channel should be silent: audio was dropped upstream"


# ---------------------------------------------------------------------------
# E. Finalization
# ---------------------------------------------------------------------------


def _make_finalized_session(tmp_path, with_audio: bool = True) -> Session:
    session = Session(sessions_dir=tmp_path)
    session.add_user_turn("hi")
    session.add_assistant_turn("hello!")
    if with_audio:
        audio = tone_bytes(440.0, 0.05) + tone_bytes(880.0, 0.05)  # interleaved-ish stand-in
        # Build a real interleaved stereo buffer: 0.05s at 24000 Hz stereo.
        n = int(SAMPLE_RATE * 0.05)
        mono = struct.unpack(f"<{n}h", tone_bytes(440.0, 0.05))
        interleaved = struct.pack(f"<{2 * n}h", *[v for pair in zip(mono, mono) for v in pair])
        session.set_recording(interleaved, SAMPLE_RATE, 2)
    return session


def test_finalize_writes_wav_and_json_with_matching_duration(tmp_path):
    asyncio.run(_finalize_writes_wav_and_json_with_matching_duration(tmp_path))


async def _finalize_writes_wav_and_json_with_matching_duration(tmp_path):
    session = _make_finalized_session(tmp_path)
    await session.finalize()

    wav_path = session.dir / "recording.wav"
    json_path = session.dir / "session.json"
    assert wav_path.exists()
    assert json_path.exists()

    with wave.open(str(wav_path), "rb") as wav_file:
        wav_duration_ms = int(wav_file.getnframes() / wav_file.getframerate() * 1000)

    payload = json.loads(json_path.read_text())
    assert payload["recording"]["file"] == "recording.wav"
    assert payload["recording"]["channelLayout"] == ["user", "bot"]
    assert abs(payload["durationMs"] - wav_duration_ms) <= 1
    assert payload["transcript"][0] == {"role": "user", "text": "hi", "timestampMs": payload["transcript"][0]["timestampMs"]}


def test_finalize_is_idempotent(tmp_path):
    asyncio.run(_finalize_is_idempotent(tmp_path))


async def _finalize_is_idempotent(tmp_path):
    session = _make_finalized_session(tmp_path)
    await session.finalize()

    wav_path = session.dir / "recording.wav"
    json_path = session.dir / "session.json"
    wav_bytes_before = wav_path.read_bytes()
    json_bytes_before = json_path.read_bytes()
    wav_mtime_before = wav_path.stat().st_mtime_ns
    json_mtime_before = json_path.stat().st_mtime_ns

    await session.finalize()  # second call: must be a no-op

    assert wav_path.read_bytes() == wav_bytes_before
    assert json_path.read_bytes() == json_bytes_before
    assert wav_path.stat().st_mtime_ns == wav_mtime_before
    assert json_path.stat().st_mtime_ns == json_mtime_before


def test_wav_write_failure_gives_null_recording(tmp_path, monkeypatch):
    asyncio.run(_wav_write_failure_gives_null_recording(tmp_path, monkeypatch))


async def _wav_write_failure_gives_null_recording(tmp_path, monkeypatch):
    session = _make_finalized_session(tmp_path)

    def boom(*args, **kwargs):
        raise OSError("simulated disk failure")

    monkeypatch.setattr("session.wave.open", boom)

    await session.finalize()

    assert not (session.dir / "recording.wav").exists()
    assert not (session.dir / "recording.wav.tmp").exists()

    payload = json.loads((session.dir / "session.json").read_text())
    assert payload["recording"] is None


def test_json_write_failure_leaves_no_session_json(tmp_path, monkeypatch):
    asyncio.run(_json_write_failure_leaves_no_session_json(tmp_path, monkeypatch))


async def _json_write_failure_leaves_no_session_json(tmp_path, monkeypatch):
    session = _make_finalized_session(tmp_path, with_audio=False)

    def boom(*args, **kwargs):
        raise OSError("simulated disk failure")

    monkeypatch.setattr("session.os.fsync", boom)

    with pytest.raises(OSError):
        await session.finalize()

    assert not (session.dir / "session.json").exists()
    assert not (session.dir / "session.json.tmp").exists()
    assert not session.finalized


def test_no_audio_gives_null_recording(tmp_path):
    asyncio.run(_no_audio_gives_null_recording(tmp_path))


async def _no_audio_gives_null_recording(tmp_path):
    session = _make_finalized_session(tmp_path, with_audio=False)
    await session.finalize()

    payload = json.loads((session.dir / "session.json").read_text())
    assert payload["recording"] is None
    assert isinstance(payload["durationMs"], int)
