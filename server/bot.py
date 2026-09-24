"""TurnTrace realtime voice pipeline (Gate A).

Browser mic --> SmallWebRTC --> Deepgram STT --> Gemini LLM --> Cartesia TTS
--> SmallWebRTC --> browser speaker.

No recording, transcript persistence, latency capture, or freeze injection
here yet -- those are later gates (B/C/D).
"""

from __future__ import annotations

import os

from google.genai.types import HttpOptions, HttpRetryOptions
from loguru import logger
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import LLMRunFrame
from pipecat.observers.loggers.llm_log_observer import LLMLogObserver
from pipecat.observers.loggers.transcription_log_observer import TranscriptionLogObserver
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
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

SYSTEM_INSTRUCTION = (
    "You are a concise voice assistant. Respond naturally in one or two "
    "short sentences. Your responses will be spoken aloud. Do not use "
    "markdown, lists, emojis, or long explanations."
)


async def run_bot(webrtc_connection) -> None:
    """Build and run one TurnTrace pipeline instance for a SmallWebRTC session."""
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

    pipeline = Pipeline(
        [
            transport.input(),  # Transport user input
            stt,  # Speech-to-text
            user_aggregator,  # User responses
            context_cleanup,  # Drop unanswered turn from context on LLM failure
            llm,  # LLM
            tts,  # Text-to-speech
            transport.output(),  # Transport bot output
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
        await runner.cancel()

    await runner.run()
