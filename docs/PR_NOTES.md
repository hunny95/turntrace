## Summary

TurnTrace is a voice-agent session inspector. This PR adds:

- A realtime Pipecat voice bot the browser talks to: Deepgram STT → Gemini → Cartesia TTS, over SmallWebRTC.
- Per-session persistence: a stereo recording plus a finalized transcript.
- Per-turn latency: from the moment the user stopped speaking to the first bot audio sent to the browser.
- A deterministic fault injector. The first two replies play normally, then all bot audio is dropped for the rest of the call while the pipeline keeps running.
- A post-call freeze detector that works only from the recording and transcript, never from simulator state.
- A Next.js review UI that shows playback, waveforms, latency spans, the detected freeze region and the transcript on one timeline.

## Implementation approach

The work was built and verified gate by gate:

1. The realtime voice path.
2. Session artifacts.
3. Latency.
4. Freeze injection.
5. Independent detection.
6. The review UI.

Each gate got automated tests, an independent QA pass and a real-session check before it was committed. Pipecat primitives are used wherever they exist: the SmallWebRTC transport, Silero VAD with Smart Turn, the Deepgram, Gemini and Cartesia services, and `AudioBufferProcessor`. The only custom pieces are:

- a small freeze-gate processor
- a clock/latency processor
- a turn tracker
- the offline detector

## Architecture

```text
Browser (Next.js, @pipecat-ai/client-js + small-webrtc-transport)
        ⇅ SmallWebRTC
Pipecat 1.11 backend (Python 3.12, FastAPI)
  transport.input() → Deepgram STT → user aggregator (Silero VAD + Smart Turn)
  → failed-turn context cleanup → Gemini (gemini-3.6-flash) → Cartesia TTS
  → FreezeGate → transport.output() → session clock / latency tracker
  → AudioBufferProcessor (stereo) → assistant aggregator

On disconnect:  recording.wav + session.json → freeze_detector → analysis.json
Review:         GET /api/sessions[/{id}[/recording]] → Next.js /sessions/[id]
```

- **Credentials:** provider keys are read only by the backend from `server/.env`.
- **CORS:** restricted to the configured frontend origin.
- **Error handling:** a provider error during a turn shows a recoverable notice and keeps the call connected. Transport failures and user disconnects share a single teardown path. Transient Gemini 5xx errors are retried with the SDK's own retry mechanism (3 attempts) while the stream is being set up.
- **Failed turns:** if a Gemini request finally fails, its unanswered user message is removed from the LLM context, so a later reply never answers a stale question. The message stays in the stored transcript.

## Recording and session model

- **Layout:** `data/sessions/<uuid>/` holds `recording.wav`, `session.json` and `analysis.json`. The directory is local and git-ignored. The server generates the UUID, and every path goes through one shared helper that keeps it inside `data/sessions`.
- **Recording:** `AudioBufferProcessor` writes a stereo 24 kHz 16-bit WAV, user on the left and bot on the right. It sits *after* `transport.output()`. In Pipecat 1.11 the output transport forwards a bot audio frame only after it has written that frame to the client, so the bot channel holds what was actually sent, not what TTS generated.
- **Clock:** one session-relative monotonic clock in integer milliseconds. Zero is the first audio frame reaching the recorder, which is WAV sample 0, so transcript, latency and freeze timestamps all line up with the recording.
- **Transcript:** finalized turns only, from the user and assistant aggregator events. Interim STT text is never stored. Assistant text is stored whether or not its audio played.
- **Finalization:**
  1. Stop the recording.
  2. Cancel the pipeline.
  3. Wait for the pipeline cleanup, which drains the recorder's handlers.
  4. Write the WAV and then `session.json`, each atomically (temp file + rename).

  Finalization is idempotent.
- **Schema:** `{id, startedAt, durationMs, recording: {file, sampleRate, channels, channelLayout} | null, transcript: [{role, text, timestampMs, turnId}], latencies: [{turnId, userStopMs, botStartMs, latencyMs}]}`.

## Latency measurement

- **Start (`userStopMs`):** `VADUserStoppedSpeakingFrame.timestamp − stop_secs`, which is when the user physically stopped speaking. It comes before the turn is released, so the VAD stop delay, the Smart Turn / endpointing wait and STT finalization all count.
- **End (`botStartMs`):** the first bot `OutputAudioRawFrame` after `transport.output()` following `BotStartedSpeakingFrame`. This is the same frame the recorder captures.
- **Where timestamps are taken:** inside the pipeline, not in an observer. Observers receive frames through async queues, so their clock reads lag.
- **Turn correlation:** `server/turn_tracker.py` gives each finalized user turn an integer `turnId`. Only the newest turn can be waiting for audio. It stops waiting when:
  - the LLM fails
  - a new user turn starts
  - its latency is recorded

  The greeting has `turnId: null` and no latency. A turn whose audio never plays gets no latency, even if its text exists, so no latency is ever made up.

