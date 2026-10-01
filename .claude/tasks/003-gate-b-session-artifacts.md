# Task 003 — Gate B: Session Recording and Transcript Persistence

## GOAL
After each realtime voice session, persist:
- `data/sessions/<uuid>/recording.wav`: a 2-channel recording of the conversation (user on the left channel, bot on the right)
- `data/sessions/<uuid>/session.json`: session metadata plus a transcript of finalized user and assistant turns, with session-relative timestamps

## WHY NOW
Gate A proved the live voice path. Latency (C), freeze injection (D), freeze detection (E) and the review UI (F) all depend on trustworthy per-session artifacts on a single session clock.

## ARCHITECTURE
Pipeline (only one processor is added, `audio_buffer`):
```text
transport.input()
→ stt
→ user_aggregator
→ context_cleanup
→ llm
→ tts
→ transport.output()
→ audio_buffer            # NEW: AudioBufferProcessor(num_channels=2)
→ assistant_aggregator
```
The official Pipecat 1.11.0 CLI template (`pipecat/cli/templates/server/_macros/pipeline_components.jinja2`) uses this exact placement: `output_stage` → `recording_stage` → `context_assistant_stage`.

New module `server/session.py` owns a small `Session` class: UUID, clock, transcript list, recording bytes, finalization. `bot.py` wires Pipecat events into it. There is no generic framework and no database.

## VERIFIED PIPECAT 1.11.0 RECORDING API
Verified by the orchestrator in `.venv/.../pipecat/processors/audio/audio_buffer_processor.py`. Re-confirm before coding.

**Constructor.** `AudioBufferProcessor(*, sample_rate=None, num_channels=1, buffer_size=0, enable_turn_audio=False, auto_start_recording=False)`.
- `sample_rate=None` falls back to `setup.audio_out_sample_rate`, which defaults to 24000 (`PipelineParams.audio_out_sample_rate` in `pipeline/worker.py`).
- Pass `sample_rate=24000` explicitly so the rate is known and fixed.

**Stereo layout.** `num_channels=2` interleaves user audio on the LEFT channel (ch 0) and bot audio on the RIGHT channel (ch 1), via `interleave_stereo_audio(user, bot)`. The output is 16-bit PCM.

**Input sources.** Both are resampled to the target rate:
- user track: `InputAudioRawFrame`
- bot track: `OutputAudioRawFrame`

**Alignment.**
- The lagging track is padded with silence before the other track is extended.
- Gaps above 200 ms of wall-clock time are filled with silence.
- `_align_track_buffers()` pads to equal length before emitting.

**Output timing.** With `buffer_size=0`, audio is only emitted when recording stops.

**Stopping.**
- `stop_recording()` does three things in order: it emits `on_audio_data(buffer, audio, sample_rate, num_channels)` and `on_track_audio_data(buffer, user, bot, sample_rate, num_channels)`, resets the buffers, then emits `on_recording_stopped`.
- It is a no-op when recording isn't active.
- `process_frame` calls `stop_recording()` on `CancelFrame`/`EndFrame`.

**Starting.** `auto_start_recording=True` starts recording on `StartFrame`.

**Handler dispatch.** Handlers run as `asyncio` tasks, not inline (`utils/base_object.py`, `_call_event_handler`, `is_sync=False` for these events). `BaseObject.cleanup()` waits for pending handler tasks. Consequences:
- Handlers must only hand bytes to the Session (no file I/O inside the handler).
- Finalization must run after the pipeline has fully finished (after `await runner.run()` returns), never from inside a handler.

**How audio reaches a recorder placed after `transport.output()`:**
- **Bot audio.** In `base_output.py`, `MediaSender._audio_task_handler` calls `_internal_write_audio_frame(frame)` and pushes the `OutputAudioRawFrame` downstream only `if push_downstream`, i.e. only after the frame was successfully written to the client transport. So the recorder sees only emitted bot audio.
- **User audio.** `InputAudioRawFrame` passes through every stage:
  - STT, because `audio_passthrough=True` is the default in `stt_service.py`
  - the user aggregator, unless the user is muted, and no mute strategies are configured
  - `context_cleanup`, the LLM and TTS, which forward unhandled frames
  - output transport, which forwards non-audio frames

  The engineer MUST confirm this empirically (see tests) or through source inspection.

