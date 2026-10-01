# Task 006 — Gate E: Independent Post-Call Bot-Audio Freeze Detection

## GOAL
Add a deterministic, post-call detector that reads only ordinary persisted session artifacts (`recording.wav` + `session.json`) and decides whether the bot's output audio disappeared persistently during an otherwise continuing conversation. It writes a small derived `analysis.json` next to the raw artifacts and returns a freeze region that Gate F can overlay on the recording timeline.

## WHY NOW
Gates A–D give us a live voice path, trustworthy recording and transcript, per-turn latency, and a real session in which bot audio was deliberately suppressed. Gate E proves that condition can be found from evidence alone. Gate F (UI) needs its output. No UI work in this task.

## DETECTOR INDEPENDENCE (mandatory)
The detector module(s) must NOT import, read, or reference:
- `freeze_gate` / `FreezeGate` / `server/freeze_gate.py`
- `FREEZE_AFTER_ASSISTANT_TURNS` or any simulator env var / config value (do not import `config` at all from the detector)
- backend logs, freeze activation logs, simulator state or timestamps
- `TurnTracker` runtime state (the detector reads persisted JSON only)

Do not add any simulator marker to `session.json` and do not create a simulator sidecar file. The detector must produce the same decision if `server/freeze_gate.py` were deleted after the recording was written. Add a test that asserts this boundary (e.g. static scan of the detector source for forbidden names, and import of the detector with `freeze_gate` blocked in `sys.modules`).

## OBSERVATIONAL DEFINITION
Label: `bot_audio_freeze`. Meaning: "assistant responses continue to exist while emitted bot audio disappears persistently during an otherwise continuing conversation." It is an observation, not a root-cause diagnosis. Never claim a simulator activated.

## CURRENT ARTIFACT SEMANTICS (verified by orchestrator on real sessions)
- `session.json`: `durationMs`, `recording {file, sampleRate, channels, channelLayout}`, `transcript[] {turnId?, role, text, timestampMs}`, `latencies[]`.
- `recording.wav`: 16-bit PCM, 24 kHz, 2 channels interleaved, layout `["user","bot"]`.
- Transcript `timestampMs` is session-relative and is written when the aggregator finalizes the entry. For assistant entries this is typically at or after the END of that response's bot audio (e.g. bot audio 9.86–13.94 s, assistant ts 13.80 s). So a window that starts at the assistant timestamp would miss real audio.
- The greeting is an assistant entry with `turnId: null` (not user-associated).
- **Legacy sessions (Gate B, e.g. `44fafc59…`) have no `turnId` key at all.** The detector must handle this: fall back to ordering, pairing each assistant entry with the most recent preceding user entry; assistant entries before any user entry are the greeting.
- Several user entries may exist with no assistant reply (provider failure, or a fragment such as turn 5 "What is the" in `3d804728…`).
- Transport idle silence is recorded as digital zeros on the bot channel, but the detector must not depend on exact zeros.

## AUDIO ANALYSIS
- Standard library only (`wave`, `array`, `math`); no new dependencies, no ML, no network.
- Validate: WAV channel count == `recording.channels` == `len(channelLayout)`; sample rate == `recording.sampleRate`; sample width == 2. Resolve the bot channel as `channelLayout.index("bot")`. Missing/invalid/duplicate layout, missing `"bot"`, or any mismatch → raise an explicit analysis error (never guess channel 1).
- Compute bot-channel RMS in fixed non-overlapping frames (20 ms).
- Documented adaptive threshold, e.g.:
  - `noise_floor` = a low percentile (e.g. 20th) of all bot frame RMS values in this recording
  - `speech_threshold = max(ABS_MIN_RMS, noise_floor * NOISE_MULTIPLIER)` with `ABS_MIN_RMS` ≈ 150 (~ −47 dBFS) and multiplier ≈ 4 (+12 dB). Tune only if tests/real sessions demand it and document why.
- A response window is **audible** iff it contains at least `MIN_VOICED_MS` (≈ 200 ms) of frames above `speech_threshold`. This tolerates low noise, tiny residual energy and leading/trailing silence, and ignores clicks.
- Keep constants named at module top with a one-line rationale each.

## TURN RESPONSE WINDOWS
For each user-associated assistant response (assistant entry with non-null turnId, or legacy-paired):
- `start` = timestamp of the FIRST user entry of that turn (user finalization time; real bot audio begins after it).
- `end` = timestamp of the next user entry that belongs to a later turn, or `durationMs` if none.
- Clamp to `[0, durationMs]`; an empty/negative window is treated as not audible but must not crash.
- Classify each response window as audible or silent.
Do not scan the recording for arbitrary silence. Silence only matters inside windows where an assistant transcript proves a response existed.

