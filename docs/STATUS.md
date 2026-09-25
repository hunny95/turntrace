# TurnTrace Status

## Branch
Expected implementation branch: `feat/voice-session-inspector`

## Current gate
Gates A–G complete. PR #1 is ready for review (not merged).

## Gates
- [x] A — realtime voice path works for several turns
- [x] B — completed session creates transcript + playable recording
- [x] C — per-turn user-to-bot latency captured
- [x] D — deterministic permanent bot-audio freeze injection works
- [x] E — independent post-call freeze detection works; trailing silence is not a freeze
- [x] F — Next.js review UI shows playback, transcript, latency, freeze region
- [x] G — final tests/build/security/PR notes/demo instructions complete

## Last verified result — Gate G: PASS

- **Final read-only review** (principal/security/realtime) found no code blockers. The only blocker was the stale PR body, resolved by this update. Follow-ups applied:
  - `path_safety.py` now requires the exact canonical lowercase UUID. An uppercase id used to be accepted: it served the recording on case-insensitive disks and returned a 500 on the detail route. Regression tests were added.
  - The `bot.py` comment on turn-id ordering now states the real margin, which is asyncio scheduling across processor queue hops, not Gemini TTFB. `GoogleLLMService` pushes `LLMFullResponseStartFrame` before the request (verified in the installed source).
  - README and PR notes: Node 22.18+, runnable command blocks, correct echo-cancellation attribution and defect attribution, and added limitations (freeze region start, localhost-only API, DEBUG logs contain conversation text).
- **Requirement audit:** every requirement passes against the code, tests and real sessions.
- **Automated checks:**
  - backend pytest 147/147
  - frontend `node --test` 39/39
  - `tsc --noEmit`, ESLint and `next build` clean
  - `git diff --check` clean
  - no provider calls
- **Local smoke** (no new voice session), in headless Chrome against the local servers:
  - `/`, `/sessions` and all three stored sessions load.
  - Frozen session:
    - Playback ran from 9.5 s through 19.4 s, past the old 10.8 s stall. A seek to 130 s kept playing.
    - Both waveform lanes painted.
    - Two latency spans, the freeze region from 00:38.4 to the end, freeze details, and the transcript with "No bot audio observed" on T3, T4 and T6.
  - Normal session: 2 latencies, no freeze region.
  - Older provider-failure session: loads, with no latencies and no freeze.
  - Page errors: none.
- **Hygiene:**
  - No tracked `.env`, recordings, session or analysis JSON, `data/`, dependency or build directories, or transcript exports, and none in history.
  - No secret patterns in tracked files or history.
  - No absolute local paths in tracked files.
  - The public-term check is clean.
- **Deferred non-blocking items:**
  - CORS `allow_credentials=True` is unused.
  - No Host-header check (localhost tool).
  - `pyproject.toml` placeholder description.
  - Unused create-next-app SVGs in `web/public`.

## Earlier verified result — Gate F: PASS

### Read-only session API (`server/session_api.py`, FastAPI `APIRouter`)
- `GET /api/sessions`: summaries, newest first by `startedAt`. Only UUID-named directories with a valid `session.json` whose `id` matches are listed; everything else is skipped. Fields: `id, startedAt, durationMs, turnCount, latencyCount, hasRecording, analysisStatus (ok|missing|error), freezeDetected (bool|null), freezeStartMs`. No transcripts, no paths.
- `GET /api/sessions/{id}`: `session.json` content, with `recording.file` removed and `latencies` defaulted to `[]` for pre-Gate-C sessions, plus `analysis` (`analysis.json` as stored, `{status:"missing"}`, or `{status:"error", error:"invalid_analysis_json"}` if unreadable). Read-only; never re-runs analysis.
- `GET /api/sessions/{id}/recording`: fixed `recording.wav` only, `audio/wav`, via Starlette 1.7.0's built-in `FileResponse` (byte ranges supported).
- No mutation routes (POST/PUT/PATCH/DELETE → 405) and no filename or path parameters.
- **Path safety:** one shared helper, `server/path_safety.py` (stdlib only), used by the API, `session.py` and `freeze_detector.py`. It accepts canonical UUIDs only and requires the resolved path to stay inside the sessions root. The detector still imports neither `config` nor `freeze_gate`.
- The router does not import `config`, `bot` or provider modules. Tests inject a temporary sessions root.