## Freeze simulation

- **What:** `server/freeze_gate.py` sits after Cartesia TTS and before `transport.output()`.
- **Config:** `FREEZE_AFTER_ASSISTANT_TURNS=2` is the default; `0` disables it.
- **Counting:** one unit per user-triggered reply, counted at its first bot audio frame. The greeting doesn't count, and neither does a provider failure or an empty reply.
- **Behavior:** replies 1–2 play. The gate freezes before forwarding reply 3's first frame. From then on every bot audio frame is **dropped**, permanently, with no timer, randomness or recovery.
- **Everything else still runs:** text and control frames continue, so STT, the LLM and the assistant transcript keep working. Frozen turns keep their text and `turnId`, and have no latency.
- **Nothing is persisted:** no threshold, flag, activation time or marker is written to any artifact. One backend log line marks activation, for humans only.

## Independent freeze detection

`server/freeze_detector.py` reads only `recording.wav` and `session.json` (`durationMs`, recording metadata, transcript). It imports neither `freeze_gate` nor `config`, and an AST test enforces that. A QA run with `freeze_gate.py` removed gave the identical result.

- **Response windows:** each user-associated assistant reply is examined over a window. The window starts at the user turn's timestamp and ends at the next later-turn user timestamp, or at session end.
- **Audio evidence:** bot-channel RMS over 20 ms frames. The threshold is `max(150, min(p20 noise floor, 250) × 4)`, capped at 1000. A reply counts as audible if it has at least 200 ms above the threshold.
- **A `bot_audio_freeze` is reported only when all of these hold:**
  - an earlier reply was audible
  - a later reply has assistant text but no audio
  - every reply after it is also silent, so the audio never recovers
  - there is transcript activity after the first silent reply
- **Region:** `startMs` is that first silent reply's persisted transcript timestamp, an observed point rather than the simulator's hidden activation time. `endMs` is `durationMs`.
- **Output:** `analysis.json` is written atomically after finalization and is deterministic, so it can be re-run with `uv run python -m freeze_detector <uuid>`. If detection fails, it writes `status: "error"`, which is never shown as a negative result.

## Review UI

- **Read-only API** (`server/session_api.py`):
  - `GET /api/sessions`
  - `GET /api/sessions/{id}`, which includes the analysis or an explicit missing/error state
  - `GET /api/sessions/{id}/recording`, a fixed filename served with `FileResponse`, byte ranges supported

  Only canonical lowercase UUIDs are accepted. There are no filename parameters and no mutation routes, and no filesystem paths are returned.
- **Pages:** `/sessions` (list with freeze badges) and `/sessions/[id]`.
- **Playback:** the WAV is fetched once. A single Blob URL feeds the `<audio>` element, and a copy of the bytes is decoded with Web Audio into USER and BOT waveform lanes.
- **Timeline:** latency spans run from `userStopMs` to `botStartMs`. The freeze region is shaded over both lanes, with an evidence summary. The playhead moves with playback, and the timeline is seekable.
- **Transcript:** timestamped and clickable to seek. "No bot audio observed" appears only on turns the detector marked silent.
- **Dependencies:** none added. It uses React, CSS modules, Canvas and Web Audio only.

## Approaches considered / trade-offs

- **SmallWebRTC vs streaming PCM over a WebSocket:** WebRTC uses the browser's own media stack (getUserMedia echo cancellation, jitter buffering, Opus) with no custom code. A WebSocket PCM stream would mean rebuilding those.
- **Stereo vs mixed recording:** separate channels let the detector measure bot energy directly. A mono mix would need source separation.
- **Pipecat `UserBotLatencyObserver` vs a turn-aware tracker:** the native observer measures the same physical points. However, it emits a bare number with no turn identity or timestamps, and it stays armed after a failed turn, so a later reply could be charged to an unanswered question. The custom tracker keeps the same semantics and adds turn identity. It never invents a latency.
- **Physical speech end vs turn release:** starting at turn release would hide the endpointing wait, which the user does experience.
- **Recording emitted audio vs generated TTS audio:** tapping TTS would record audio the user never heard, which would hide an output failure. Recording after the output transport makes the WAV show exactly what was sent.
- **Dropping audio vs forwarding silent frames:** silent frames still trigger "bot started speaking" and would produce a false latency. Muting after the recorder would leave audio in the recording that the user never heard. Dropping frames before the output keeps the browser, recording and latency consistent.
- **Counting at first audio vs at response start:** Gemini's service announces a response before its request succeeds, so counting at the start would let provider errors use up the threshold.
- **Simulator state vs independent evidence:** reading the gate's flag would make detection trivial and meaningless. The detector sees only what a real incident would leave behind, and an import-boundary test keeps the two apart.
- **Deterministic RMS + transcript rules vs an ML/LLM detector:** a binary "was there bot audio" question on a clean TTS channel doesn't need a model. Rules are reproducible, explainable and free. Only windows backed by the transcript are examined, so trailing silence and pauses can never count. Requiring persistence and continuation gives up some recall (a final silent reply followed by hang-up isn't reported) in exchange for no false positives on the known negatives.
- **Separate `analysis.json` vs changing `session.json`:** the raw evidence stays untouched, and the derived result can be regenerated.
- **Local filesystem vs a database or cloud storage:** a single-machine inspector needs neither, and plain files are easy to inspect.
- **Web Audio + Canvas waveform vs a waveform library:** the WAV is already fetched for playback, so decoding it once avoids a dependency, a second derived artifact and another endpoint.

