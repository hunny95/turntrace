"""Gate D: deterministic, backend-only, permanent bot-audio freeze injection.

Splits into two pieces on purpose:
- `FreezeState`: framework-free state machine, fed only plain values (a
  snapshot turn id, a "here's an output audio frame" pulse). No Pipecat
  imports, so it is testable in complete isolation, the same pattern as
  `turn_tracker.TurnTracker`.
- `FreezeGate`: thin Pipecat `FrameProcessor` glue around one `FreezeState`
  instance, placed in the pipeline right after `tts` and before
  `transport.output()` (see bot.py). It forwards every frame unchanged
  except a dropped `OutputAudioRawFrame` once frozen.

See .claude/tasks/005-gate-d-freeze-injection.md for the full spec this
implements ("FREEZE STATE MACHINE", "TURN-COUNTING RULES").

Verified against installed Pipecat 1.11.0 source (server/.venv):
- Cartesia emits `TTSAudioRawFrame(OutputAudioRawFrame)`; dropping every
  `OutputAudioRawFrame` (isinstance, so subclasses included) before
  `transport.output()` means the frame never reaches the transport, so no
  `BotStartedSpeakingFrame` is ever emitted for it, no audio is written to
  the client, and the Gate B recorder (positioned after
  `transport.output()`) sees silence for it -- all three "for free", with
  no coordination needed with Gate B/C code.
- `LLMFullResponseStartFrame` is routed through `TTSService`'s own
  serialization queue (services/tts_service.py process_frame), so it
  reaches this gate already ordered after the previous response's audio
  has drained, and before that response's own `TTSStartedFrame` /
  `TTSAudioRawFrame` / `TTSTextFrame`s -- see tts_service.py process_frame,
  `elif isinstance(frame, LLMFullResponseStartFrame): ... await
  self._serialization_queue.put(frame)`.
- `CartesiaTTSService` is constructed with `pause_frame_processing=False`
  (services/cartesia/tts.py), so it never blocks its own frame processing
  waiting for a `BotStoppedSpeakingFrame` that dropped audio would prevent
  from ever arriving. Text frames (`TTSTextFrame`) are pushed by the TTS
  service itself, upstream of this gate, independently of whether the
  paired audio is later dropped here -- so the assistant transcript is
  unaffected by freezing. Pipecat's internal frame queues
  (FrameProcessorQueue/FrameQueue) are unbounded, so dropping a frame here
  creates no backpressure on the TTS service either.
"""

from __future__ import annotations

from loguru import logger
from pipecat.frames.frames import Frame, LLMFullResponseStartFrame, OutputAudioRawFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from turn_tracker import TurnTracker


class FreezeState:
    """Framework-free freeze state machine.

    Counts one unit per user-associated assistant *response* (not per TTS
    chunk/sentence/frame): the response's first `OutputAudioRawFrame`, but
    only if that response answers a real (non-`None`) pending user turn --
    see `on_response_started`. Once frozen, stays frozen forever: no timer,
    no randomness, no reset/unfreeze API (per CLAUDE.md and the task spec).

    A `freeze_after_assistant_turns` of 0 disables the gate: it can never
    freeze, since `_user_turns_counted > 0` is never true.
    """

    def __init__(self, freeze_after_assistant_turns: int) -> None:
        if freeze_after_assistant_turns < 0:
            raise ValueError("freeze_after_assistant_turns must be a non-negative integer")
        self._threshold = freeze_after_assistant_turns
        self._user_turns_counted = 0
        self._frozen = False
        self._pending_response_turn_id: int | None = None
        self._response_counted = False

    @property
    def frozen(self) -> bool:
        return self._frozen

    def on_response_started(self, pending_turn_id: int | None) -> None:
        """`LLMFullResponseStartFrame` reached the gate: snapshot which user
        turn (if any) this new response answers, and reset the per-response
        counted flag so this response's first audio frame (if any) is
        counted exactly once.

        `pending_turn_id` is `None` for the greeting (no pending user turn)
        and for a response whose triggering user turn was abandoned/failed
        before this response started -- such a response is never counted,
        matching "TURN-COUNTING RULES" (greeting excluded; failed/empty
        responses produce no audio anyway).
        """
        self._pending_response_turn_id = pending_turn_id
        self._response_counted = False

    def on_output_audio_frame(self) -> bool:
        """An `OutputAudioRawFrame` (or subclass) reached the gate,
        downstream. Returns whether it must be dropped.

        Counts the response exactly once, on its first audio frame, only if
        it answers a real user turn. The freeze decision (crossing the
        threshold) is made *before* returning whether to drop -- so if this
        very frame is what pushes the count over the threshold, this frame
        (the whole triggering response) is also dropped, per the task spec's
        "the whole triggering response is silent".
        """
        if not self._response_counted and self._pending_response_turn_id is not None:
            self._response_counted = True
            self._user_turns_counted += 1
            if self._threshold and self._user_turns_counted > self._threshold:
                self._frozen = True
        return self._frozen


class FreezeGate(FrameProcessor):
    """Pipecat glue around one `FreezeState`. Placed in the pipeline between
    `tts` and `transport.output()` (see bot.py). Forwards every frame
    unchanged except a dropped `OutputAudioRawFrame` once frozen -- no
    silent substitution, no mutation of any other frame.

    One instance per session (constructed fresh in `run_bot`), so a new call
    always starts unfrozen at count 0. There is no session/simulator state
    persisted anywhere: `Session`/`session.json` never learn this gate
    exists.
    """

    def __init__(self, tracker: TurnTracker, freeze_after_assistant_turns: int) -> None:
        super().__init__()
        self._tracker = tracker
        self._state = FreezeState(freeze_after_assistant_turns)

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if direction == FrameDirection.DOWNSTREAM:
            if isinstance(frame, LLMFullResponseStartFrame):
                self._state.on_response_started(self._tracker.pending_turn_id)
            elif isinstance(frame, OutputAudioRawFrame):
                was_frozen = self._state.frozen
                drop = self._state.on_output_audio_frame()
                if self._state.frozen and not was_frozen:
                    # Info log only -- not an artifact, never read by the
                    # Gate E detector (see CLAUDE.md "Freeze detection" and
                    # the task's "DETECTOR-INDEPENDENCE INVARIANT").
                    logger.info(
                        "FreezeGate: bot audio permanently suppressed from this point"
                    )
                if drop:
                    return

        await self.push_frame(frame, direction)
