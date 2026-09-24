# Task 005 — Gate D: Deterministic Permanent Bot-Audio Freeze Injection

## GOAL
Add a backend-only, deterministic, per-session freeze simulator (`FreezeGate`). Once the configured number of user-triggered assistant turns has been spoken, it drops every later bot audio frame before `transport.output()`, for the rest of the call. Text, control, STT, LLM, transcript and connection stay alive.

## WHY NOW
Gates A–C provide a live voice path, trustworthy recording and transcript artifacts, and turn-aware latency. Gate E needs a real frozen session to detect from ordinary evidence. Gate D creates that condition only. It does not detect it.

## VERIFIED PIPECAT 1.11.0 FRAME SEMANTICS
The orchestrator checked these in the installed source under `server/.venv/lib/python3.12/site-packages/pipecat`. The implementer must re-confirm them before coding.

- **Audio frame types** (`frames/frames.py`):
  - `TTSAudioRawFrame(OutputAudioRawFrame)`
  - `OutputAudioRawFrame(DataFrame, AudioRawFrame)`
  - Cartesia emits `TTSAudioRawFrame`.
  - `FreezeGate` drops downstream `OutputAudioRawFrame`, which includes all subclasses.
- **Transport idle silence:** `base_output.py` generates idle and mixer silence `OutputAudioRawFrame`s inside the transport's own audio task, after the gate. The gate never sees them. Gate B/C already handle them.
- **BotStartedSpeakingFrame** (`base_output.py` ~801/821): emitted only by `_bot_started_speaking()`. That is reached from `_handle_bot_speech` for `TTSAudioRawFrame` / `SpeechOutputAudioRawFrame`, never from `TTSStartedFrame` alone. A `TTSStoppedFrame` with no preceding audio does not trigger bot-stopped (`_tts_audio_received` guard). Dropping audio therefore means no `BotStartedSpeakingFrame`, so there is no Gate C latency.
- **Gemini response boundary** (`services/google/llm.py` ~692): `_process_context` pushes `LLMFullResponseStartFrame` before the request is made, and errors are raised with `push_error`.
  - **Consequential:** a failed Gemini request still produces a response start/end pair with zero text and zero audio.
  - So counting at `LLMFullResponseStartFrame` would wrongly consume the threshold on provider failure.
- **TTS ordering** (`services/tts_service.py` ~779): `LLMFullResponseStartFrame` is routed through the TTS serialization queue. It therefore reaches the gate in order, before that response's `TTSStartedFrame`, `TTSAudioRawFrame` and `TTSTextFrame`s, and after the previous response's audio has drained.
- **Assistant transcript** (`llm_response_universal.py`):
  - The assistant aggregator builds its text from `TTSTextFrame` / `TextFrame`.
  - `on_assistant_turn_started` fires on `LLMFullResponseStartFrame` / `TTSStartedFrame` at the aggregator.
  - `on_assistant_turn_stopped` fires on end, interruption, or `EndFrame`/`CancelFrame`.
  - None of these need audio frames, so dropping only audio keeps the transcript.
  - The implementer must verify that `TTSTextFrame`s still reach the aggregator when their audio is dropped, including any word-timestamp pacing that could depend on audio.

## PIPELINE PLACEMENT
```
... llm → tts → freeze_gate → transport.output() → clock_anchor → audio_buffer → assistant_aggregator
```
The gate must sit before `transport.output()`. Dropped audio then never reaches the browser, `BotStartedSpeakingFrame`, the Gate C tap, or the Gate B recorder. Do not place it after the recorder, and never mute already-recorded audio.

## FREEZE STATE MACHINE
Per-session instance. The state is small:
- `user_turns_counted = 0`
- `frozen = False`
- `current_response_turn_id` (snapshot at response start)
- `current_response_counted = False`

Downstream frames only:
1. **`LLMFullResponseStartFrame`**:
   - Snapshot which user turn this response answers from `TurnTracker`. Add a small read-only public accessor, e.g. `pending_turn_id`, returning `_pending.turn_id` or `None`.
   - Reset `current_response_counted = False`.
   - Forward the frame.
2. **`OutputAudioRawFrame`**, i.e. the first real TTS output of a response:
   - If this is the response's first audio frame, the snapshot turn id is not `None`, and the response is not yet counted:
     - `user_turns_counted += 1` and mark the response counted.
     - If `user_turns_counted > FREEZE_AFTER_ASSISTANT_TURNS`, set `frozen = True` and log one concise info line.
   - Then, if `frozen`, DROP the frame. Otherwise forward it.
   - The decision is made before the first audio frame is forwarded, so the whole triggering response is silent.
3. **Every other frame, in either direction**: forward unchanged.

Rules:
- Once `frozen` is True, it never becomes False: no timer, no randomness, and no frontend trigger.
- A threshold of `0` disables the gate. It never freezes.
- Implementation choice: this state machine may live in a tiny framework-free class, tested directly, plus thin Pipecat glue. Keep it minimal; one file `server/freeze_gate.py` is expected.