## Testing

**Automated** (no provider calls):
- backend `uv run pytest`: 147 passed
- frontend `npm test` (`node --test`): 39 passed
- `tsc --noEmit`, ESLint and `next build`: all clean

The tests cover:
- path safety and UUID validation
- final-only transcripts
- a real `AudioBufferProcessor` driven with synthetic PCM (checked channel separation)
- finalization idempotence and write failures
- latency tracker edge cases: greeting, failed turn, superseded turn, late VAD stop, text without audio
- freeze gate counting, pass-through and no silent substitutes
- detector positives and negatives: trailing silence, recovery, never-worked bot, noise, amplitude range, high speech occupancy, swapped layout, error paths
- the import boundary
- session API security: path leakage, byte ranges and 416, no mutation routes
- UI timeline, waveform and media-state helpers

**Defects found by verification**, each fixed and covered by a regression test or a retest:
- **Independent QA:** a late VAD stop could drop a turn's id.
- **Independent QA:** a speech-heavy bot channel could raise the noise floor enough to hide real speech. Fixed by capping the noise estimate.
- **Independent QA:** an older session without a `latencies` key failed to render. The API now defaults it to `[]`.
- **Human testing in Chrome:** playback stalled at about 10.8 s because a Blob URL was revoked too early.
- **Final review:** uppercase UUIDs were accepted by the path helper. Ids must now match the canonical lowercase form exactly.

**Real sessions:**
| Session | Result |
|---|---|
| Frozen (`3d804728…`, ~174 s) | Replies 1–2 were audible (2296 ms and 7902 ms latency). Bot RMS was 0 from reply 3 on, and assistant text continued. **Detected:** region 38.4 s → end. |
| Normal (`085a9d47…`) | Two turns, 2378 ms and 2387 ms. **Not detected.** |
| Provider failure + recovery (`44fafc59…`) | A Gemini 503 left a user turn with no reply, then the conversation recovered. **Not detected.** |

All three render correctly in the review UI, and playback runs past two minutes with seeking.

## Known limitations

- The detector's RMS threshold is tuned to this pipeline's TTS output. A much quieter voice or a gain change could need re-tuning.
- A silent final reply followed immediately by hang-up is intentionally not reported.
- Heavy barge-in can make turn association ambiguous, both for the freeze count and for detection windows.
- Assigning a `turnId` to the assistant reply relies on a timing margin, not a formal Pipecat ordering guarantee. No full-pipeline async ordering harness exists.
- Recordings are held in memory until the call ends, and the review UI loads the whole WAV. That is fine for sessions of a few minutes.
- The Gemini retry policy is unit-tested against simulated errors, not a live 5xx. Gemini latency varies widely, and free-tier quota can return 429s.
- The freeze region starts at the first silent reply's transcript timestamp. That is an observed point, and it can begin a few seconds after bot audio was actually expected (about 4 s in the frozen test session).
- The local API relies on binding to localhost and on CORS. It has no Host-header check and no auth.
- The backend runs at DEBUG log level, and its logs contain conversation text (never credentials).

## Future improvements

- Break latency down into endpointing, STT, LLM TTFB, TTS TTFB and transport.
- A stronger turn-correlation primitive, such as an explicit turn id carried on frames, instead of a timing margin.
- More adaptive or calibrated audio detection, such as a per-voice noise and speech profile, plus more incident types: partial dropouts, stutter and latency spikes.
- Streaming or on-disk recording for long sessions, and range-based playback instead of loading the whole WAV.
- Durable or cloud session storage with retention controls.
- Authentication and access control for the session API.
- Production observability (metrics and traces) and a deployment story.