### Review UI (`/sessions`, `/sessions/[id]`)
- Shared top bar (TurnTrace · Voice Agent Session Inspector; Live / Sessions). The live call page at `/` is unchanged apart from the nav and a shared API-URL constant.
- Sidebar: newest-first list with start time, duration and a freeze badge (detected / none / not analyzed / analysis failed).
- Summary: duration, start time, transcript turns, measured latency count, average measured latency (from persisted values), freeze status.
- **Playback:** the WAV is fetched once. One Blob object URL backs a single `<audio>` element, and a separate copy of the bytes is decoded with Web Audio for the waveform. Controls: play/pause, time readout, 1× / 1.5× / 2×, Space to toggle.
- **Waveform:** USER and BOT lanes mapped from `recording.channelLayout`. Peaks come from the pure helper `computePeaks`, drawn on one shared scale with a 0.05 full-scale floor, so near-silence stays flat. Canvas is DPR-aware and redrawn on resize.
- **Timeline:** full recording duration; click or arrow keys (±5 s) to seek (`role="slider"`); a playhead updated by `requestAnimationFrame` while playing.
- **Latency overlay:** one span per persisted latency, from `userStopMs/durationMs` to `botStartMs/durationMs`, labelled `T{turnId} · x.xx s` from `latencyMs`. Nothing is derived from transcript timestamps.
- **Freeze overlay:** only when `analysis.status == "ok"` and `freeze.detected`. It spans `startMs` to `endMs`, labelled "Detected bot-audio freeze", translucent so the waveform stays visible. A details block shows start, first affected turn, "persists to end of session", and evidence turn lists from `analysis.json`.
- **Transcript:** chronological rows with `mm:ss.s`, role, `TURN n` when present, `LATENCY x.xx s` for the matching `turnId`, and "No bot audio observed" only for turns in `evidence.silentAssistantTurnIds`. Clicking a row seeks to `timestampMs − 500 ms` (clamped at 0). The row at or before the playhead is highlighted.
- **Analysis states:** "Freeze detected", "No persistent freeze detected", "Not analyzed", "Analysis failed". Missing or error is never shown as "no freeze".
- **Robustness:** a runtime response parser (no `any`) turns malformed data into a handled error. A failed waveform decode or missing recording leaves the metadata, overlays and transcript usable.
- No new npm or Python dependencies; plain CSS modules, React and Web Audio.

### QA defect 1 and repair: older session missing `latencies`
- **Defect:** the real Gate B session `44fafc59…` has no `latencies` key. The detail endpoint passed that through and the strict frontend parser rejected it, so the session showed an error instead of rendering.
- **Repair:** the detail endpoint returns `latencies: []` when the key is absent, and a regression test was added. The real session then parsed with 4 turns, no latencies, no freeze.

### Human-found defect 2 and repair: playback stalled at ~00:10.8
- **Symptom (desktop Chrome, frozen session):** audio and `currentTime` stopped at about 10.8 s while the button still showed "Pause".
- **Root cause:** a frontend Blob URL lifetime bug in `SessionDetailView.tsx`.
  - A separate `useEffect(() => () => revoke(objectUrlRef.current), [audioState])` ran its cleanup on every `audioState` change.
  - The load effect set the ref to the new URL, then set `audioState` to `ready`. The cleanup left over from the `loading` render then revoked the URL the `<audio>` element had just received.
  - `src` is assigned at commit, before passive cleanups run, so the element had already started reading and played what it had buffered, then could not continue.
  - About 10.8 s is roughly 1 MiB of 24 kHz stereo 16-bit PCM (96,000 B/s). That is consistent with buffering evidence; Chrome's internal chunk size was not verified.
- **Backend exonerated** against the real endpoint (orchestrator curl and independent QA):
  - the full response is 200, `accept-ranges: bytes`, and byte-identical to the source WAV
  - `bytes=0-1048575`, `1048576-2097151`, `15000000-15999999` and `16700000-` each return 206 with the correct `Content-Range`/`Content-Length` and bytes identical to the source
  - an unsatisfiable range returns 416
  - no backend recording code was changed
