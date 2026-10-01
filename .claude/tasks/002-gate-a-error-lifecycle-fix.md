# Task 002 — Gate A blocker: provider-error vs transport-state lifecycle

## GOAL
Make UI state truthful about the realtime session, and recover boundedly from transient Gemini 503s, so a non-fatal provider error can never leave a hidden live session behind a "disconnected/Connect" UI.

## WHY NOW
Human Gate A test failed. Gate A cannot pass while the UI can claim the session ended while the mic is still captured and the bot can speak later.

## HUMAN-OBSERVED FAILURE (evidence)
- Turn 3 ("And which one is the largest?") transcribed; Gemini returned `503 UNAVAILABLE` ("model is currently experiencing high demand").
- Pipecat pushed an `ErrorFrame` with `fatal: False`; RTVI forwarded it to the client as an error message.
- UI switched to status `error`, text "Connection error", button "Connect". No WebRTC disconnect happened.
- Over a minute later, while the user was talking to another app, VAD/Deepgram kept transcribing into the same context; Gemini then answered "Jupiter…" and the bot spoke while the UI showed error/Connect.
- Transport closed only when the page/connection finally closed (~2.5 min later).
- Latency notes (DO NOT tune in this task): Gemini TTFB ≈ 7.0s on the Mercury turn; Cartesia TTFB ≈ 0.12s / TTFA ≈ 0.25s; smart-turn 3s incomplete-utterance fallback on turn 3.

## ROOT CAUSE (verified by orchestrator from source)
1. `web/src/app/page.tsx` `onError` sets `status = "error"` for **every** RTVI error, ignoring `message.data.fatal`. `message.data` is an object (`{message, fatal}`), so the text falls back to "Connection error".
2. The button renders "Connect" whenever `status !== "connected"`, so a non-fatal error presents a disconnected UI **without calling `client.disconnect()`** — mic, transport, and bot pipeline stay live.
3. `@pipecat-ai/client-js` 1.13.1 itself only auto-disconnects when `data.fatal` is true (`dist/index.module.js` ~L1609). So for fatal:false, nothing tears the session down.
4. Clicking "Connect" in that state would create a second `PipecatClient` and overwrite `clientRef` without disconnecting the first (leaked live session).
5. Backend has no retry for Gemini 503: `GoogleLLMService` only offers `retry_on_timeout` (first-chunk timeout); the 503 surfaced via `push_error("Unknown error occurred: …")`.
6. The unanswered user turn stays in `LLMContext`; the next user speech triggers an LLM run over the whole context, which answered the old question.