**Sample-zero alignment caveat.**
- `start_recording()` resets `_last_*_update_time` to `None`, so no leading silence is inserted.
- As a result, WAV sample 0 corresponds to the first audio frame the recorder receives, not to the moment recording started.
- The session clock `t0` MUST be anchored to that same instant (see SESSION DATA MODEL).

## VERIFIED TRANSCRIPT API
From `processors/aggregators/llm_response_universal.py`:

**User turns.** `user_aggregator` event `on_user_turn_stopped(aggregator, strategy, message: UserTurnStoppedMessage)`.
- `message.content` is the finalized, aggregated user transcript: the same text written to the LLM context.
- It is `None` only in `realtime_service_mode`. We are in cascade mode, but skip `None`/empty content defensively.
- Interim transcriptions are never delivered here, so final-only is guaranteed by construction.

**Assistant turns.** `assistant_aggregator` event `on_assistant_turn_stopped(aggregator, message: AssistantTurnStoppedMessage)`, with fields `content: str`, `interrupted: bool` and `timestamp: str`.
- It fires on `LLMFullResponseEndFrame`, on interruption, and on `EndFrame`/`CancelFrame`.
- The turn start is triggered by `LLMFullResponseStartFrame`/`TTSStartedFrame`, not by bot speech.
- `content` may be empty; skip empty content.

**Ignore the provider/ISO timestamps** in these messages. Stamp entries with our session monotonic clock when the event fires.

**Do not use:**
- the removed/legacy `TranscriptProcessor`
- log parsing

## FUTURE FREEZE-COMPATIBILITY INVARIANT (MANDATORY)
> **Bot recording must reflect emitted/delivered audio, while assistant transcript may exist independently.**

**Recording.** Gate D will insert a `FreezeGate` between `tts` and `transport.output()` that drops bot `OutputAudioRawFrame`s. With the recorder after `transport.output()`:
- dropped frames never reach the output transport, so they are never pushed downstream to `audio_buffer`
- the bot (right) channel is therefore silence-padded for that period

**Transcript.** The assistant transcript comes from `assistant_aggregator`, which aggregates `TextFrame`s (TTS text) and turn-boundary frames.
- The output transport passes these frames through its clock queue by `pts`, independently of whether audio was written.
- So generated text still reaches the transcript while the audio is suppressed.

**Rules:**
- Do NOT place the recorder upstream of `transport.output()`.
- Do NOT tap TTS audio directly.

Gate B must not implement `FreezeGate`. A unit/integration test (below) must demonstrate the invariant with a stand-in audio-dropping processor that lives only in the test.

## SESSION DATA MODEL
`Session` (in `server/session.py`):

**Identity and paths**
- `id`: `str(uuid.uuid4())`, generated server-side in `run_bot`: exactly one per pipeline run.
- `started_at`: wall-clock `datetime.now(timezone.utc)` ISO-8601 string ending in `Z` or `+00:00`, for display only.
- `dir`: `SESSIONS_DIR / id`.
  - `SESSIONS_DIR` defaults to `<repo>/data/sessions`, resolved from `__file__`, not from the cwd.
  - It is configurable only through a constructor argument, so tests can use `tmp_path`.
  - Validate `id` with `uuid.UUID(id)` and assert that the resolved dir is inside `SESSIONS_DIR`.

**Clock**
- `t0`: `time.monotonic()` anchor.
- Set `t0` when the first audio frame reaches the recorder, which is WAV sample 0.
  - Minimal acceptable mechanism: a trivial pass-through `FrameProcessor` placed immediately before `audio_buffer`, recording `monotonic()` on the first `InputAudioRawFrame`/`OutputAudioRawFrame` seen while recording.
  - Or an equivalent verified approach.
- Before audio starts, fall back to the session creation time and document the fallback.
- `rel_ms()` returns `int((monotonic() - t0) * 1000)`.

