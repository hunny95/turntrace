"""Regression tests for the Gate A error-lifecycle fix.

Covers:
  1. GoogleLLMService (as constructed by bot.py) carries the expected bounded
     retry options after Pipecat's http_options header merge.
  2. The google-genai retry predicate/backoff actually recovers from
     503-then-success, exhausts after repeated 503s, and does not retry 429 --
     exercised at the google-genai client level with no real network calls.
  3. FailedLLMTurnContextCleanup removes only the trailing unanswered user
     message(s) after an LLM ErrorFrame, preserving prior completed turns.

No network access is performed anywhere in this file.
"""

from __future__ import annotations

import asyncio

import pytest
import tenacity
from google.genai._api_client import retry_args
from google.genai.errors import ClientError, ServerError
from google.genai.types import HttpOptions, HttpRetryOptions

import config


def _build_llm_http_options() -> HttpOptions:
    """Mirror bot.py's GoogleLLMService construction of http_options."""
    return HttpOptions(
        retry_options=HttpRetryOptions(
            attempts=config.GEMINI_RETRY_ATTEMPTS,
            initial_delay=config.GEMINI_RETRY_INITIAL_DELAY,
            max_delay=config.GEMINI_RETRY_MAX_DELAY,
            exp_base=config.GEMINI_RETRY_EXP_BASE,
            http_status_codes=list(config.GEMINI_RETRY_HTTP_STATUS_CODES),
        )
    )


def test_google_llm_service_carries_retry_options_after_header_merge(monkeypatch):
    """bot.py's factory args survive Pipecat's update_google_client_http_options merge."""
    monkeypatch.setenv("GOOGLE_API_KEY", "test-key")

    from pipecat.services.google.llm import GoogleLLMService

    llm = GoogleLLMService(
        api_key="test-key",
        settings=GoogleLLMService.Settings(model=config.GEMINI_MODEL),
        http_options=_build_llm_http_options(),
    )

    retry_options = llm._http_options.retry_options
    assert retry_options is not None
    assert retry_options.attempts == 3
    assert retry_options.http_status_codes == [500, 502, 503, 504]
    # Pipecat's header merge must not have clobbered retry_options, and must
    # still have added its own client header.
    assert llm._http_options.headers is not None
    assert "x-goog-api-client" in llm._http_options.headers


def _server_error(status_code: int) -> ServerError:
    return ServerError(status_code, {"message": f"{status_code} error"}, None)


def _client_error(status_code: int) -> ClientError:
    return ClientError(status_code, {"message": f"{status_code} error"}, None)


def test_retry_recovers_after_two_503s():
    """A fake transport returning 503, 503, success recovers on the 3rd attempt."""
    retry_kwargs = retry_args(
        HttpRetryOptions(
            attempts=3,
            initial_delay=0.01,
            max_delay=0.02,
            exp_base=2,
            http_status_codes=[500, 502, 503, 504],
        )
    )
    retrying = tenacity.AsyncRetrying(**retry_kwargs)

    calls = {"count": 0}

    async def fake_transport_call():
        calls["count"] += 1
        if calls["count"] < 3:
            raise _server_error(503)
        return "ok"

    result = asyncio.run(retrying(fake_transport_call))

    assert result == "ok"
    assert calls["count"] == 3


def test_retry_exhausts_after_repeated_503s():
    """Three consecutive 503s (attempts=3) raise after exactly 3 calls."""
    retry_kwargs = retry_args(
        HttpRetryOptions(
            attempts=3,
            initial_delay=0.01,
            max_delay=0.02,
            exp_base=2,
            http_status_codes=[500, 502, 503, 504],
        )
    )
    retrying = tenacity.AsyncRetrying(**retry_kwargs)

    calls = {"count": 0}

    async def fake_transport_call():
        calls["count"] += 1
        raise _server_error(503)

    with pytest.raises(ServerError):
        asyncio.run(retrying(fake_transport_call))

    assert calls["count"] == 3


