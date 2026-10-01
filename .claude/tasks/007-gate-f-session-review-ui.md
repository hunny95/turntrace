# Task 007 — Gate F: Session Review UI

## GOAL
A Next.js review experience that lets a reviewer browse saved sessions, play a recording, see user/bot waveforms on one shared timeline, see per-turn latency spans and the detected freeze region aligned on that timeline, and read/seek a timestamped transcript — backed by the smallest safe read-only backend API.

## WHY NOW
Gates A–E produce `recording.wav`, `session.json` (transcript + latencies) and `analysis.json` (freeze detection). Gate F is the required visual review surface. Zero provider calls: all work uses already-persisted sessions.

## READ-ONLY SESSION API
Implement as a FastAPI `APIRouter` in a new module (e.g. `server/session_api.py`) included by `server.py`. The router must not import `config` at module import time in a way that triggers `check_required_env_vars()` for tests; make the sessions root injectable (e.g. a module-level default from the same `SESSIONS_DIR` the Gate B/E code uses, overridable via FastAPI dependency or a factory) so tests use `tmp_path`. Do not import `freeze_gate`, bot, or provider modules.

- `GET /api/sessions` → `SessionSummary[]`, newest first by `startedAt` (tie-break by id). Only UUID-named directories with a readable, valid `session.json` whose `id` matches the directory; everything else silently skipped. Fields: `id, startedAt, durationMs, turnCount, latencyCount, hasRecording, analysisStatus ("ok"|"missing"|"error"), freezeDetected (bool|null), freezeStartMs (int|null)`. `turnCount` = number of distinct user turns (count user entries' distinct `turnId`s when present; for legacy sessions without `turnId`, count user entries). Document the rule. No transcripts, no paths.
- `GET /api/sessions/{session_id}` → `{ session: <session.json content minus nothing path-like>, analysis: {status:"missing"} | <analysis.json content> }`. `recording.file` must NOT be returned (replace the recording block with `{sampleRate, channels, channelLayout}` or `null`). Unreadable/malformed `analysis.json` → `{status:"error", error:"invalid_analysis_json"}`, never a negative result. 400/422 for invalid id, 404 for unknown session, 500-class with a short safe code for malformed `session.json`. Never re-run or write analysis.
- `GET /api/sessions/{session_id}/recording` → fixed `recording.wav` only, `audio/wav`, via Starlette `FileResponse` (range support is fine; verify installed behavior, don't hand-roll streaming). 404 with a short message if missing.
- No delete/upload/mutation, no filename/path parameters, no query params that affect file selection.

## PATH SAFETY
Reuse the existing UUID validation + resolved-inside-root check (`freeze_detector.resolve_session_dir` or `Session._resolve_session_dir` logic). If reuse would require importing `config`/provider code, extract one shared helper and have existing callers use it — do NOT create a second, weaker implementation. Normalize: accept only canonical `uuid.UUID(x)` round-trips (reject non-canonical forms such as braces/urn/uppercase-without-hyphens if `str(uuid.UUID(x)) != x.lower()`), then resolve and assert `is_relative_to(root)`. Error bodies never include filesystem paths.

## FRONTEND DATA MODEL
`web/src/lib/sessionTypes.ts` (or similar): `SessionSummary`, `SessionDetail`, `RecordingMetadata`, `TranscriptEntry` (`turnId?: number | null`), `LatencyRecord`, `AnalysisResult` (discriminated union: ok+freeze | error | missing), `FreezeResult` (detected true with region+evidence | detected false with reason/detail). No `any`. A small runtime validator/parser for API responses so a malformed response yields a handled error state, not a crash.

API base URL: reuse the existing `NEXT_PUBLIC_TURNTRACE_API_URL ?? "http://localhost:7860"` pattern from `page.tsx` (move to a shared lib constant). The frontend must never reference `data/sessions` or any filesystem path.

## PAGE / ROUTE DESIGN
- Keep the live call page at `/` working unchanged in behavior; add a slim shared top bar (TurnTrace · Voice Agent Session Inspector, nav: Live, Sessions) in the layout or a shared component.
- `/sessions` — sidebar list + empty detail prompt ("Select a session").
- `/sessions/[id]` — sidebar list + detail. Read `node_modules/next/dist/docs/` for this Next.js version's dynamic route / `params` conventions before writing (see `web/AGENTS.md`). Client components for interactivity are fine; data comes from the backend at runtime (client-side fetch is acceptable; the backend is local).
- Layout per the Gate F brief: header bar; left session list (short id, start time, duration, freeze badge); right: session header + summary stats, timeline, freeze details, transcript.

## RECORDING PLAYBACK
Fetch the WAV once as an `ArrayBuffer`; create an object URL (revoke on change/unmount) for a single `HTMLAudioElement`; decode a copy with `AudioContext.decodeAudioData` (or `OfflineAudioContext`) for waveform peaks. Controls: play/pause, current time / total, seek, speed 1× / 1.5× / 2×. Keyboard: Space toggles play when focus isn't in an input. Timeline duration = `session.durationMs` (fall back to audio duration if absent).

## WAVEFORM DESIGN
Pure helper `computePeaks(channelData: Float32Array[], bins: number)` in `web/src/lib/` returning per-channel per-bin max-abs (and/or RMS) values. Map channels by `recording.channelLayout` (user/bot), never by assumed index. Normalization: one shared scale for both lanes = max(global peak, a fixed floor such as 0.05 full-scale) so near-zero noise stays visually flat and frozen sections are flat. Render with canvas or SVG, two labeled lanes (USER, BOT), device-pixel-ratio aware if canvas. Recompute bins on container resize (ResizeObserver) cheaply from the already-decoded data. If decoding fails: show an inline "Waveform unavailable" in the lanes; overlays, playback controls (if audio loads) and transcript still work.

## LATENCY OVERLAY
For every persisted latency `{turnId, userStopMs, botStartMs, latencyMs}`: a span on the timeline from `userStopMs/durationMs` to `botStartMs/durationMs` (clamped to [0,1]), across/adjacent to the waveform lanes, with a marker at each edge and a label `T{turnId} · 2.30 s` (from `latencyMs`, not recomputed). Pure helper `spanPercent(startMs, endMs, durationMs)`. Never derive latency from transcript timestamps.

## FREEZE OVERLAY
Only when `analysis.status === "ok" && freeze.detected === true`: a translucent, hatched or tinted region from `startMs/durationMs` to `endMs/durationMs` over both lanes, labeled "Detected bot-audio freeze". The waveform must remain visible beneath. Uses `analysis.json` only.

## TRANSCRIPT
Chronological by `timestampMs` (stable). Each row: `mm:ss.s` timestamp, role (USER / ASSISTANT), `TURN n` when `turnId` is a number, text. Assistant rows show `LATENCY x.xx s` when a latency with matching `turnId` exists. "No bot audio observed" indicator only when the entry is an assistant entry and its `turnId` is in `analysis.freeze.evidence.silentAssistantTurnIds` of a detected freeze — never inferred from missing latency. Clicking a row (a button, keyboard accessible) seeks to `max(0, timestampMs − 500)` ms (documented + tested pure helper `transcriptSeekMs`). Highlight the active row = last entry with `timestampMs <= currentMs` (pure helper, tested).

Note on assistant timestamps: assistant `timestampMs` is finalization time (≈ end of that audio); this is why the seek offset exists; do not change backend semantics.

## ANALYSIS STATES
Pure helper `analysisState(analysis)` → `"detected" | "negative" | "missing" | "error"`. Labels: "Freeze detected", "No persistent freeze detected", "Not analyzed", "Analysis failed" (with short error code). Missing/error must never be rendered as "no freeze". Session list badges use the same mapping from summary fields.

## FREEZE DETAILS
For detected freezes: started (`mm:ss.s`), first affected turn, persists to end of session (when `endMs >= durationMs`), evidence: earlier audible turns, silent assistant turns, continued activity turns. Only fields present in `analysis.json`.

## SESSION SUMMARY
Duration, started at (local time), transcript turns (same rule as backend `turnCount`, via pure helper), measured latencies count, average measured latency (mean of persisted `latencyMs`, "—" when none), freeze status. No invented metrics.

## ERROR STATES
Loading skeleton/text; no sessions ("No saved sessions yet — run a call on the Live page"); backend unreachable; session 404; malformed response; missing recording (detail still renders; timeline shows overlays on an empty lane with a notice); waveform decode failure; analysis missing/error. One failure must not blank the page.

## ACCESSIBILITY
Semantic buttons for list items, transcript rows, play/pause, speed; visible focus rings; timeline has `role="slider"` with `aria-valuemin/max/now` + arrow-key seek (±5 s); color is never the only signal (freeze and latency have text labels); sufficient contrast on the dark theme.

## DESIGN DIRECTION
Dark, restrained observability tool. Monospace for timestamps/ids/numbers (Geist Mono already loaded). Neutral greys, one accent for user lane, one for bot lane, amber for latency spans, red/hatched for freeze. Dense but aligned; no hero, no gradients, no gratuitous pills/cards/animation. Plain CSS modules / existing `globals.css`; no UI, chart, waveform or state libraries.

## ACCEPTANCE CRITERIA
Backend: 1 safe list API; 2 safe detail API; 3 safe recording endpoint; 4 UUID/traversal protections tested; 5 no filesystem paths returned; 6 missing ≠ negative; 7 error ≠ negative; 8 no mutation endpoints.
Frontend: 9 live page preserved; 10 Sessions nav; 11 newest-first list; 12 selection loads persisted data; 13 playable recording; 14 user & bot lanes; 15 timeline = full duration; 16 click/keyboard seek; 17 visible moving playhead (rAF while playing, cancelled on pause/unmount); 18 latency spans from persisted userStopMs→botStartMs on the timeline; 19 human-readable latency labels; 20 freeze region from analysis startMs→endMs on the timeline; 21 freeze persists to recording end; 22 chronological transcript; 23 timestamps; 24 turn ids when present; 25 legacy entries without turnId render; 26 transcript click seeks; 27 freeze evidence shown; 28 normal session says "No persistent freeze detected"; 29 missing → "Not analyzed"; 30 error → "Analysis failed"; 31 usable loading/error/empty states; 32 no keys/paths in frontend source or bundle; 33 Gate A–E backend tests pass; 34 zero provider calls.

## IN SCOPE
- New `server/session_api.py` (or similar) + `include_router` in `server.py`; shared path helper extraction only if needed.
- New `server/tests/test_session_api.py` using FastAPI `TestClient` with tmp session fixtures (synthetic tiny WAVs).
- Web: shared nav, `/sessions`, `/sessions/[id]`, components, CSS modules, `web/src/lib/*` pure helpers + `node --test` tests.
- Short README section: how to open the review UI.

## OUT OF SCOPE
New voice calls, changes to bot pipeline / FreezeGate / detector logic / session schema, re-running analysis from the API, auth, DB, deployment, new npm/Python dependencies (FastAPI TestClient needs `httpx`; if absent, add it as a dev-only dependency and report it), `docs/STATUS.md` / `docs/PR_NOTES.md`, git operations.

## SECURITY / PRIVACY
Keys backend-only; no `NEXT_PUBLIC_*` secrets. No paths in responses/errors. Fixed filename. UUID-validated ids. CORS unchanged (restricted to `FRONTEND_ORIGIN`; GET is covered). No real session UUIDs, transcripts or recordings in source, tests or fixtures. `data/` stays ignored.

## REQUIRED AUTOMATED TESTS
Backend (pytest, tmp root): list; newest-first; malformed dirs ignored (non-UUID name, missing/invalid session.json, id mismatch, file instead of dir); invalid UUID rejected; non-canonical UUID forms rejected; unknown UUID 404; traversal strings (`..`, `%2e%2e%2f…`, encoded slashes) never reach files; detail; analysis detected / negative / missing / error / malformed-json; `recording.file` absent from responses and no absolute path anywhere in any JSON body; recording 200 `audio/wav` with exact bytes; missing recording 404; no route accepts a filename (e.g. `/recording?file=session.json` still returns WAV or ignores param; `/api/sessions/{id}/session.json` 404/405); no mutation methods (POST/DELETE → 405).
Frontend (node --test, fixtures): A `formatMs(38397)` → `"00:38.4"`; B span 20000–25000 of 100000 → 20%–25%; C freeze 40000–100000 → 40%–100%; clamping; D analysis states ×4; E entry without turnId → no turn label, no crash; F latency matched by turnId; G silent indicator only from evidence (assistant without latency but not in evidence → not silent); H `computePeaks` two channels, bin count, silence ≈ 0, tone → peaks, noise floor keeps tiny noise flat; I summary: turn count (with/without turnIds), latency count, average; transcript seek offset; active-row selection; response parser rejects malformed payloads.
Then: backend full pytest, `npm test`, `npm run typecheck`, `npm run lint`, `npm run build`, `git diff --check`. Grep the build output (`.next/static`) for provider key names and `data/sessions`.

## HUMAN UI TEST
Orchestrator-run after QA. Frozen session (freeze from 00:38.4 to end, latency T1 2.30 s, T2 7.90 s, bot lane flat after onset, user lane active), normal control (two spans, no freeze, "No persistent freeze detected"), legacy provider-failure control (no turn labels, no freeze). Selected from the list, not hard-coded.

## RETURN FORMAT
Per `.claude/agents/software-engineer.md`: STATUS, SUMMARY, FILES CHANGED, COMMANDS / TESTS RUN, RESULTS, RISKS / BLOCKERS, QA SHOULD VERIFY.