**Content and state**
- `transcript`: list of `{"role": "user"|"assistant", "text": str, "timestampMs": int}`, ordered by append. Do not invent turn pairing; turnIds are deferred to Gate C.
- `audio`: the final interleaved PCM bytes, `sample_rate` and `num_channels`, set by the `on_audio_data` handler.
- `finalized`: bool. `finalize()` is idempotent, guarded by the flag plus an `asyncio.Lock` or equivalent.

## STORAGE SCHEMA
`data/sessions/<uuid>/session.json`:
```json
{
  "id": "<uuid>",
  "startedAt": "2026-09-24T12:00:00.000Z",
  "durationMs": 25000,
  "recording": { "file": "recording.wav", "sampleRate": 24000, "channels": 2,
                 "channelLayout": ["user", "bot"] },
  "transcript": [
    { "role": "user", "text": "...", "timestampMs": 4200 },
    { "role": "assistant", "text": "...", "timestampMs": 6100 }
  ]
}
```
- `durationMs` is the WAV duration: frames / sampleRate × 1000, as an integer.
- If no recording was produced, `recording` is `null` and `durationMs` falls back to the session clock. The JSON must never advertise a WAV that doesn't exist.
- No latency or freeze fields.

## FINALIZATION ORDER
1. `on_client_disconnected`:
   - `await audio_buffer.stop_recording()` first, so the final data is emitted before any cancellation.
   - Then `await runner.cancel()`. (`CancelFrame` also triggers `stop_recording`, but it is a no-op if already stopped.)
2. The `on_audio_data` handler task stores the bytes in `Session`.
3. `on_assistant_turn_stopped` for any in-flight turn fires on `CancelFrame` and appends the entry.
4. `await runner.run()` returns, after pipeline cleanup has awaited the processors' pending event-handler tasks. Verify this in source (`BaseObject.cleanup` / worker cleanup). If it's not guaranteed, explicitly await the pending handler work in `Session` before finalizing.
5. `await session.finalize()` runs in a `try/finally` around `runner.run()` so it also runs on errors:
   - a. `mkdir` the session dir.
   - b. If audio is present:
     - write the WAV with stdlib `wave` to `recording.wav.tmp`
     - `os.replace` it to `recording.wav`
   - c. Build the JSON. The `recording` block is set only if step b succeeded.
   - d. Write the JSON to `session.json.tmp`, flush it, `os.fsync` it, then `os.replace` it to `session.json`.
   - e. Set `finalized=True` and log the session dir path (no secrets).
   - A second call is a no-op.
   - A WAV write error is logged clearly and results in `recording: null`, with the error surfaced in the logs.
   - A JSON write error is logged and re-raised or logged loudly, with no `.tmp` left masquerading as the final file.

## ACCEPTANCE CRITERIA
1. Exactly one backend UUID per voice session.
2. Artifacts only under `data/sessions/<uuid>/`.
3. A completed session creates `recording.wav`.
4. The WAV is structurally valid and playable (stdlib `wave` readable).
5. The recording contains both user and assistant audio in a normal session.
6. Stereo, with user=L (ch0) and bot=R (ch1), if verified reliable.
7. The channel layout is documented (JSON `channelLayout` + code comment) and tested.
8. Bot audio is recorded after `transport.output()`, satisfying the freeze invariant.
9. Finalized user transcript entries are persisted.
10. Interim Deepgram fragments are not persisted.
11. Finalized assistant entries are persisted independently of the recording audio.
12. Timestamps are session-relative monotonic integer ms.
13. `durationMs` is persisted.
14. `startedAt` is UTC ISO-8601.
15. Finalization is idempotent.
16. Disconnect does not truncate the recording.
17. Disconnect does not drop already-finalized transcript entries.
18. `session.json` is written only after the recording result is known.
19. JSON is written atomically (tmp + replace).
20. No client-supplied path or ID is used. No new file-serving endpoint in Gate B.
21. No Gate C+ functionality.
22–24. Zero real Gemini, Deepgram or Cartesia calls in automated tests.
25. Gate A backend tests still pass.
26. Frontend tests, typecheck, lint and build still pass (the frontend should be untouched).
27. `data/`, `*.wav`, transcripts and `.env` remain untracked.

