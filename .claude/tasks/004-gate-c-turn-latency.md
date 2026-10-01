# Task 004 — Gate C: Per-Turn User-to-Bot Latency

## GOAL
Capture and persist a turn-aware user-to-bot latency for every successful user turn. Store it in `session.json` on the same recording-aligned session clock that Gate B uses.

## WHY NOW
Gates A and B provide a live voice path and trustworthy per-session artifacts on one clock. The review UI (F) shows latency markers. Gate D introduces turns that have assistant text but no bot audio, and those turns must end up with no latency. Correlation therefore has to be turn-aware before Gate D lands.

## LATENCY DEFINITION
> User-to-bot latency is measured from actual user speech end to first emitted bot audio.

- `userStopMs` is the moment the user physically stopped speaking: `VADUserStoppedSpeakingFrame.timestamp - stop_secs`, converted to the session clock. This is the last VAD stop before the turn is finalized.
- `botStartMs` is the moment the first bot audio of the response to that turn was emitted toward the browser (see below).
- `latencyMs = botStartMs - userStopMs`, exactly, as integers.

The measurement therefore includes:
- the VAD `stop_secs`
- the Smart Turn wait, including its ≈3 s incomplete-utterance fallback
- STT finalization
- LLM time-to-first-byte (TTFB) and retries
- TTS startup
- output scheduling

It never uses:
- `on_user_turn_stopped` time as the start
- assistant transcript time
- LLM TTFB alone
- TTS time-to-first-audio (TTFA) alone

## VERIFIED PIPECAT 1.11.0 API SEMANTICS
Verified by the orchestrator in the installed source under `server/.venv/.../pipecat`. Re-confirm before coding.

**`observers/user_bot_latency_observer.py`: `UserBotLatencyObserver`**
- Start:
  - `VADUserStoppedSpeakingFrame` sets `_user_stopped_time = frame.timestamp - frame.stop_secs`.
  - The source comment says this is "the actual time the user stopped speaking".
  - It is taken at the VAD stop, before `UserStoppedSpeakingFrame` (turn release), so the endpointing and Smart Turn wait are inside the measured interval.
- Reset: `VADUserStartedSpeakingFrame` clears the start.
- Stop: the first downstream `BotStartedSpeakingFrame` while armed emits `on_latency_measured(observer, latency_seconds)`, then clears the start.
- It does NOT expose:
  - a turn id
  - start/stop timestamps in the event (only a float)
  - any failure awareness
- After a provider failure it stays armed until the next VAD start. There is no turn correlation, so it cannot satisfy this gate's invariants alone.
- **Decision:** reuse its *semantics* (the VAD-stop minus `stop_secs` start, and bot-start as the end) in a small custom tracker. Do not use the observer itself for persistence.

**`frames/frames.py`**
- `VADUserStoppedSpeakingFrame(SystemFrame)` has `stop_secs: float` and `timestamp: float = time.time()`, which is wall-clock time at the VAD determination.
- **Convert it to the session monotonic clock.** Example: `session.rel_ms() - round((time.time() - frame.timestamp + frame.stop_secs) * 1000)`, computed at the moment the frame is observed.

**`processors/aggregators/llm_response_universal.py`**
- The user aggregator (which owns the VAD in our config) broadcasts `VADUserStartedSpeakingFrame` / `VADUserStoppedSpeakingFrame` through `_queued_broadcast_frame`, so the frames travel both upstream and downstream.
- `UserStartedSpeakingFrame` / `UserStoppedSpeakingFrame` are the turn-level start and release, after the start and stop strategies run.
- `on_user_turn_stopped` fires at turn release, after Smart Turn.

**`transports/base_output.py`: `MediaSender`**
- `_audio_task_handler` calls `_handle_frame(frame)` first.
  - For a `TTSAudioRawFrame`, that calls `_bot_currently_speaking()` → `_bot_started_speaking()`.
  - This pushes `BotStartedSpeakingFrame` downstream and upstream, once per speech run (guarded by `_bot_speaking`).
- Only after that does it call `_internal_write_audio_frame(frame)`. The audio frame is pushed downstream only if the write succeeded (`push_downstream`).
- Therefore:
  - `BotStartedSpeakingFrame` fires only for audio that reached the output transport. Audio dropped upstream of the output (Gate D) never produces it.
  - `BotStartedSpeakingFrame` is emitted just *before* that frame's write.
  - The first `OutputAudioRawFrame` seen downstream of `transport.output()` after it is the first successfully written bot audio. This is the same frame the recorder records.
- **Gate D warning:** `_handle_bot_speech` triggers bot-started for any `TTSAudioRawFrame`, even if it is silent. Gate D's FreezeGate must DROP frames, not zero them. Record this note; do not implement Gate D.

**`pipeline/worker_observer.py`**
- Observers receive `on_push_frame` through a per-observer `asyncio.Queue` proxy task.
- Timestamps taken inside an observer are therefore delayed by scheduling. **Stamp times inside an in-pipeline processor**, not in a `BaseObserver`.

