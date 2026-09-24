"""TurnTrace realtime voice pipeline (Gates A–C).

Browser mic --> SmallWebRTC --> Deepgram STT --> Gemini LLM --> Cartesia TTS
--> SmallWebRTC --> browser speaker.

Gate B adds per-session recording + transcript persistence (server/session.py).
Gate C adds per-turn user-to-bot latency capture (server/turn_tracker.py).
Gate D adds deterministic, permanent bot-audio freeze injection
(server/freeze_gate.py), placed after `tts` and before `transport.output()`.
Gate E adds an independent, deterministic post-call freeze detector
(server/freeze_detector.py), run once per session after finalize() succeeds.
"""

from __future__ import annotations

import asyncio
import os
import time

from google.genai.types import HttpOptions, HttpRetryOptions
from loguru import logger
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    Frame,
    InputAudioRawFrame,
    LLMRunFrame,
    OutputAudioRawFrame,
    UserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.observers.loggers.llm_log_observer import LLMLogObserver
from pipecat.observers.loggers.transcription_log_observer import TranscriptionLogObserver
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.processors.audio.audio_buffer_processor import AudioBufferProcessor
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.cartesia.tts import CartesiaTTSService
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.google.llm import GoogleLLMService
from pipecat.transports.base_transport import TransportParams
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
from pipecat.workers.runner import WorkerRunner

from config import (
    CARTESIA_VOICE_ID,
    FREEZE_AFTER_ASSISTANT_TURNS,
    GEMINI_MODEL,
    GEMINI_RETRY_ATTEMPTS,
    GEMINI_RETRY_EXP_BASE,
    GEMINI_RETRY_HTTP_STATUS_CODES,
    GEMINI_RETRY_INITIAL_DELAY,
    GEMINI_RETRY_MAX_DELAY,
)
from context_cleanup import FailedLLMTurnContextCleanup
from freeze_detector import run_detection_safely
from freeze_gate import FreezeGate
from session import Session
from turn_tracker import TurnTracker

# Recording sample rate, passed explicitly to AudioBufferProcessor so it is
# known and fixed rather than left to fall back to
# PipelineParams.audio_out_sample_rate.
RECORDING_SAMPLE_RATE = 24000


class SessionClockAnchor(FrameProcessor):
    """Anchors Session.t0 to the first audio frame reaching the recorder, and
    (Gate C) taps the same post-transport.output() position to feed
    TurnTracker the VAD-stop, turn-start-abandonment and first-bot-audio
    signals it needs for per-turn latency.

    Placed immediately before `audio_buffer` in the pipeline so that
    WAV sample 0 (the first audio byte AudioBufferProcessor receives, since
    start_recording() resets its silence-gap timestamps) and the session
    clock's t0 refer to the same instant. Pure pass-through: never mutates
    or drops frames.

    Gate C frame reachability, verified by source inspection (see
    .claude/tasks/004-gate-c-turn-latency.md): VADUserStoppedSpeakingFrame,
    UserStartedSpeakingFrame and BotStartedSpeakingFrame are all Pipecat
    SystemFrames. Every processor between their origin and this position
    forwards them unmodified -- LLMUserAggregator's default `else:
    push_frame(...)` branch, FailedLLMTurnContextCleanup's unconditional
    forward, GoogleLLMService's `else: push_frame(...)` branch, TTSService's
    SystemFrame bypass of its serialization queue (`not isinstance(frame,
    SystemFrame)` gate), and BaseOutputTransport's own `elif isinstance(frame,
    SystemFrame): push_frame(...)` -- so all three reach this tap positioned
    right after transport.output(), the same position that sees only
    successfully-written bot audio (base_output.py: MediaSender pushes
    OutputAudioRawFrame downstream only after a successful write, and emits
    BotStartedSpeakingFrame for that same run just before the write).

    LLMFullResponseStartFrame is NOT observed here: Cartesia's TTS service
    routes it through its own serialization queue (delayed to stay ordered
    with that turn's audio), so instead we use the already-wired
    `on_assistant_turn_started` event on assistant_aggregator (fired by
    LLMFullResponseStartFrame reaching *that* processor), see bot.py's event
    handlers below.
    """

    def __init__(self, session: Session, tracker: TurnTracker):
        super().__init__()
        self._session = session
        self._tracker = tracker
        self._awaiting_bot_audio = False

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, (InputAudioRawFrame, OutputAudioRawFrame)):
            self._session.mark_first_audio()

        if direction == FrameDirection.DOWNSTREAM:
            if isinstance(frame, VADUserStoppedSpeakingFrame):
                self._tracker.on_vad_stop(self._vad_stop_session_ms(frame))
            elif isinstance(frame, UserStartedSpeakingFrame):
                # Turn-level start (also what triggers interruption/
                # broadcast_interruption in LLMUserAggregator._on_user_turn_started),
                # not the raw per-blip VADUserStartedSpeakingFrame -- a VAD
                # blip that doesn't start a new turn must not discard a
                # valid pending measurement.
                self._tracker.on_user_turn_started()
            elif isinstance(frame, BotStartedSpeakingFrame):
                self._awaiting_bot_audio = True
            elif isinstance(frame, OutputAudioRawFrame) and self._awaiting_bot_audio:
                # First successfully-written bot audio frame of this speech
                # run -- the same frame the recorder captures.
                self._awaiting_bot_audio = False
                result = self._tracker.on_bot_audio_started(self._session.rel_ms())
                if result is not None:
                    self._session.add_latency(
                        result.turn_id, result.user_stop_ms, result.bot_start_ms
                    )

        await self.push_frame(frame, direction)

    def _vad_stop_session_ms(self, frame: VADUserStoppedSpeakingFrame) -> int:
        """Convert VADUserStoppedSpeakingFrame's wall-clock timestamp to the
        session-relative monotonic clock.

        frame.timestamp is time.time() at the VAD's determination;
        frame.timestamp - frame.stop_secs is the actual physical instant the
        user stopped speaking (both wall-clock). Converted at observation
        time via the wall-clock delta between now and that instant,
        subtracted from the current session-relative time -- see
        .claude/tasks/004-gate-c-turn-latency.md.
        """
        elapsed_ms = round((time.time() - frame.timestamp + frame.stop_secs) * 1000)
        return self._session.rel_ms() - elapsed_ms