## TURN-COUNTING RULES
- The unit is one user-associated assistant response: `LLMFullResponseStartFrame` … end. It is not a TTS chunk, sentence, text frame, audio chunk or token.
- **Greeting:** no pending user turn, so the snapshot is `None`. It is never counted and stays audible.
- **Failed LLM / empty response:** no TTS audio arrives, so nothing is counted.
- **Interrupted after audio began:** it was counted at its first audio frame. That is the simplest rule consistent with "genuinely began". Document it.
- **Cancelled before any audio:** not counted.
- **Why the snapshot reads the tracker at the gate:**
  - `tracker.response_turn_id` is stamped downstream, at the assistant aggregator, after the gate has already seen the audio, so it cannot be used.
  - `_pending` for turn N is armed at user finalization, which happens before N's `LLMFullResponseStartFrame` exists.
  - `_pending` is only cleared by N's own measurement (downstream, after audio), by a new user turn start (the response is then stale or interrupted anyway), or by LLM failure (no audio, so not counted).
  - The implementer must verify this reasoning against the code, and flag any hole.

## CONFIGURATION
- The environment variable is `FREEZE_AFTER_ASSISTANT_TURNS`, read in `server/config.py`. It already exists there.
- Default: `2`.
- `0` means disabled.
- Validate that the value is a non-negative integer, and fail fast with a clear message on an invalid value. The message may name the variable and must not print secrets.
- `.env.example` already contains `FREEZE_AFTER_ASSISTANT_TURNS=2`. Only add a short comment documenting that `0` disables it.
- Never touch `server/.env`.

## PER-SESSION LIFECYCLE
- `run_bot` builds a new `FreezeGate` each session, with no module-level mutable state, so a new call always starts at count 0, unfrozen.
- There is no reset or unfreeze API.

## RECORDING INVARIANT
The bot/right channel contains the greeting and the turn 1 and turn 2 audio, and no emitted bot audio for turns 3 and later. The recorder's own silence padding is fine. The user/left channel keeps recording normally after the freeze.

## TRANSCRIPT INVARIANT
Frozen turns still produce user and assistant transcript entries, with their normal `turnId`s and the generated assistant text.

## LATENCY INVARIANT
Frozen turns have no `BotStartedSpeakingFrame` and therefore no latency record. Latency must never be fabricated.

## DETECTOR-INDEPENDENCE INVARIANT
- Nothing about the simulator goes in `session.json` or any other artifact: no threshold, flag, activation timestamp, marker text or sidecar file.
- `Session` must not know `FreezeGate` exists.
- An info log line on activation is allowed. It is not an artifact and must never be consumed by the detector.

## ACCEPTANCE CRITERIA
1–23 as listed in the orchestrator brief:
- Backend-only.
- Placed after TTS and before `transport.output()`.
- Threshold 2 supported; greeting excluded.
- Turns 1–2 audible; turn 3 and later emit zero audio.
- Permanent.
- Input, STT, LLM and text stay alive.
- Transcript keeps frozen text and turnIds.
- No latency for frozen turns.
- Recording channels are correct.
- Only audio is dropped, with no silent substitution.
- Each new session starts unfrozen.
- No ground truth is persisted.
- Gates A–C are unchanged.
- Zero provider calls in tests.
- No Gate E/F work.

## IN SCOPE
- `server/freeze_gate.py`
- Wiring in `server/bot.py`, plus a docstring update
- A read-only accessor in `server/turn_tracker.py`
- Validation in `server/config.py`
- An `.env.example` comment
- `server/tests/test_freeze_gate.py`
- A minimal update to the Gate B test stand-in in `test_session.py`, only if it is useful

## OUT OF SCOPE
- Freeze detection or any RMS classification
- JSON freeze regions
- UI changes
- New dependencies
- Commits, pushes or PR edits
- Changes to `server/.env`

## SECURITY / COST
- Automated tests construct no Deepgram, Gemini or Cartesia services and make no network calls.
- Never print env values.

## REQUIRED AUTOMATED TESTS
All tests use synthetic frames, Pipecat `run_test` or direct `process_frame`, and fake clocks.
1. The greeting (no pending turn) is forwarded and not counted; the gate is not frozen.
2. Turns 1–2: all audio is forwarded; the gate is not frozen.
3. Turn 3: its first and every later audio frame are dropped, and zero turn-3 audio reaches downstream.
4. Turns 4–5 are also fully dropped (permanent).
5. While frozen, text, TTSText, LLM start/end, TTS start/stop, interruption and unrelated frames all pass.
6. No zero-valued or replacement audio appears downstream.
7. A new gate instance (session B) starts unfrozen, and its first two turns are audible.
8. A failed response (start/end with no audio) does not count, and the threshold still triggers on the correct later turn.
9. Gate C compatibility: in the real `clock_anchor` + `TurnTracker` chain, the frozen turn gets a transcript entry with its `turnId` and no latency; the later frozen turn also gets no latency.
10. Gate B compatibility: in a synthetic chain gate → anchor → `AudioBufferProcessor`, the user audio after the freeze is recorded, pre-freeze bot audio is present, and the post-freeze bot channel is all zero.
11. The finalized `session.json` keys contain no freeze or simulator field; add a denylist assertion.
12. No provider services are constructed or called.
13. Config validation: `0` disables; a negative or non-integer value fails.

## HUMAN E2E TEST
The orchestrator runs this after QA passes: one live call with greeting, 2 audible turns, and 2 silent turns, then a manual disconnect, then artifact inspection.

## RETURN FORMAT
Use the structured software-engineer / qa-engineer return format.