## TURN CORRELATION MODEL
There is one small framework-free state machine: a `TurnTracker` in `server/session.py`, or a sibling module. It is fed by thin Pipecat glue in `bot.py`.

State:
- `next_turn_id` (starts at 1)
- `last_vad_stop_ms: int | None`
- `pending: {turn_id, user_stop_ms} | None`, the one turn currently awaiting bot audio
- `response_turn_id: int | None`, the turn the current assistant response answers
- `awaiting_bot_audio: bool`

Transitions:
1. **VAD stop** (`VADUserStoppedSpeakingFrame`): set `last_vad_stop_ms`. The last one before finalization wins, so several VAD segments inside one turn resolve to the physical end.
2. **User turn finalized** (`on_user_turn_stopped` with non-empty content):
   - Allocate a `turn_id` and append the user transcript entry with that id.
   - Set `pending = {turn_id, last_vad_stop_ms}`, then clear `last_vad_stop_ms`.
   - If `last_vad_stop_ms` is None, the turn has no measurable start. It is still recorded in the transcript with its id, but it can never get a latency. Do not fabricate one.
3. **New user turn starts** (`UserStartedSpeakingFrame`, the turn-level start, which is also what triggers an interruption):
   - Any unmeasured `pending` turn is abandoned: set it to None.
   - Its response, if one ever arrives, is stale.
   - Verify in source whether `UserStartedSpeakingFrame` or `VADUserStartedSpeakingFrame` is the right abandonment signal for our config, and justify the choice. A VAD blip that neither starts a turn nor interrupts the bot should not discard a valid measurement.
4. **LLM failure** (the `ErrorFrame` pushed upstream by the LLM, the same signal `FailedLLMTurnContextCleanup` uses): the pending turn failed, so set `pending` to None.
   - Reuse the existing detection point. For example, give `FailedLLMTurnContextCleanup` an optional `on_llm_failed` callback, or have the tracker glue observe the same frame. Do not re-implement provider-failure classification.
   - Context cleanup and historical transcript remain separate concerns: the failed user entry stays in the transcript.
5. **Assistant response starts** (`LLMFullResponseStartFrame` downstream of the LLM, or `on_assistant_turn_started`; pick one and verify it):
   - Set `response_turn_id = pending.turn_id` if a pending turn exists, else None (the proactive greeting).
   - `on_assistant_turn_stopped` stamps the assistant transcript entry with `response_turn_id`.
6. **Bot audio emitted** (post-output tap, see BOT START):
   - If `pending` is not None and has `user_stop_ms`, record the latency, then set `pending` to None. This covers exactly one measurement per turn and handles duplicate frames.
   - Otherwise do nothing. This covers the greeting, turns that were already measured, and stale audio.

Guarantees:
- A later response can't be attached to an earlier failed or abandoned turn. `pending` holds only the newest finalized turn, and it is cleared by failure, by a new user turn, or by its own measurement.
- A turn whose audio never plays (Gate D) is cleared by the next user turn, so it has no latency even though its assistant text exists.
- The engineer must also verify, in source, that an in-flight LLM request for turn A is cancelled or interrupted when user turn B starts. If it is not, document the residual risk. Do not build request-id plumbing unless the source shows it is necessary.

## BOT START (emitted audio)
Use one pass-through processor placed after `transport.output()`: extend `SessionClockAnchor`, or add a sibling immediately next to it. It works as follows:
- It arms on a downstream `BotStartedSpeakingFrame`.
- It stamps `botStartMs = session.rel_ms()` on the next downstream `OutputAudioRawFrame`. That frame has been successfully written, and it is the same frame the recorder captures.
- It is armed once per speech run, so later frames in the run are ignored.
- It never mutates or drops frames.
- The same processor may also observe the VAD, user-turn and `LLMFullResponseStartFrame` frames that travel downstream through it, if the engineer verifies they reach that point. Otherwise, place the observation where they are verified to pass.

## TURN ID MODEL
- `turnId` is an integer ≥ 1, allocated server-side in finalization order.
- User transcript entries always carry a `turnId`.
- An assistant entry carries the `turnId` of the user turn it answers, or `null` for the proactive greeting (or any response with no pending user turn).
- No status or freeze fields.

## SESSION SCHEMA
Minimal extension of the Gate B schema:
```json
{
  "id": "…", "startedAt": "…", "durationMs": 25000, "recording": { … },
  "transcript": [
    { "turnId": null, "role": "assistant", "text": "Hi! How can I help?", "timestampMs": 900 },
    { "turnId": 1, "role": "user", "text": "…", "timestampMs": 5300 },
    { "turnId": 1, "role": "assistant", "text": "…", "timestampMs": 8100 }
  ],
  "latencies": [
    { "turnId": 1, "userStopMs": 4200, "botStartMs": 6150, "latencyMs": 1950 }
  ]
}
```
- Transcript `timestampMs` keeps its Gate B meaning: the event time when the entry was finalized.
- `latencies` is always present; it is `[]` when there are no latencies.
- No provider metrics, TTFB, TTFA or debug data are persisted.
- Old Gate B artifacts are not migrated.