def test_retry_does_not_retry_429():
    """429 (quota) is deliberately excluded from the retryable status codes."""
    retry_kwargs = retry_args(
        HttpRetryOptions(
            attempts=3,
            initial_delay=0.01,
            max_delay=0.02,
            exp_base=2,
            http_status_codes=[500, 502, 503, 504],
        )
    )
    retrying = tenacity.AsyncRetrying(**retry_kwargs)

    calls = {"count": 0}

    async def fake_transport_call():
        calls["count"] += 1
        raise _client_error(429)

    with pytest.raises(ClientError):
        asyncio.run(retrying(fake_transport_call))

    assert calls["count"] == 1


def test_failed_turn_cleanup_drops_only_trailing_unanswered_user_message():
    from pipecat.frames.frames import ErrorFrame
    from pipecat.processors.aggregators.llm_context import LLMContext
    from pipecat.processors.frame_processor import FrameDirection

    from context_cleanup import FailedLLMTurnContextCleanup

    context = LLMContext()
    context.add_message({"role": "user", "content": "What is the biggest planet?"})
    context.add_message({"role": "assistant", "content": "Jupiter is the biggest planet."})
    context.add_message({"role": "user", "content": "And which one is the largest?"})

    class FakeLLM:
        pass

    fake_llm = FakeLLM()
    cleanup = FailedLLMTurnContextCleanup(context, fake_llm)

    error_frame = ErrorFrame(error="Unknown error occurred: 503 UNAVAILABLE")
    error_frame.processor = fake_llm

    asyncio.run(cleanup.process_frame(error_frame, FrameDirection.UPSTREAM))

    messages = context.get_messages()
    assert messages == [
        {"role": "user", "content": "What is the biggest planet?"},
        {"role": "assistant", "content": "Jupiter is the biggest planet."},
    ]


def test_failed_turn_cleanup_ignores_errors_from_other_processors():
    from pipecat.frames.frames import ErrorFrame
    from pipecat.processors.aggregators.llm_context import LLMContext
    from pipecat.processors.frame_processor import FrameDirection

    from context_cleanup import FailedLLMTurnContextCleanup

    context = LLMContext()
    context.add_message({"role": "user", "content": "Hello"})

    class FakeLLM:
        pass

    class FakeTTS:
        pass

    fake_llm = FakeLLM()
    fake_tts = FakeTTS()
    cleanup = FailedLLMTurnContextCleanup(context, fake_llm)

    error_frame = ErrorFrame(error="TTS error")
    error_frame.processor = fake_tts

    asyncio.run(cleanup.process_frame(error_frame, FrameDirection.UPSTREAM))

    assert context.get_messages() == [{"role": "user", "content": "Hello"}]


def test_failed_turn_cleanup_keeps_user_speech_added_during_failed_request():
    from pipecat.frames.frames import ErrorFrame, LLMContextFrame
    from pipecat.processors.aggregators.llm_context import LLMContext
    from pipecat.processors.frame_processor import FrameDirection

    from context_cleanup import FailedLLMTurnContextCleanup

    context = LLMContext()
    context.add_message({"role": "user", "content": "Which planet is closest to the Sun?"})
    context.add_message({"role": "assistant", "content": "Mercury."})
    context.add_message({"role": "user", "content": "And which one is the largest?"})

    class FakeLLM:
        pass

    fake_llm = FakeLLM()
    cleanup = FailedLLMTurnContextCleanup(context, fake_llm)

    # Request issued with 3 messages; user speaks again while it is retrying.
    asyncio.run(cleanup.process_frame(LLMContextFrame(context=context), FrameDirection.DOWNSTREAM))
    context.add_message({"role": "user", "content": "Hello, are you there?"})

    error_frame = ErrorFrame(error="Unknown error occurred: 503 UNAVAILABLE")
    error_frame.processor = fake_llm
    asyncio.run(cleanup.process_frame(error_frame, FrameDirection.UPSTREAM))

    assert context.get_messages() == [
        {"role": "user", "content": "Which planet is closest to the Sun?"},
        {"role": "assistant", "content": "Mercury."},
        {"role": "user", "content": "Hello, are you there?"},
    ]
