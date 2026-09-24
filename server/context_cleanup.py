"""Post-failure LLM context cleanup (Gate A error-lifecycle fix).

When Gemini fails a turn (after its own bounded retries are exhausted),
``GoogleLLMService`` pushes an ``ErrorFrame`` upstream and produces no
assistant response. Left alone, the unanswered user message(s) for that turn
stay in the shared ``LLMContext``, so the *next* LLM run re-answers the whole
context -- including the stale question -- rather than just the new user
speech.

``FailedLLMTurnContextCleanup`` is a small processor placed directly upstream
of the LLM service in the pipeline. It watches for the LLM's own ErrorFrame
passing through (upstream direction) and strips the trailing unanswered user
message(s) of the failed request from the context before continuing to
propagate the frame. Only messages that existed when the failed request was
issued are eligible; user speech aggregated while the request was in flight
(e.g. during retry backoff) is kept.

See .claude/tasks/002-gate-a-error-lifecycle-fix.md for the incident this
addresses.
"""

from __future__ import annotations

from collections.abc import Callable

from pipecat.frames.frames import ErrorFrame, Frame, LLMContextFrame
from pipecat.processors.aggregators.llm_context import LLMContext, LLMContextMessage
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.llm_service import LLMService


class FailedLLMTurnContextCleanup(FrameProcessor):
    """Drops the trailing unanswered user turn from context after an LLM failure.

    Must be placed immediately upstream of the LLM service (i.e. directly
    before it in the pipeline's processor list) so it observes the
    ``ErrorFrame`` the LLM pushes upstream on failure, and only that LLM's
    errors -- not errors originating further downstream (e.g. TTS) that also
    pass through this link on their way further upstream.
    """

    def __init__(
        self,
        context: LLMContext,
        llm: LLMService,
        on_llm_failed: Callable[[], None] | None = None,
    ):
        super().__init__()
        self._context = context
        self._llm = llm
        # Context length when the most recent request was sent to the LLM.
        self._request_len: int | None = None
        # Gate C: optional sync callback, invoked once per detected LLM
        # failure, after context cleanup. Lets TurnTracker (server/
        # turn_tracker.py) clear the pending turn's latency measurement
        # without this module knowing anything about turns or latency --
        # reuses this module's existing ErrorFrame detection point rather
        # than re-implementing provider-failure classification elsewhere.
        self._on_llm_failed = on_llm_failed

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, LLMContextFrame) and direction == FrameDirection.DOWNSTREAM:
            self._request_len = len(frame.context.messages)
        elif (
            isinstance(frame, ErrorFrame)
            and direction == FrameDirection.UPSTREAM
            and frame.processor is self._llm
        ):
            self._drop_trailing_user_messages()
            if self._on_llm_failed is not None:
                self._on_llm_failed()

        await self.push_frame(frame, direction)

    def _drop_trailing_user_messages(self) -> None:
        """Remove the failed request's trailing user messages from the context.

        Only the first ``_request_len`` messages (what the failed request saw)
        are trimmed; stops at the first non-user message, so prior completed
        turns are left untouched. Messages added after the request are kept.
        """
        request_len = self._request_len
        self._request_len = None

        def _without_trailing_user(
            messages: list[LLMContextMessage],
        ) -> list[LLMContextMessage]:
            cut = len(messages) if request_len is None else min(request_len, len(messages))
            trimmed = list(messages[:cut])
            while trimmed and self._is_user_message(trimmed[-1]):
                trimmed.pop()
            return trimmed + list(messages[cut:])

        self._context.transform_messages(_without_trailing_user)

    @staticmethod
    def _is_user_message(message: LLMContextMessage) -> bool:
        return isinstance(message, dict) and message.get("role") == "user"