## FAILED-TURN BEHAVIOR
- The user entry is kept in the transcript with its `turnId`.
- There is no assistant entry unless one was actually generated.
- There is no latency entry.
- The pending turn is cleared on the LLM `ErrorFrame`, and in any case by the next user turn.

## INTERRUPTION BEHAVIOR
- A new user turn before bot audio: the older pending turn is abandoned and gets no latency.
- User barge-in during bot audio: the turn was already measured at its first audio, and nothing more is recorded.

## PROACTIVE-GREETING BEHAVIOR
The greeting has no pending user turn. Its assistant entry has `turnId: null`, and it produces no latency.

## ACCEPTANCE CRITERIA
1. Every finalized user turn has a stable server-side `turnId`.
2. An assistant entry shares the `turnId` of the user turn it answers. The greeting has `null`.
3. The greeting produces no latency.
4. `userStopMs` = VAD stop timestamp − `stop_secs`, on the session clock (verified semantics).
5. The Smart Turn and endpointing wait is included.
6. `botStartMs` = the first successfully written bot audio frame of the response, observed after `transport.output()`.
7. `latencyMs == botStartMs - userStopMs` exactly.
8. All values are on the Gate B recording-aligned session clock, and `0 ≤ userStopMs ≤ botStartMs ≤ durationMs`.
9. Successful turns persist exactly one latency entry.
10. A provider-failed or no-audio turn has no latency.
11. A later successful response never attaches to an earlier failed turn.
12. A later successful response never attaches to an earlier abandoned turn.
13. Multiple bot audio frames produce no duplicate latency entries.
14. Transcript `timestampMs` keeps its meaning.
15–18. Gate A and B behavior, the stereo recording, transcript persistence, and idempotent finalization do not regress.
19. Tests make zero real provider calls.
20. No Gate D+ functionality (no FreezeGate, detector or UI).

## IN SCOPE
- The `TurnTracker` state machine plus `Session` integration and the JSON schema: `server/session.py` or a small new module.
- Frame-to-tracker glue in `server/bot.py`: extend the post-output processor, and add at most one small processor if needed.
- An optional failure callback on `FailedLLMTurnContextCleanup`, keeping existing behavior and tests intact.
- New tests in `server/tests/test_turn_latency.py`, with minimal updates to existing tests if the schema changes demand them.

## OUT OF SCOPE
- Freeze injection or detection.
- UI changes (the web app does not read `session.json` yet).
- Migrating old artifacts.
- Persisting a latency breakdown.
- Tuning VAD or Smart Turn.
- Git operations.

## SECURITY / COST
- No real provider calls in tests: use synthetic frames, fake processors, a patched or injected clock, and deterministic PCM.
- No secrets read or logged.
- `data/` stays untracked.

## REQUIRED AUTOMATED TESTS
Use a deterministic clock: inject or monkeypatch the monotonic/wall time sources so that times are exact.
1. **Normal turn:** VAD stop at a known time, then finalize, response start and bot audio → exact `userStopMs`, `botStartMs` and `latencyMs`; one record.
2. **Greeting:** bot audio with no user turn → `latencies == []`, and the assistant entry has `turnId: null`.
3. **Failure then success (mandatory):** turn 1 finalized → LLM `ErrorFrame` → no audio. Turn 2 → audio. Turn 1 has no latency; turn 2's latency is computed from turn 2's user stop.
   - Also run the variant *without* an `ErrorFrame`, where only the new user turn supersedes.
4. **Superseded turn:** turn A finalized → `UserStartedSpeaking` for B → B finalized → audio → only B is measured.
5. **Duplicate audio:** many audio frames and a second `BotStartedSpeakingFrame` in one turn → one record, first frame time.
6. **Endpointing:** physical VAD stop at T, turn finalized at T+3 s (Smart Turn fallback), audio at T+4.5 s → `latencyMs == 4500`. It is not measured from finalization.
7. **Persisted JSON:** `turnId` values and `latencies` survive `finalize()`, and the latency invariants hold in the written file.
8. **Recording regression:** the existing stereo WAV tests still pass. Also add a pipeline-level test through `run_test` that proves the post-output processor still passes every frame unmodified.
9. **Failed-turn transcript:** the failed user text stays in the transcript with its `turnId` and no latency.
10. **No external APIs:** tests construct no real provider services, or assert that no network client is created or called.
11. **Gate D precursor:** assistant response start plus assistant text for turn N, with no bot audio, then turn N+1 with audio → N has no latency, and the assistant entry for N has `turnId` N.

Also run the full backend test suite and the frontend test, typecheck, lint and build.

## HUMAN E2E TEST
The orchestrator runs this after QA passes: one short real session (a greeting and two questions). Verify `session.json` with a local script.

## RETURN FORMAT
Use the software-engineer or qa-engineer structured format from `.claude/agents/*.md`.