- **Repair:**
  - The recording-load effect (deps `[hasRecording, id]`) creates the URL, keeps it in a local variable, and revokes it only in its own cleanup, on recording replacement or unmount.
  - The session view is keyed by id, so switching sessions revokes the old URL.
  - Play state, playhead, speed, transcript highlighting and rerenders never touch it.
- **Media state:** playback status now follows native media events through the pure helper `web/src/lib/playback.ts`:
  - `play` → buffering until `playing`
  - `waiting`/`stalled` while active → "Buffering…"
  - `pause`/`ended` → stopped
  - `error` → "Playback error"
  - `timeupdate` keeps the time readout in sync
- **Verification:**
  - Independent QA in headless Chromium, against the real app:
    - playback reached 32 s
    - seeks to 40, 100 and 160 s resumed
    - one object URL and one recording fetch per session view
    - a forced media error showed "Playback error"
  - **Headless Chromium did not reproduce the original pre-fix stall**, so the root cause rests on code inspection plus the human desktop-Chrome reproduction.
  - A human retest in the same desktop Chrome passed.

### Human validation (desktop Chrome, zero provider calls)
- **Frozen `3d804728…`:**
  - continuous playback went past 10.8 s, 15 s, 30 s, the freeze start (≈ 00:38.4), 1:00, 1:20 and 2:00; late seeks reached ≈ 02:28 of 02:54.4
  - no Buffering or Playback error appeared
  - separate USER and BOT lanes; latency spans ≈ 2.30 s and 7.90 s; freeze region from ≈ 00:38.4 to the end
  - bot lane flat while later user activity continues
  - assistant text after the freeze, with silent turns marked from analysis evidence
  - 1× / 1.5× / 2×, timeline seek and transcript seek (including the late entry at ≈ 02:22.1) worked
- **Normal and older sessions:** covered by the earlier human pass and QA; both load and play.
- **Non-blocking polish noted:** latency labels are slightly crowded because both intervals fall early in a long session; inline transcript annotations could use a little more spacing from the sentence text; the freeze-region treatment is intentionally strong.

### Automated / QA
- Backend pytest 145/145 (97 before Gate F):
  - 48 session-API tests: listing, ordering, malformed directories, invalid, non-canonical and traversal ids, 404s, analysis detected / negative / missing / error / malformed, no `file` or absolute paths in responses, exact recording bytes, first / second / later / open-ended byte ranges, 416, filename query ignored, mutation methods 405, older session without `latencies`
- Frontend `node --test` 39/39: time formatting, span and freeze positioning and clamping, analysis states, older entries without `turnId`, latency lookup by `turnId`, silent indicator from evidence only, `computePeaks` (bins, silence flat, tone peaks, noise floor), summary counts and average, seek offset, active row, parser rejection of malformed payloads, media-state transitions.
- `tsc`, ESLint, `next build` clean. `.next/static` contains no provider key names, no `data/sessions` and no local paths.
- **Independent QA answers:**
  - latency overlaid at its correct interval on the recording timeline: yes (T1 4.29–5.60 %, T2 10.17–14.70 %)
  - freeze region aligned to `startMs`–`endMs` on the same timeline: yes (22.01–100 %)
  - frozen session shows user activity with a flat bot lane: yes (bot peak exactly 0 from 39 s to the end)
  - older pre-`turnId` session renders and isn't labelled frozen: yes (after repair 1)
  - arbitrary local files readable through the API: no

### Known limitations
- The whole WAV is fetched into memory for playback and decoding, which is fine for local sessions of a few minutes.
- There is no automated DOM/React test of the object-URL lifetime; it is covered by code structure, QA browser checks and the human retest.
- Latency labels can crowd when intervals are close together on a long timeline.

## Earlier verified result — Gate E: PASS