## IN SCOPE
- `server/session.py` (new)
- `server/bot.py` wiring
- a `SESSIONS_DIR` constant in `server/config.py`
- `server/tests/test_session.py` (new)
- brief README note on where artifacts land

## OUT OF SCOPE
- Latency measurement or storage.
- `UserBotLatencyObserver`.
- `FreezeGate`, or any freeze injection or detection.
- RMS analysis.
- Review UI, API endpoints for sessions, or file serving.
- Database, cloud storage, auth, deployment.
- FFmpeg.
- New dependencies.
- Frontend changes.
- Status/PR docs (the orchestrator owns those).

## SECURITY
- UUIDs are generated server-side only, and paths are built server-side and asserted inside `SESSIONS_DIR`.
- No secrets are logged. Don't log transcript text beyond what the existing observers already do.
- Tests use synthetic, non-sensitive text and audio, and write only under `tmp_path`.
- Never read or print `server/.env`.

## REQUIRED AUTOMATED TESTS
All tests are deterministic, with no network. Do not instantiate real provider services (no Deepgram, Gemini or Cartesia objects that could connect).

**A. Session creation**
- A valid UUID4 is created.
- The dir is exactly `SESSIONS_DIR/<uuid>`.
- A non-UUID id is rejected.

**B. Transcript**
- Final user text is appended.
- Empty/`None` content is skipped.
- Assistant text is appended.
- Timestamps are non-decreasing ints relative to `t0` (patch `time.monotonic` or inject a clock).
- A final-only integration check: drive a real `LLMUserAggregator` (from an `LLMContextAggregatorPair` with a trivial/no VAD setup, if feasible), or at minimum assert against the event contract, that `InterimTranscriptionFrame` does not produce an entry. If driving the real aggregator is impractical, say so and test the handler contract instead.

**C. Recording**
- Run a real `AudioBufferProcessor(sample_rate=24000, num_channels=2)` in a Pipecat test pipeline (Pipecat ships test utilities, e.g. `pipecat.tests.utils.run_test`, if present in 1.11.0; verify) with synthetic `InputAudioRawFrame` (a tone) and `OutputAudioRawFrame` (a different tone).
- Finalize, then open the file with `wave`:
  - `nchannels == 2`
  - `framerate == 24000`
  - `sampwidth == 2`
  - left (ch0) non-zero where user audio was fed
  - right (ch1) non-zero where bot audio was fed

**D. Freeze-invariant proof**
- Same harness, but with a test-only processor placed before a fake output stage that drops `OutputAudioRawFrame`s, and `TextFrame`/assistant events still flowing.
- Assert that the right channel is all-zero (or no bot audio) while an assistant transcript entry exists.
- If faithfully simulating `transport.output()` is impractical, document exactly what the test proves, and rely on the verified `base_output.py` push-after-write behavior.

**E. Finalization**
- It writes the WAV and the JSON.
- The JSON references the existing WAV, and the WAV duration matches `durationMs` (±1 ms).
- A second `finalize()` doesn't change the files (mtime/bytes).
- A simulated WAV write failure gives `recording: null` and no `recording.wav`.
- A simulated JSON write failure leaves no `session.json`.
- No audio gives `recording: null`.

**F.** The Gate A suite (`uv run pytest`) passes in full.

## HUMAN E2E TEST
The orchestrator runs this after QA, with one short real-provider session:
1. Greeting.
2. "Hi, can you hear me?"
3. "What is the largest planet?"
4. Disconnect.
5. Inspect the artifacts.

## RETURN FORMAT
Software engineer: the `software-engineer.md` format (STATUS / SUMMARY / FILES CHANGED / COMMANDS / RESULTS / RISKS / QA SHOULD VERIFY). Include:
- the exact source locations you verified
- how `t0` is anchored
- the finalization sequence as implemented