## REQUIRED BEHAVIOR / DESIGN
### A. Frontend: transport state and provider errors are distinct
- Drive the connection status from the client's transport state (`onTransportStateChanged` / `RTVIEvent.TransportStateChanged`, types in `@pipecat-ai/client-js` `TransportState`) plus onConnected/onDisconnected — not from `onError`.
- `onError` with `data.fatal !== true`: keep status/buttons unchanged (still connected, Disconnect visible); show a separate, dismissible/auto-clearing notice: "Assistant temporarily unavailable. Please try again." Do not say "Connection error". Clear the notice when the bot next starts speaking or the user starts a new turn (use existing RTVI events; verify names in installed d.ts).
- `onError` with `data.fatal === true`: explicitly `await client.disconnect()` (idempotent with the library's own), then show disconnected + error text.
### B. Frontend: truthful media lifecycle
- Single teardown function used by Disconnect, fatal error, failed connect, onDisconnected, and unmount: calls `client.disconnect()`, nulls `clientRef`, clears `audioRef.srcObject`, and ignores any later events from that old client (guard by comparing to the current client instance).
- `handleConnect` must tear down any existing client first; never two live clients.
- Verify in `@pipecat-ai/small-webrtc-transport` 1.10.8 source that `disconnect()` stops local mic tracks and closes the peer connection. If it does not, stop the local tracks explicitly using the client's public API (e.g. `enableMic(false)` / tracks()), not private fields.
- Invariant: whenever the UI offers "Connect", there is no live client, no captured mic, no remote audio attached.
- Put the state/lifecycle logic in a small framework-free module (e.g. `web/src/lib/session.ts`) with an injectable client so it can be unit-tested; keep `page.tsx` thin.
### C. Backend: bounded Gemini retry
- Use the google-genai client's supported retry: pass `http_options` with `HttpRetryOptions` to `GoogleLLMService` (`http_options` param exists at `pipecat/services/google/llm.py` L216; Pipecat merges its header via `update_google_client_http_options` — confirm your options survive that merge).
- Policy: `attempts=3` (1 original + 2 retries), `initial_delay=0.5`, `max_delay=2.0`, `exp_base=2`, small jitter, `http_status_codes=[500, 502, 503, 504]` (not 429 — quota won't recover in seconds).
- Verify from `google/genai/_api_client.py` that the **async streaming** generate path goes through the retrying request (`_async_request` with `retry_options`) and that retry only covers establishing the response (i.e. before any chunk is yielded, so no partial output is duplicated). Report the evidence. If streaming does NOT use it, stop and report — do not hand-roll a retry loop without orchestrator approval.
- Do not change model, VAD, turn strategy, or `retry_on_timeout`.
### D. Backend: failed turn cannot resurface
- When the LLM turn fails (ErrorFrame from the LLM after retries exhausted), remove the trailing unanswered user message(s) that belonged to the failed turn from the `LLMContext`, so the next response addresses only new speech. Use the smallest mechanism supported by installed Pipecat (e.g. an LLM service error event handler if one exists, else a tiny processor that observes the upstream `ErrorFrame` from the LLM). Verify ErrorFrame direction/APIs in source; don't invent.
- The non-fatal RTVI error already reaches the client — keep it non-fatal (the connection stays healthy). Do not include raw provider error text in UI copy.
- Existing `on_client_disconnected → runner.cancel()` stays; confirm it cancels in-flight LLM/TTS so nothing speaks after a true disconnect.

## ACCEPTANCE CRITERIA
1. Non-fatal RTVI error: status stays connected, Disconnect still shown, notice "Assistant temporarily unavailable. Please try again." shown; no Connect button.
2. Fatal error / transport disconnect / failed connect / Disconnect click / unmount: client disconnected, mic tracks stopped, remote audio detached, late events from the old client ignored.
3. Never more than one live client.
4. Gemini 500/502/503/504 retried at most 2 times with bounded backoff before any output; 429 and 4xx not retried.
5. After exhausted retries: connection stays up, unanswered user turn removed from context, user can speak again and get an answer to the new utterance.
6. No provider key/credential exposure changes; no new infrastructure; no Gate B+ work; no latency tuning.
7. Backend pytest, frontend typecheck, lint, production build all pass.

## REGRESSION TESTS (minimum)
Frontend (prefer zero new deps: Node 22.22 runs `.ts` via built-in type stripping with `node --test`; if that fails with Next's TS config, add `vitest` as the only new dev dependency and a `test` script):
- non-fatal error does not change connected status and does not trigger disconnect.
- fatal error → disconnect called, status disconnected, audio detached.
- Disconnect → client.disconnect called, mic-stop path invoked, later TrackStarted/bot events from old client ignored (no stale audio attach).
- connect while a client exists → old client disconnected first.
Backend (no network):
- GoogleLLMService constructed by `bot.py` factory carries retry options with attempts=3 and codes [500,502,503,504] after Pipecat's header merge.
- retry recovery: a fake HTTP layer (or mocked `_async_request_once`) returning 503 then success yields a successful response with 2 calls; 3×503 raises after 3 calls; 429 not retried. Test at the google-genai client level with a mocked transport — no real API calls.
- failed-turn context cleanup: after an LLM ErrorFrame, trailing unanswered user message is removed; prior completed turns preserved.

## IN SCOPE
`web/src/app/page.tsx`, new `web/src/lib/*`, web test setup, `server/bot.py`, `server/config.py` (retry constants), `server/tests/*`, small processor module if needed.

## OUT OF SCOPE
Recording, persistence, latency capture, freeze injection/detection, review UI, model/VAD/turn tuning, provider switching, git operations.

## REQUIRED VERIFICATION
`uv run pytest -q`; web `npm run typecheck`, `npx eslint .`, `npm run build`, web tests; secret grep of `web/src` and `web/.next`; brief backend startup + `/api/health`. Human test is performed by the user later.

## RETURN FORMAT
Per `.claude/agents/software-engineer.md`, plus: exact evidence (file:line) for streaming-retry path, transport `disconnect()` mic-stop behavior, and ErrorFrame direction used for context cleanup.
