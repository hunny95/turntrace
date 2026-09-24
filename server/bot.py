"""TurnTrace realtime voice pipeline (Gate A + Gate B).

Browser mic --> SmallWebRTC --> Deepgram STT --> Gemini LLM --> Cartesia TTS
--> SmallWebRTC --> browser speaker.

Gate B adds per-session recording + transcript persistence (server/session.py).
Latency capture and freeze injection are later gates (C/D).
"""

from __future__ import annotations

import os

from google.genai.types import HttpOptions, HttpRetryOptions
from loguru import logger
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import Frame, InputAudioRawFrame, LLMRunFrame, OutputAudioRawFrame
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
    GEMINI_MODEL,
    GEMINI_RETRY_ATTEMPTS,
    GEMINI_RETRY_EXP_BASE,
    GEMINI_RETRY_HTTP_STATUS_CODES,
    GEMINI_RETRY_INITIAL_DELAY,
    GEMINI_RETRY_MAX_DELAY,
)
from context_cleanup import FailedLLMTurnContextCleanup
from session import Session

# Recording sample rate, passed explicitly to AudioBufferProcessor so it is
# known and fixed rather than left to fall back to
# PipelineParams.audio_out_sample_rate.
RECORDING_SAMPLE_RATE = 24000


class SessionClockAnchor(FrameProcessor):
    """Anchors Session.t0 to the first audio frame reaching the recorder.

    Placed immediately before `audio_buffer` in the pipeline so that
    WAV sample 0 (the first audio byte AudioBufferProcessor receives, since
    start_recording() resets its silence-gap timestamps) and the session
    clock's t0 refer to the same instant. Pure pass-through: never mutates
    or drops frames.
    """

    def __init__(self, session: Session):
        super().__init__()
        self._session = session

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, (InputAudioRawFrame, OutputAudioRawFrame)):
            self._session.mark_first_audio()

        await self.push_frame(frame, direction)


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

    context_cleanup = FailedLLMTurnContextCleanup(context, llm)

    clock_anchor = SessionClockAnchor(session)
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
            transport.output(),  # Transport bot output
            clock_anchor,  # Gate B: anchor session t0 to first recorded audio frame
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
        # transcriptions never reach this handler.
        session.add_user_turn(message.content)

    @assistant_aggregator.event_handler("on_assistant_turn_stopped")
    async def on_assistant_turn_stopped(aggregator, message):
        # Fires on LLMFullResponseEndFrame, interruption, and EndFrame/
        # CancelFrame, independently of whether bot audio was delivered.
        session.add_assistant_turn(message.content)

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