## EARLIER AUDIBLE BASELINE
A freeze requires at least one user-associated assistant response classified audible BEFORE the onset. The greeting does not count as baseline (it is not a user-associated response). A call whose bot never produced audible responses is not a mid-call freeze.

## PERSISTENCE RULE
Let `L` = the last audible user-associated assistant response (by turn order). Onset = the first assistant response after `L`. By construction all responses after `L` are silent, so any audible later response (recovery) simply moves `L` forward and removes the onset. Silent responses before `L` are transient anomalies, not a persistent freeze.

## CONTINUATION RULE
Require evidence the session stayed active after the onset: at least one transcript entry (user or assistant) whose `timestampMs` is strictly greater than the onset assistant entry's `timestampMs` (equivalently a later turn). Without it → no freeze (insufficient persistence evidence). This intentionally declines to classify a final silent response immediately followed by hang-up; document that trade-off.

## TRAILING-SILENCE RULE
Silence after the last audible response, with no later assistant transcript expecting audio, is never a freeze. Long natural pauses between turns are never examined as freeze evidence because no assistant response expects audio there.

## FREEZE REGION SEMANTICS
On detection:
- `startTurnId` = onset turn id (legacy sessions: 1-based ordinal of user-associated responses; note in docs).
- `startMs` = onset assistant entry's `timestampMs` — "the first evidence-backed silent assistant-response point, not the simulator's hidden activation timestamp."
- `endMs` = `durationMs` (condition persists through end of recording).

## ANALYSIS SCHEMA (`data/sessions/<uuid>/analysis.json`)
Success, positive:
```json
{
  "version": 1,
  "status": "ok",
  "freeze": {
    "detected": true,
    "label": "bot_audio_freeze",
    "reason": "persistent_assistant_text_without_bot_audio",
    "startMs": 38397,
    "endMs": 174418,
    "startTurnId": 3,
    "evidence": {
      "audibleAssistantTurnIdsBefore": [1, 2],
      "silentAssistantTurnIds": [3, 4, 6],
      "continuationTurnIds": [4, 5, 6]
    }
  }
}
```
Success, negative:
```json
{ "version": 1, "status": "ok",
  "freeze": { "detected": false, "reason": "no_persistent_bot_audio_freeze", "detail": "<short code>" } }
```
`detail` is one of a small documented set, e.g. `no_assistant_responses`, `no_silent_assistant_response_after_audible`, `no_audible_baseline`, `no_continuation_after_silent_response`. Negative evidence may also include `audibleAssistantTurnIds` / `silentAssistantTurnIds` (small integer lists only). No PCM arrays, no confidence score. Optional small `audio` block with `speechThresholdRms` and `frameMs` is fine.

Analysis failure (distinct from negative — never write `detected:false` on failure):
```json
{ "version": 1, "status": "error", "error": "<short safe code, e.g. invalid_channel_layout | missing_recording | wav_metadata_mismatch | invalid_session_json>" }
```
No `freeze` key on error. Error messages must not include absolute paths beyond the session dir or any secrets.

## POST-CALL LIFECYCLE
In `bot.py`, after `await session.finalize()` completes successfully (so `recording.wav` and `session.json` are fully written via atomic replace), run the detector off the event loop (`asyncio.to_thread`) wrapped in try/except that logs a concise message and never raises into / corrupts the session. Never touch or rewrite `recording.wav` or `session.json`. If finalize raised, do not run analysis. The detector reads only final files (never `*.tmp`).

## IDEMPOTENCE
Pure function over file contents → same result every run. Write `analysis.json` via `analysis.json.tmp` + flush + fsync + `os.replace`, cleaning the tmp on failure (mirror `Session._write_json`). Rerunning overwrites with identical content.

## CLI
Small re-analysis entry point, run from `server/`:
`uv run python -m freeze_detector <session-uuid>` (or an equivalently small script). It validates the argument as a UUID, builds the path server-side under the sessions dir (reuse `SESSIONS_DIR` from `session.py` only if it pulls in no simulator/config coupling; otherwise define the path locally), writes `analysis.json`, and prints the analysis JSON. Optional `--no-write` flag. Nonzero exit on analysis error. No arbitrary filesystem paths accepted.