### Simulation vs detection
- **Simulation (Gate D):** creates the condition, meaning bot output audio is dropped from a certain point onward.
- **Detection (this gate):** `server/freeze_detector.py` analyzes only ordinary persisted evidence after the call ends. It does not import `freeze_gate` or `config`, does not read `FREEZE_AFTER_ASSISTANT_TURNS`, logs or simulator state, and no simulator marker exists in any artifact.

### Detection definition (observational label `bot_audio_freeze`)
A freeze is reported only when all of the following hold:
- **Earlier audio:** at least one earlier user-associated assistant response has meaningful bot audio. The greeting doesn't count.
- **Silent response:** a later assistant response has transcript text but no meaningful bot audio in its response window.
- **Persistence:** every later assistant response is also silent, so bot audio never recovers.
- **Continuation:** some transcript entry (user or assistant) comes strictly after that first silent response.

Each response window runs from that turn's first user entry to the next later-turn user entry, or to `durationMs`. A user turn with no assistant entry, such as a provider failure, can never start a freeze region.

### Audio analysis
- **Channel:** the bot channel is taken from `recording.channelLayout`. Channel count, sample rate and sample width are validated against the WAV, and a mismatch is an explicit error.
- **Framing:** bot RMS is computed in 20 ms frames.
- **Threshold:** `max(150, min(p20 noise floor, 250) × 4)`, so it can never exceed 1000 RMS.
- **Audible:** a response is audible if at least 200 ms of frames are above the threshold.
- Exact zeros are not required, and no ML, LLM or network calls are used.

### Output
- **File:** `data/sessions/<uuid>/analysis.json`. It is written atomically after `session.finalize()` succeeds, the call runs off the event loop, and exceptions are logged and swallowed. Raw artifacts are never modified.
- **Status:** `status: "ok"` carries a `freeze` block. On failure it is `status: "error"` with a short code and no `freeze` key.
- **Region:** `startTurnId` is the first silent response. `startMs` is that assistant entry's `timestampMs`, an evidence-backed observed point and not the simulator's activation time. `endMs` is `durationMs`.
- **CLI:** `cd server && uv run python -m freeze_detector <uuid> [--no-write]`, which accepts a UUID only.
- **Older sessions:** pre-Gate-C sessions have no `turnId`. Each assistant entry is paired with the most recent preceding user entry, and turns are numbered 1-based by user entry.

### QA defect and repair (one cycle)
- **Defect:** first QA found that the p20 noise floor over the whole bot channel rises to speech level when the bot speaks in more than about 80% of frames. Real bot speech was then classified as silent, a false negative.
- **Repair:** the noise floor is now capped at 250 RMS, and a regression test was added.
- **Re-verification:** a scoped independent QA re-check passed. It covered 95% bot-speech occupancy, constant hiss, digital silence, small noise, loud speech and low-amplitude (700) speech.

### Real-session validation (human + QA, zero provider calls)
- `3d804728…` (Gate D session): `detected: true`, `startTurnId: 3`, `startMs: 38397`, `endMs: 174418`. Audible before: [1, 2]; silent: [3, 4, 6]; continuation: [4, 5, 6].
- `085a9d47…` (Gate C normal session): `detected: false` (`no_silent_assistant_response_after_audible`).
- `44fafc59…` (Gate B session with a 503 and recovery, older format): `detected: false`. The failed user turn has no assistant entry and so cannot start a freeze region.

### Automated / QA
- **Backend:** pytest 97/97, including 34 Gate E tests covering:
  - normal calls, trailing silence, provider failure, transient silence followed by recovery
  - a final silent response with no continuation, and a bot that never worked
  - a positive case and its region, noise and amplitude cases, dominant bot speech
  - swapped channel layout and six error paths
  - atomic and idempotent writes that leave raw files untouched
  - older sessions without `turnId`
  - an import-boundary AST test, and a subprocess check that no provider modules are loaded
- **Frontend:** tests 5/5; typecheck, lint and build are clean.
- **QA:** an independent QA mutation check showed the boundary test catches an injected `import config`. An isolation run with `freeze_gate.py`, `config.py` and `session.py` unavailable gave the identical decision.

