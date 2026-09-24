"""Framework-free turn correlation state machine for Gate C latency.

No Pipecat imports here: this module is fed plain ints/None by thin Pipecat
glue in bot.py (VAD stop times already converted to the session clock, turn
finalization/abandonment/failure signals, and the first-bot-audio session
time). Keeping it framework-free makes it independently, deterministically
testable.

See .claude/tasks/004-gate-c-turn-latency.md, "TURN CORRELATION MODEL", for
the full state machine this implements.

Async ordering (repair cycle 1, see .claude/tasks -- QA-found defect):
Frame delivery to the post-output SessionClockAnchor tap (per-processor
queued task) and the on_user_turn_stopped handler (a separately scheduled
asyncio task, see llm_response_universal.py's `is_sync=False` registration)
are concurrent with each other. In particular, VADUserStoppedSpeakingFrame
for a turn can reach the tap (-> on_vad_stop) either before OR after
on_user_turn_stopped fires (-> on_user_turn_finalized) for that same turn.
TurnTracker must not lose the turn id in either ordering -- see
on_user_turn_finalized/on_vad_stop below, and PendingTurn.finalized_ms.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class LatencyResult:
    """One measured user-to-bot latency, ready for Session.add_latency()."""

    turn_id: int
    user_stop_ms: int
    bot_start_ms: int


@dataclass
class _PendingTurn:
    """The one finalized turn still awaiting correlation, in either order:
    its VAD stop time may already be known, or may still be in flight.
    """

    turn_id: int
    user_stop_ms: int | None
    finalized_ms: int


class TurnTracker:
    """Correlates user turns, VAD stop times, and first-bot-audio events into
    per-turn latency measurements -- surviving provider failures and
    interruptions without ever attaching a response to the wrong turn.

    State:
    - ``_next_turn_id``: monotonically allocated, starting at 1.
    - ``_last_vad_stop_ms``: the most recent VAD-determined physical stop
      time not yet attributed to a turn. Several VAD segments inside one
      turn resolve to the last (physical end) before finalization. Also
      used as the fallback store for a VAD stop that arrives too early to
      belong to any pending turn (i.e. it's for the *next* turn).
    - ``_pending``: the one finalized turn still awaiting its bot audio, or
      ``None``. Cleared by its own measurement, by the next user turn
      starting, or by an LLM failure -- whichever happens first -- so a
      later response can never attach to an earlier failed or abandoned
      turn. Its ``user_stop_ms`` may start out ``None`` (VAD stop not yet
      observed) and get filled in later by a late ``on_vad_stop`` -- see
      that method.
    - ``response_turn_id``: which turn (if any) the *currently open*
      assistant response answers. Independent of ``_pending``: it is read,
      not consumed, so a turn with assistant text but no bot audio (Gate D)
      still gets a correctly labeled transcript entry even though it has no
      latency. Crucially, this is set from ``_pending.turn_id`` even when
      ``user_stop_ms`` is still unknown -- turn-id correlation must never be
      lost just because the VAD-stop frame hasn't arrived yet.
    """

    def __init__(self) -> None:
        self._next_turn_id = 1
        self._last_vad_stop_ms: int | None = None
        self._pending: _PendingTurn | None = None
        self.response_turn_id: int | None = None

    def on_vad_stop(self, user_stop_ms: int) -> None:
        """VADUserStoppedSpeakingFrame observed: record the physical stop
        time (session clock).

        Two cases:
        - A pending turn is already finalized but still missing its VAD
          stop (finalization raced ahead of the VAD-stop frame), and this
          stop's session time precedes that finalization -- i.e. it
          physically belongs to the pending turn, not a later one. Fill it
          in directly.
        - Otherwise (no pending turn awaiting a stop, or this stop is later
          than the pending turn's finalization -- it's for whatever turn
          finalizes next): store it as ``_last_vad_stop_ms``, exactly as
          before. The last one before finalization wins.
        """
        if (
            self._pending is not None
            and self._pending.user_stop_ms is None
            and user_stop_ms <= self._pending.finalized_ms
        ):
            self._pending.user_stop_ms = user_stop_ms
            return
        self._last_vad_stop_ms = user_stop_ms

    def on_user_turn_finalized(self, finalized_ms: int) -> int:
        """on_user_turn_stopped fired with non-empty content: allocate a
        turn id and arm `pending` from the most recent VAD stop, if any.

        Always arms `pending` -- even when no VAD stop has been recorded
        yet (the VAD-stop frame may simply not have reached the tap yet;
        see on_vad_stop's late-fill-in path above) -- so turn-id
        correlation (response_turn_id) is never lost. A turn that truly
        never gets a VAD stop still can't produce a latency; see
        on_bot_audio_started.

        Args:
            finalized_ms: session.rel_ms() at the moment of finalization,
                used to decide whether a later-arriving VAD stop physically
                belongs to this turn or to the next one.
        """
        turn_id = self._next_turn_id
        self._next_turn_id += 1
        self._pending = _PendingTurn(
            turn_id=turn_id, user_stop_ms=self._last_vad_stop_ms, finalized_ms=finalized_ms
        )
        self._last_vad_stop_ms = None
        return turn_id

    def on_user_turn_started(self) -> None:
        """UserStartedSpeakingFrame (turn-level start / interruption signal):
        abandon any unmeasured pending turn -- its eventual response, if any,
        is stale and must not attach to it."""
        self._pending = None

    def on_llm_failed(self) -> None:
        """The LLM failed the pending turn's request: no latency for it."""
        self._pending = None

    def on_assistant_turn_started(self) -> None:
        """LLMFullResponseStartFrame / on_assistant_turn_started: stamp which
        user turn (if any) this response answers. Does not consume
        `pending` -- the bot-audio measurement is a separate concern. Set
        even when `pending.user_stop_ms` is still unknown, so the turn id
        is never lost regardless of VAD-stop arrival order (see module
        docstring)."""
        self.response_turn_id = self._pending.turn_id if self._pending is not None else None

    def on_bot_audio_started(self, bot_start_ms: int) -> LatencyResult | None:
        """First emitted bot audio of a response (post-output tap).

        Returns a measurement exactly once per pending turn, then clears
        `pending` so duplicate audio frames or a second BotStartedSpeaking
        run produce no duplicate record. Returns None for the greeting (no
        pending turn), stale/duplicate audio, turns already abandoned or
        failed, and a turn whose VAD stop never arrived -- CLAUDE.md: never
        fabricate a latency for a turn with no measurable start.
        """
        if self._pending is None:
            return None
        pending = self._pending
        self._pending = None
        if pending.user_stop_ms is None:
            return None
        return LatencyResult(pending.turn_id, pending.user_stop_ms, bot_start_ms)
