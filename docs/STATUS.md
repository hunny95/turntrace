# TurnTrace Status

## Branch
Expected implementation branch: `feat/voice-session-inspector`

## Current gate
Gate A complete. Next: Gate B — session recording and transcript persistence.

## Gates
- [x] A — realtime voice path works for several turns
- [ ] B — completed session creates transcript + playable recording
- [ ] C — per-turn user-to-bot latency captured
- [ ] D — deterministic permanent bot-audio freeze injection works
- [ ] E — independent post-call freeze detection works; trailing silence is not a freeze
- [ ] F — Next.js review UI shows playback, transcript, latency, freeze region
- [ ] G — final tests/build/security/PR notes/demo instructions complete

## Last verified result — Gate A: PASS

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
