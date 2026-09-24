"""Per-session artifact persistence for TurnTrace (Gate B).

Owns one voice session's identity, session-relative monotonic clock,
transcript, recording bytes, and atomic finalization to
``data/sessions/<uuid>/``.

No Pipecat imports here: this module only stores bytes/text handed to it
by event handlers wired in bot.py, and writes them to disk. Keeping it
framework-free makes it independently testable (see tests/test_session.py)
and keeps the future FreezeGate (Gate D) out of scope for Gate B.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
import wave
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger

from config import SESSIONS_DIR

# WAV output is always 16-bit PCM (matches AudioBufferProcessor's output and
# Pipecat's audio frames throughout this pipeline).
SAMPLE_WIDTH_BYTES = 2

# Documented channel layout: user on the left channel (0), bot on the right
# channel (1). Matches AudioBufferProcessor(num_channels=2)'s
# interleave_stereo_audio(user, bot) convention.
CHANNEL_LAYOUT = ["user", "bot"]


@dataclass
class TranscriptEntry:
    """One finalized transcript line, stamped with the session clock."""

    role: str
    text: str
    timestamp_ms: int

    def to_json(self) -> dict:
        return {"role": self.role, "text": self.text, "timestampMs": self.timestamp_ms}


class Session:
    """One voice session's artifacts: transcript + 2-channel recording.

    Timing: ``rel_ms()`` returns milliseconds since ``t0``. ``t0`` defaults
    to session-creation time (a documented fallback for the period before
    any audio has been seen) and is overwritten exactly once, by
    ``mark_first_audio()``, when the first audio frame reaches the
    recorder -- which is WAV sample 0. All timestamps use
    ``time.monotonic()`; never wall-clock time.
    """

    def __init__(self, sessions_dir: Path | str = SESSIONS_DIR) -> None:
        self.id = str(uuid.uuid4())
        self.started_at = (
            datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        )

        self._sessions_dir = Path(sessions_dir).resolve()
        self.dir = self._resolve_session_dir(self.id)

        # Fallback anchor: session creation time. Overwritten once by
        # mark_first_audio(). See class docstring.
        self._t0 = time.monotonic()
        self._t0_set_by_audio = False

        self.transcript: list[TranscriptEntry] = []

        self.audio: bytes | None = None
        self.sample_rate: int | None = None
        self.num_channels: int | None = None

        self.finalized = False
        self._finalize_lock = asyncio.Lock()

    def _resolve_session_dir(self, session_id: str) -> Path:
        """Validate the id and build a path guaranteed to live under SESSIONS_DIR."""
        # Raises ValueError for anything that isn't a valid UUID.
        uuid.UUID(session_id)
        session_dir = (self._sessions_dir / session_id).resolve()
        if session_dir.parent != self._sessions_dir:
            raise ValueError(f"resolved session dir {session_dir} escapes {self._sessions_dir}")
        return session_dir

    # -- Clock -----------------------------------------------------------

    def mark_first_audio(self) -> None:
        """Anchor t0 to the first audio frame reaching the recorder.

        No-op after the first call, so t0 stays fixed at WAV sample 0 for
        the rest of the session.
        """
        if not self._t0_set_by_audio:
            self._t0 = time.monotonic()
            self._t0_set_by_audio = True

    def rel_ms(self) -> int:
        """Milliseconds elapsed since t0, using the monotonic clock."""
        return int((time.monotonic() - self._t0) * 1000)

    # -- Transcript --------------------------------------------------------

    def add_user_turn(self, content: str | None) -> None:
        """Append a finalized user transcript entry. Skips empty/None content."""
        if not content:
            return
        self.transcript.append(TranscriptEntry("user", content, self.rel_ms()))

    def add_assistant_turn(self, content: str | None) -> None:
        """Append a finalized assistant transcript entry. Skips empty content."""
        if not content:
            return
        self.transcript.append(TranscriptEntry("assistant", content, self.rel_ms()))

    # -- Recording -----------------------------------------------------------

    def set_recording(self, audio: bytes, sample_rate: int, num_channels: int) -> None:
        """Store the final merged/interleaved PCM bytes from on_audio_data."""
        self.audio = audio
        self.sample_rate = sample_rate
        self.num_channels = num_channels

    # -- Finalization --------------------------------------------------------

    async def finalize(self) -> None:
        """Write recording.wav and session.json. Idempotent; safe to call once
        from a try/finally so it also runs on pipeline errors.
        """
        async with self._finalize_lock:
            if self.finalized:
                return

            self.dir.mkdir(parents=True, exist_ok=True)

            duration_ms = self.rel_ms()
            recording_info: dict | None = None

            if self.audio and self.sample_rate and self.num_channels:
                recording_info = self._write_wav()
                if recording_info is not None:
                    frame_count = len(self.audio) // (SAMPLE_WIDTH_BYTES * self.num_channels)
                    duration_ms = int(frame_count / self.sample_rate * 1000)

            payload = {
                "id": self.id,
                "startedAt": self.started_at,
                "durationMs": duration_ms,
                "recording": recording_info,
                "transcript": [entry.to_json() for entry in self.transcript],
            }

            self._write_json(payload)

            self.finalized = True
            logger.info(f"Session {self.id}: finalized artifacts at {self.dir}")

    def _write_wav(self) -> dict | None:
        """Write recording.wav atomically. Returns the JSON `recording` block,
        or None (and leaves no recording.wav) if the write failed.
        """
        wav_path = self.dir / "recording.wav"
        tmp_path = self.dir / "recording.wav.tmp"
        try:
            with wave.open(str(tmp_path), "wb") as wav_file:
                wav_file.setnchannels(self.num_channels)
                wav_file.setsampwidth(SAMPLE_WIDTH_BYTES)
                wav_file.setframerate(self.sample_rate)
                wav_file.writeframes(self.audio)
            os.replace(tmp_path, wav_path)
        except OSError:
            logger.exception(f"Session {self.id}: failed to write recording.wav")
            tmp_path.unlink(missing_ok=True)
            return None

        return {
            "file": "recording.wav",
            "sampleRate": self.sample_rate,
            "channels": self.num_channels,
            "channelLayout": CHANNEL_LAYOUT,
        }

    def _write_json(self, payload: dict) -> None:
        """Write session.json atomically (tmp + fsync + replace).

        Re-raises on failure so callers/logs surface it loudly, and never
        leaves a .tmp file masquerading as the final file.
        """
        json_path = self.dir / "session.json"
        tmp_path = self.dir / "session.json.tmp"
        try:
            with open(tmp_path, "w") as f:
                json.dump(payload, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, json_path)
        except OSError:
            logger.exception(f"Session {self.id}: failed to write session.json")
            tmp_path.unlink(missing_ok=True)
            raise