## ACCEPTANCE CRITERIA
1. Detector uses recording/transcript evidence only; no FreezeGate/config/log dependency.
2. Bot channel resolved from `recording.channelLayout`; WAV metadata validated.
3. Meaningful-audio classification (adaptive RMS + min voiced duration), not exact-zero checks.
4. Earlier audible baseline required.
5. Assistant transcript without meaningful bot audio forms a freeze onset.
6. Condition persists through all later assistant responses; later recovery prevents classification.
7. Continuation after the onset required.
8. Trailing silence, provider failure without assistant text, transient silent response + recovery, final ambiguous silent response, and never-worked bot are all NOT freezes.
9. Positive returns `startTurnId`, `startMs` (onset assistant timestamp), `endMs = durationMs`.
10. `analysis.json` derived, small, atomic, idempotent; raw artifacts never mutated.
11. Failure (`status:"error"`) distinguishable from negative.
12. Legacy (no-turnId) sessions analyzed correctly.
13. Gates A–D tests still pass; zero provider calls; no UI.

## IN SCOPE
- New `server/freeze_detector.py` (single module: analysis + atomic write + `__main__` CLI).
- Minimal hook in `server/bot.py` after finalize.
- New `server/tests/test_freeze_detector.py` with synthetic WAV/JSON fixtures in `tmp_path`.
- Short README note on the CLI command (a few lines).

## OUT OF SCOPE
UI/Next.js changes, HTTP endpoints for analysis, changes to FreezeGate/TurnTracker/Session schema, new dependencies, docs/STATUS.md and docs/PR_NOTES.md (orchestrator owns), git operations.

## SECURITY / COST
Zero calls to Gemini/Deepgram/Cartesia in tests or CLI. The detector must not import provider modules or `config` (which checks env). No secret reads/prints. UUID-validated paths only. `analysis.json` stays under git-ignored `data/`.

## REQUIRED AUTOMATED TESTS
Synthetic fixtures: build stereo 16-bit WAVs with a helper that places user/bot tone bursts (e.g. sine) or noise at given ms ranges, plus matching session.json.
1. Normal call → no freeze.
2. Normal call + long trailing silence → no freeze.
3. Provider failure (user turn, no assistant, no bot audio) then recovery → no freeze.
4. One silent assistant turn followed by audible recovery → no freeze.
5. Final silent assistant turn with no later activity → no freeze (`no_continuation_after_silent_response`).
6. Bot audio absent from start (assistant texts exist) → no freeze (`no_audible_baseline`).
7. Persistent freeze → detected.
8. `startTurnId` is first silent assistant turn after last audible one.
9. `startMs` equals that assistant entry's `timestampMs`.
10. `endMs == durationMs`.
11. Low non-zero noise (e.g. RMS ~20–60 random/dither) in the frozen region still detects freeze.
12. Low-amplitude real speech-like signal (e.g. amplitude ~600–1000) is audible; test several amplitudes.
13. Swapped `channelLayout` `["bot","user"]` honored (bot audio placed on channel 0).
14. Missing/invalid/mismatched layout or WAV metadata → `status:"error"`, no `freeze` key.
15. Atomic idempotent write: two runs produce byte-identical `analysis.json`; no leftover `.tmp`; raw files unchanged (hash before/after).
16. Detector source has no reference to `freeze_gate`, `FreezeGate`, `FREEZE_AFTER_ASSISTANT_TURNS`, `config`; imports with `sys.modules["freeze_gate"] = None`.
17. No provider modules imported by the detector (check `sys.modules` for `pipecat.services`/`deepgram`/`cartesia`/`google.genai` absent after importing detector in a subprocess).
18. Legacy transcript without `turnId` keys analyzed via ordering.
Also add a test that the bot.py hook swallows detector exceptions (or keep the hook trivially small and cover the wrapper function).

## REAL-SESSION VALIDATION (local only, not committed)
Run the CLI with `--no-write` or normal write against:
- `3d804728-f9d5-4e73-ace3-93d7a8198f67` → detected, startTurnId 3, startMs 38397, endMs 174418.
- `085a9d47-38d6-4e7e-abe4-c3247314d4e9` → not detected.
- `44fafc59-5cb3-42be-bf5f-cee3086389d6` (legacy, no turnIds, 503 turn) → not detected.
Do not hard-code any of these values in production code.

## RETURN FORMAT
STATUS / SUMMARY / FILES CHANGED / COMMANDS / TESTS RUN / RESULTS (incl. real-session outputs) / RISKS / BLOCKERS / QA SHOULD VERIFY.