SYSTEM_INSTRUCTION = (
    "You are a concise voice assistant. Respond naturally in one or two "
    "short sentences. Your responses will be spoken aloud. Do not use "
    "markdown, lists, emojis, or long explanations."
)


async def run_bot(webrtc_connection) -> None:
    """Build and run one TurnTrace pipeline instance for a SmallWebRTC session."""
    session = Session()
    logger.info(f"Session {session.id}: starting")

    transport = SmallWebRTCTransport(
        webrtc_connection=webrtc_connection,
        params=TransportParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_out_10ms_chunks=2,
        ),
    )

    stt = DeepgramSTTService(api_key=os.environ["DEEPGRAM_API_KEY"])

    llm = GoogleLLMService(
        api_key=os.environ["GOOGLE_API_KEY"],
        settings=GoogleLLMService.Settings(
            model=GEMINI_MODEL,
            system_instruction=SYSTEM_INSTRUCTION,
        ),
        # Bounded retry for transient Gemini server errors only (not 429,
        # which won't recover within this window). See config.py and
        # .claude/tasks/002-gate-a-error-lifecycle-fix.md.
        http_options=HttpOptions(
            retry_options=HttpRetryOptions(
                attempts=GEMINI_RETRY_ATTEMPTS,
                initial_delay=GEMINI_RETRY_INITIAL_DELAY,
                max_delay=GEMINI_RETRY_MAX_DELAY,
                exp_base=GEMINI_RETRY_EXP_BASE,
                http_status_codes=list(GEMINI_RETRY_HTTP_STATUS_CODES),
            )
        ),
    )

    tts = CartesiaTTSService(
        api_key=os.environ["CARTESIA_API_KEY"],
        settings=CartesiaTTSService.Settings(
            voice=CARTESIA_VOICE_ID,
        ),
    )

    context = LLMContext()
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(vad_analyzer=SileroVADAnalyzer()),
    )

    tracker = TurnTracker()
    context_cleanup = FailedLLMTurnContextCleanup(context, llm, on_llm_failed=tracker.on_llm_failed)
    freeze_gate = FreezeGate(tracker, FREEZE_AFTER_ASSISTANT_TURNS)

    clock_anchor = SessionClockAnchor(session, tracker)
    audio_buffer = AudioBufferProcessor(
        sample_rate=RECORDING_SAMPLE_RATE,
        num_channels=2,  # user = left (ch0), bot = right (ch1)
        auto_start_recording=True,
    )

    pipeline = Pipeline(
        [
            transport.input(),  # Transport user input
            stt,  # Speech-to-text
            user_aggregator,  # User responses
            context_cleanup,  # Drop unanswered turn from context on LLM failure
            llm,  # LLM
            tts,  # Text-to-speech
            freeze_gate,  # Gate D: drop bot audio (only) before output, once frozen
            transport.output(),  # Transport bot output
            clock_anchor,  # Gate B: anchor session t0; Gate C: VAD stop /
            # turn-start / first-bot-audio taps for TurnTracker
            audio_buffer,  # Gate B: 2-channel recording (after transport.output(),
            # so bot audio here is emitted/delivered audio -- see
            # .claude/tasks/003-gate-b-session-artifacts.md freeze invariant)
            assistant_aggregator,  # Assistant spoken responses
        ]
    )

    worker = PipelineWorker(
        pipeline,
        params=PipelineParams(
            enable_metrics=True,
            enable_usage_metrics=True,
        ),
        observers=[TranscriptionLogObserver(), LLMLogObserver()],
    )

    runner = WorkerRunner(handle_sigint=False)
    await runner.add_workers(worker)

    @audio_buffer.event_handler("on_audio_data")
    async def on_audio_data(buffer, audio, sample_rate, num_channels):
        # Handler runs as an asyncio task (see BaseObject._call_event_handler);
        # only hand bytes to Session here, no file I/O (per task spec).
        session.set_recording(audio, sample_rate, num_channels)

    @user_aggregator.event_handler("on_user_turn_stopped")
    async def on_user_turn_stopped(aggregator, strategy, message):
        # message.content is the finalized aggregated user transcript (None
        # only in realtime_service_mode, which we don't use). Interim
        # transcriptions never reach this handler. Gate C: a turn id is only
        # allocated for non-empty content, matching the TURN CORRELATION
        # MODEL ("Allocate a turn_id ... on_user_turn_stopped with non-empty
        # content"). finalized_ms lets TurnTracker attribute a VAD-stop
        # frame that arrives *after* this handler runs (see turn_tracker.py
        # module docstring, "Async ordering").
        if not message.content:
            return
        turn_id = tracker.on_user_turn_finalized(session.rel_ms())
        session.add_user_turn(message.content, turn_id)

    @assistant_aggregator.event_handler("on_assistant_turn_started")
    async def on_assistant_turn_started(aggregator):
        # Fires on LLMFullResponseStartFrame reaching assistant_aggregator
        # (cascade/non-realtime path; see _handle_llm_start in
        # llm_response_universal.py) -- used instead of tapping
        # LLMFullResponseStartFrame in SessionClockAnchor because Cartesia's
        # TTS service delays that frame in its own serialization queue.
        #
        # Ordering vs. on_user_turn_stopped (verified in
        # llm_response_universal.py's _maybe_emit_user_turn_stopped, ~1443-
        # 1476): that method awaits self._push_aggregation() -- which pushes
        # the LLMContextFrame that starts the LLM request -- BEFORE it calls
        # _call_event_handler("on_user_turn_stopped", ...). Since that event
        # is registered with is_sync=False (base_object.py), the handler
        # runs as a *separately scheduled* asyncio task, not inline; so the
        # literal claim "the user handler task is created before the LLM
        # request" is FALSE -- the LLM request is enqueued first. In
        # practice this is still safe: our on_user_turn_stopped task is
        # scheduled via call_soon and runs within microseconds (it does no
        # I/O), while LLMFullResponseStartFrame can only appear after a real
        # network round trip to Gemini (observed >=0.5s TTFB in this
        # codebase's Gate A testing -- .claude/tasks/001.../STATUS.md). So
        # tracker.on_user_turn_finalized() always completes, allocating the
        # turn id, before this handler could plausibly fire for that same
        # turn. This is a comfortable timing margin, not a framework-
        # enforced invariant -- documented here as a residual assumption.
        tracker.on_assistant_turn_started()

    @assistant_aggregator.event_handler("on_assistant_turn_stopped")
    async def on_assistant_turn_stopped(aggregator, message):
        # Fires on LLMFullResponseEndFrame, interruption, and EndFrame/
        # CancelFrame, independently of whether bot audio was delivered.
        # tracker.response_turn_id was stamped by on_assistant_turn_started
        # above, for this same (sequential, non-overlapping) assistant turn.
        session.add_assistant_turn(message.content, tracker.response_turn_id)

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        logger.info("Client connected")
        context.add_message(
            {
                "role": "developer",
                "content": "Start by greeting the user briefly and asking how you can help.",
            }
        )
        await worker.queue_frames([LLMRunFrame()])

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        logger.info("Client disconnected")
        # Stop recording (emits on_audio_data with the final buffer) before
        # cancelling, so the recording isn't truncated. CancelFrame would
        # also trigger stop_recording, but it's a no-op once already stopped.
        await audio_buffer.stop_recording()
        await runner.cancel()

    try:
        await runner.run()
    finally:
        # runner.run() only returns after pipeline cleanup has awaited every
        # processor's pending event-handler tasks (Pipeline.cleanup ->
        # _cleanup_processors -> BaseObject.cleanup), so on_audio_data and
        # on_assistant_turn_stopped handler tasks are guaranteed to have
        # completed by this point. Runs on errors too, via try/finally.
        await session.finalize()
        # Gate E: run the independent post-call freeze detector only after
        # finalize() has completed successfully (if it raised, this line is
        # never reached, and no analysis runs over a partially-written
        # session). Off the event loop; the hook itself never raises -- see
        # freeze_detector.run_detection_safely.
        await asyncio.to_thread(run_detection_safely, session.dir)