### Known limitations
- **Threshold tuning:** the threshold is a deterministic heuristic tuned to this pipeline, whose bot channel is TTS output plus digital idle silence. It is not a universal speech detector. A future voice or gain change that makes genuine speech sustain an RMS below about 1000 could require re-tuning.
- **Final silent response:** one that isn't followed by any later activity is intentionally not classified, to avoid false positives.
- **Window boundaries:** windows depend on transcript timestamps. Heavy overlap, such as barge-in while previous audio drains, could attribute audio to a neighbouring window.

## Earlier verified result — Gate D: PASS

### Simulation vs detection
- **Simulation (this gate):** the backend deterministically and permanently drops bot output audio after the configured number of user-triggered assistant responses.
- **Detection:** not implemented yet. Gate E will detect a freeze independently from ordinary persisted session evidence (recording + transcript + timing).

### Design (verified against installed Pipecat 1.11.0 source)
- **Placement:** `… llm → tts → freeze_gate → transport.output() → clock_anchor → audio_buffer → assistant_aggregator`. Backend-only (`server/freeze_gate.py`); a fresh gate per session, no module-level state, so every new call starts unfrozen.
- **Config:** `FREEZE_AFTER_ASSISTANT_TURNS` (default 2). `0` disables; negative or non-integer values fail config validation, naming only the variable.
- **Counting:**
  - At `LLMFullResponseStartFrame` (which the TTS service serializes in order ahead of that response's audio) the gate snapshots the pending user turn id from `TurnTracker` (new read-only `pending_turn_id`).
  - A response counts once, at its first bot audio frame, and only if it answers a user turn. The proactive greeting (no pending turn) never counts.
  - `GoogleLLMService` pushes `LLMFullResponseStartFrame` before the request, so a failed or empty reply still produces a start/end pair; counting at first audio means provider failures and empty replies do not consume the threshold.
- **Suppression:**
  - When the count exceeds the threshold, the gate freezes before forwarding that first frame, so the entire third response is silent. The state never unfreezes.
  - Frozen `OutputAudioRawFrame`s (incl. `TTSAudioRawFrame`) are dropped, never replaced with silence. No audio reaches the output transport, so no `BotStartedSpeakingFrame`, no browser audio, no recorded bot audio, and no Gate C latency.
  - All text/control frames pass unchanged, so the assistant transcript survives. Cartesia runs with `pause_frame_processing=False`, so it never waits on bot-stopped-speaking signals that dropped audio would withhold.
- **Detector independence:** no threshold, flag, activation time, marker or sidecar is written to any artifact. The only trace is one backend info log line on activation, which is not an artifact.
- Details: `.claude/tasks/005-gate-d-freeze-injection.md`.

### Human end-to-end test (one real session)
- Session `3d804728-…`: `durationMs` 174418 vs WAV 174419 ms; stereo, 24 kHz, 16-bit, layout user/bot.
- The greeting did not count. Turns 1–2 had bot audio and latency; from turn 3 onward bot-channel RMS was 0 for the rest of the call.

  | Turn | Assistant text | Latency (ms) | Bot RMS, 7 s after |
  | --- | --- | --- | --- |
  | 1 | present | 2296 | ≈ 3000 |
  | 2 | present | 7902 | ≈ 2442 |
  | 3 | "Two plus two equals four." | none | 0 |
  | 4 | "As I mentioned, two plus two is four." | none | 0 |
  | 6 | "The sky is typically blue during a clear day." | none | 0 |

- User audio continued on the user channel after the freeze (RMS ≈ 900–1400 on turns 3–6).
- The final question was split by turn detection into turn 5 ("What is the") and turn 6 ("color of sky?"). This is Smart Turn segmentation, not a Gate D defect, and it was not tuned here. Turn 6 still produced assistant text while audio stayed suppressed, which is further evidence that the pipeline stayed alive.
- `session.json` top-level keys: `durationMs, id, latencies, recording, startedAt, transcript`. The session directory holds only `recording.wav` and `session.json`. The local verifier script returned PASS.

### Automated / QA
- Backend pytest 63/63, including 15 Gate D tests in `test_freeze_gate.py`:
  - greeting not counted; turns 1–2 audible
  - turn 3 dropped from its first frame; turns 4–5 dropped
  - text/control/interruption pass-through while frozen; no silent substitute frames
  - new session starts unfrozen; failed/empty reply does not count
  - Gate C chain: frozen turn keeps text + `turnId` with no latency
  - Gate B chain: real `AudioBufferProcessor` keeps user audio and pre-freeze bot audio, silent bot channel after
  - `session.json` denylist for freeze/simulator terms; config validation; no provider services constructed
- Independent QA: PASS. Answers: a triggering-turn audio frame can reach `transport.output()`: no; later user speech still produces persisted assistant text: yes; Gate E could read a hidden flag instead of evidence: no; a frozen turn has text + `turnId` with no audio and no latency: yes.

### Known limitation
- If the user starts speaking while the previous response's audio is still draining, the user-turn start can clear the pending turn before the next response's start frame reaches the gate, so that response's turn association (and therefore the count) can be ambiguous. QA verified that once frozen, no later bot audio can leak regardless. The scripted human test avoided overlap by waiting between turns.

## Earlier verified result — Gate C: PASS

### Metric
User-to-bot latency is measured from actual user speech end to first emitted bot audio.
- `userStopMs` = `VADUserStoppedSpeakingFrame.timestamp - stop_secs`, which is the physical end of speech, converted to the session clock. This includes the VAD stop delay, the Smart Turn / endpointing wait, STT finalization, LLM, TTS, and output scheduling.
- `botStartMs` = the first bot `OutputAudioRawFrame` observed after `transport.output()`, following a `BotStartedSpeakingFrame`. The output transport forwards an audio frame only after writing it to the client, so this is the first emitted audio, and it is the same frame the recorder captures.
- `latencyMs = botStartMs - userStopMs`, on the Gate B recording-aligned session clock.

### Design (verified against installed Pipecat 1.11.0 source)
- **Native observer:** `UserBotLatencyObserver` uses the same start and end points: VAD stop minus `stop_secs`, and `BotStartedSpeakingFrame`. It is not used directly:
  - it emits only a bare latency value, with no turn identity or timestamps
  - it stays armed after a failed turn until the next VAD start
- **Timestamps:** taken inside the pipeline by the existing post-output `SessionClockAnchor`, not in a `BaseObserver`. Observers are fed through per-observer asyncio queues (`pipeline/worker_observer.py`), which delays their clock reads.
- **Turn tracker:** `server/turn_tracker.py` is framework-free.
  - Each finalized user turn gets an integer `turnId`.
  - At most one turn is pending bot audio. It is cleared by:
    - an LLM failure (`FailedLLMTurnContextCleanup` `on_llm_failed` callback, same `ErrorFrame` detection point)
    - a new `UserStartedSpeakingFrame`
    - its own measurement
  - The assistant entry takes the pending turn's id at `on_assistant_turn_started`.
  - The proactive greeting has `turnId: null` and no latency.
- **Schema:** each transcript entry gains `turnId`. A top-level `latencies: [{turnId, userStopMs, botStartMs, latencyMs}]` is added. Transcript `timestampMs` keeps its Gate B event-time meaning.
- Details: `.claude/tasks/004-gate-c-turn-latency.md`.

### QA defect and repair (one cycle)
- **Defect:** QA found that a finalized user turn lost its `turnId` if its VAD-stop frame reached the tap after finalization. Its assistant entry was then labelled `null`.
- **Repair:**
  - A finalized turn always keeps its id.
  - A late VAD stop attaches only if it is at or before that turn's finalization time, so later noise cannot shorten an earlier turn's latency.
  - A turn with no valid VAD stop gets no latency.
  - Three regression tests were added.
- QA re-verified: PASS.

### Human end-to-end test (one real session)
- Session `085a9d47-…`: `durationMs` 28600 equals the WAV length; stereo, 24 kHz, 16-bit.
- Greeting: `turnId: null`, no latency.
- User and assistant entries for turns 1 and 2 share their ids. No duplicate latency records.
- Latencies (`latencyMs = botStartMs − userStopMs`, all values within `durationMs`):

  | Turn | userStopMs | botStartMs | latencyMs |
  | --- | --- | --- | --- |
  | 1 | 8925 | 11303 | 2378 |
  | 2 | 18645 | 21032 | 2387 |

- Turn 1 physical stop (8925) precedes the finalized-transcript event (9370). The latency is measured from speech end, not from finalization.
- Bot-channel RMS in the 300 ms before / after `botStartMs`: 313 → 6819 (turn 1), 18 → 5178 (turn 2).
- The verifier script returned PASS.

### Automated / QA
- Backend pytest 48/48, including 16 Gate C tests in `test_turn_latency.py` plus the updated session tests:
  - normal turn with exact values
  - greeting
  - failure followed by success, both with and without an `ErrorFrame`
  - superseded turn
  - duplicate audio
  - endpointing inclusion
  - persisted JSON
  - pass-through through `run_test`
  - failed-turn transcript
  - no provider services constructed
  - Gate D precursor
  - late or missing VAD stop
- Frontend 5/5, `tsc`, ESLint and `next build` pass.
- Zero real provider calls in tests.
- Independent QA answers:
  1. The Smart Turn / endpointing wait is included: yes.
  2. A response after a failed 503 can be attributed to the failed turn: no.
  3. A turn whose audio is suppressed before the output will have no latency even though its assistant text carries the `turnId`: yes.

### Accepted residual risks
- Pipecat pushes the LLM request before the `on_user_turn_stopped` handler task that allocates the `turnId` runs (`llm_response_universal.py` `_maybe_emit_user_turn_stopped`). The association with the assistant turn therefore relies on a timing margin, not a formal ordering guarantee:
  - the handler does no I/O
  - a real model response needs a network round trip
  - this is documented in `bot.py`
- There is no automated harness for these async orderings through the real pipeline; the tests drive the tracker in a fixed order.
- Extremely unusual startup congestion could, in theory, make `userStopMs` negative (there is no clamp). The live test did not show this.

## Earlier verified result — Gate B: PASS

### Design (verified against installed Pipecat 1.11.0 source)
- Recording:
  - Pipecat `AudioBufferProcessor(sample_rate=24000, num_channels=2)`
  - user = left (ch0), bot = right (ch1)
- Pipeline placement: `… tts → transport.output() → clock anchor → AudioBufferProcessor → assistant aggregator`.
  - The output transport forwards a bot audio frame downstream only after it has been written to the client.
  - The recorded bot channel is therefore emitted audio. Audio suppressed upstream of the output (Gate D) will record as silence.
- Transcript:
  - The finalized user text comes from `on_user_turn_stopped`, and the assistant text from `on_assistant_turn_stopped`.
  - Interim STT fragments are never persisted.
  - Assistant text is independent of whether bot audio was emitted.
  - A user turn that failed at the provider stays in the persisted transcript even though it is removed from the LLM context.
- Clock:
  - Session-relative monotonic integer ms.
  - t0 is anchored to the first audio frame reaching the recorder, which is WAV sample 0.
  - `startedAt` is UTC ISO-8601.
- Storage:
  - Artifacts go to `data/sessions/<uuid>/recording.wav` and `session.json`.
  - The UUID is generated by the server, and the path is validated to stay inside `data/sessions`.
- Finalization:
  1. Recording stops on disconnect, before the pipeline is cancelled.
  2. `runner.run()` returns only after pipeline cleanup has awaited the pending event handlers.
  3. The WAV is written (tmp + replace), then the JSON (tmp + fsync + replace).
  4. The JSON sets `recording: null` if the WAV write failed.
  5. Finalization is idempotent.
- Details: `.claude/tasks/003-gate-b-session-artifacts.md`.

### Human end-to-end test (one real session)
- Artifacts:
  - `recording.wav` and `session.json` were created.
  - The `session.json` id matches the directory UUID.
  - `durationMs` 129662 matches the WAV length of 129.66 s.
- WAV format:
  - 2 channels, 24000 Hz, 16-bit.
  - Per-window RMS shows clean separation: user speech on L with R at 0; bot speech on R with L near 0.
- Playback:
  - Both sides are audible with `afplay`.
  - The final bot response is complete, not truncated.
- Transcript:
  - Finalized user and assistant turns appear in order, with no interim fragments.
  - Timestamps are non-negative, ordered, within `durationMs`, and consistent with the audio regions.
- Real provider failures during the session were reflected faithfully:
  - an initial LLM completion timeout
  - a real Gemini 503 on one user turn: the user entry was kept, no assistant entry or audio was fabricated, and later turns succeeded
- Disconnect was clean and finalization succeeded.
- `data/` is ignored and untracked (`git check-ignore`, `git ls-files data`).

### Automated / QA
- backend pytest 31/31 (10 Gate A + 21 Gate B)
  - The Gate B tests drive a real `AudioBufferProcessor` via `pipecat.tests.utils.run_test`, with synthetic PCM.
  - Stereo WAVs are checked with stdlib `wave`.
  - A test-only drop processor proves a silent bot channel while text still flows.
  - Finalization is tested for idempotence, failure paths, and atomic JSON.
- frontend 5/5, `tsc`, ESLint, `next build` pass.
- Zero real provider calls in tests.
- Independent QA re-verified the push-after-write behavior and the cleanup ordering in the Pipecat source.
  - It answered yes to: "will a future pre-output FreezeGate show bot silence while the assistant transcript still has text?"

## Earlier verified result — Gate A: PASS

### Human end-to-end test
Browser mic → SmallWebRTC → Pipecat → Deepgram → Gemini → Cartesia → browser speaker.
- Connect, mic permission, and multi-turn conversation on one connection worked, well beyond three turns.
- Follow-up context held (closest planet → "which one is the largest").
- Disconnect was clean.

Observed latency (recorded only; tuning deferred):
- Deepgram STT TTFB ≈ 0.5–0.8 s.
- Cartesia TTFB/first audio ≈ 0.1–0.3 s.
- Gemini TTFB ≈ 1.4–4.6 s typically, ≈ 7 s once.
- Smart Turn occasionally waited its ≈ 3 s incomplete-utterance fallback.

### Real Gemini 503 incident → lifecycle fix
- Symptom: Gemini returned 503 UNAVAILABLE. Pipecat marked the error non-fatal, but the frontend showed "Connection error" / Connect without disconnecting. The mic and pipeline stayed live, and a stale answer was spoken later.
- Fix:
  - connection status now follows the transport lifecycle, and non-fatal provider errors show a recoverable notice
  - a single teardown path (client disconnect → mic tracks stopped → bot audio detached; late events from old clients ignored)
  - bounded Gemini retry for 500/502/503/504 only
  - removal of the failed turn's unanswered user message(s) from context
- Details: `.claude/tasks/002-gate-a-error-lifecycle-fix.md`.

### Real Gemini 429 (free-tier quota) validation
429 RESOURCE_EXHAUSTED occurred during extended testing:
- the UI stayed connected with Disconnect available
- the notice "Assistant temporarily unavailable. Please try again." appeared, with no false "Connection error"
- manual Disconnect shut down the session and pipeline cleanly

429 is intentionally not retried.

### Controlled lifecycle test (`GEMINI_MODEL=turntrace-invalid-model`)
- The deterministic 404 NOT_FOUND left the UI connected with the notice shown.
- After Disconnect: UI disconnected, mic capture stopped, no further transcription, no delayed bot speech.
- Note: an invalid model makes Pipecat mark `GoogleLLMService` unusable. This validates truthful UI/teardown behavior, not in-conversation recovery.

### Automated / QA (engineer → independent QA)
- Checks:
  - backend pytest 10/10
  - frontend session tests 5/5
  - `tsc --noEmit`, ESLint, and `next build` pass
  - `/api/health` 200
- Pipecat 1.11.0 and google-genai streaming-retry paths source-inspected.
- QA found a race: speech arriving during retry could be dropped by cleanup. Fixed by scoping cleanup to the failed request's messages; regression test added.
- Security:
  - provider keys are backend-only; none appear in web source or build
  - `server/.env` is ignored and untracked
  - CORS is restricted to `FRONTEND_ORIGIN`
- No Gate B+ functionality introduced.

## Current blockers
None.

## Rule
Only the orchestrator updates this file after QA evidence.
