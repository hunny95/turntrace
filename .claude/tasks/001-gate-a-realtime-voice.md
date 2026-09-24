# Task 001 — Gate A: Realtime Voice Path

## GOAL
Establish the minimum working realtime voice conversation.

## WHY NOW
All recording, latency, observability, and freeze work depends on first proving the core voice pipeline.

## ARCHITECTURE
```text
Browser microphone
→ SmallWebRTC
→ Pipecat
→ Deepgram STT
→ Gemini
→ Cartesia TTS
→ SmallWebRTC
→ browser speaker
```

Layout:
- `server/` — Python 3.12 backend, managed by `uv` (`server/pyproject.toml` + committed `uv.lock`, `requires-python = ">=3.12,<3.13"`). Reads credentials from `server/.env` (already present locally, git-ignored).
- `web/` — Next.js + TypeScript app, package name `turntrace-web`, npm with committed `package-lock.json`.

## ENVIRONMENT FACTS (verified by orchestrator)
- macOS arm64. System Python is 3.13; Python 3.12.14 is available via `uv` (`uv python find 3.12`). `uv` 0.12.18 installed.
- Node 22.22, npm 11.16 (use npm; pnpm also exists but do not use it).
- `server/.env` has `DEEPGRAM_API_KEY`, `GOOGLE_API_KEY`, `CARTESIA_API_KEY` PRESENT. Never print, cat, or echo it.

## VERSION / API FACTS (verified by orchestrator — confirm against installed source)
- `pipecat-ai` latest is **1.11.0**. Extras that exist: `deepgram`, `google`, `webrtc`, `runner`, `local` (Silero uses base/`silero` deps — confirm). **There is no `cartesia` extra** — confirm `pipecat.services.cartesia.tts` imports with the chosen extras; add a direct dependency only if the import fails.
- Pipecat 1.x API differs from older tutorials. The current official `examples/getting-started/06-voice-agent.py` uses: `PipelineWorker` + `PipelineParams` (`pipecat.pipeline.worker`), `WorkerRunner` (`pipecat.workers.runner`), `LLMContext` + `LLMContextAggregatorPair` + `LLMUserAggregatorParams(vad_analyzer=SileroVADAnalyzer())`, and `XService.Settings(...)` for service settings (e.g. `system_instruction`, `voice`). Do NOT use `PipelineTask`/`OpenAILLMContext`-style code unless the installed source shows it is current.
- Official SmallWebRTC reference: `github.com/pipecat-ai/pipecat-examples/tree/main/p2p-webrtc/voice-agent` (`server.py`, `bot.py`). Follow its offer/answer endpoint and `SmallWebRTCConnection`/`SmallWebRTCTransport` pattern from the installed version.
- JS client: `@pipecat-ai/client-js` 1.13.x + `@pipecat-ai/small-webrtc-transport` 1.10.x (React helpers `@pipecat-ai/client-react` optional — only if it simplifies; don't add UI kits).
- Pin exact versions (uv.lock / package-lock.json).

## ACCEPTANCE CRITERIA
1. Backend has one clear documented local startup command.
2. Frontend has one clear documented local startup command.
3. Browser requests microphone permission.
4. SmallWebRTC connection establishes.
5. User microphone audio reaches the backend.
6. Deepgram returns final user transcription.
7. User text reaches Gemini.
8. Gemini generates a short spoken-response-friendly answer.
9. Gemini response reaches Cartesia.
10. Cartesia-generated audio reaches the browser.
11. Three sequential turns work without reconnecting.
12. Disconnect works cleanly.
13. Provider credentials stay backend-only.
14. Missing credentials produce useful errors mentioning variable names but never values.
15. No database or unrelated infrastructure is introduced.
16. Relevant backend checks/tests pass.
17. Frontend type checking passes.
18. Frontend production build passes.

Additional implementation constraints:
- Backend logs (INFO) must make each hop observable for the human test without logging secrets/headers: client connected, final user transcript text, bot response text (or LLM/TTS start), client disconnected. Use Pipecat's built-in logging/observers where available rather than custom infrastructure.
- CORS restricted to `FRONTEND_ORIGIN` (default `http://localhost:3000`).
- Frontend reaches backend via a URL configured by a non-secret env var (e.g. `NEXT_PUBLIC_TURNTRACE_API_URL`, default `http://localhost:7860`) or a Next.js rewrite. No provider key in any `NEXT_PUBLIC_*` var or client bundle.
- Server-generated session identity only if the transport needs it; no persistence.
- Minimal UI: title **TurnTrace**, subtitle **Voice Agent Session Inspector**, Connect button, connection status, Disconnect button, bot audio playback. No transcript/review UI yet.
- Missing-key startup check: fail fast at backend start (or bot start) with e.g. `Missing required environment variable(s): CARTESIA_API_KEY` — names only.

## GEMINI BEHAVIOR
Use a current low-latency Gemini Flash-class model compatible with the verified Pipecat `GoogleLLMService` integration (check the installed default model and pick a current Flash model; record the choice).

System instruction:
"You are a concise voice assistant. Respond naturally in one or two short sentences. Your responses will be spoken aloud. Do not use markdown, lists, emojis, or long explanations."

Do not add: RAG, tools, function calling, web search, memory, runtime agents, Laya, Jev.

## IN SCOPE
- `server/` project, bot pipeline, SmallWebRTC offer/answer HTTP endpoint, credential check, minimal smoke test(s).
- `web/` Next.js app with Connect/status/Disconnect.
- `.env.example` update if new non-secret variables are needed.
- Short "Local development" section in README with the two startup commands.

## OUT OF SCOPE (prohibited in this task)
- Gate B+: recording (AudioBufferProcessor), session.json/transcript persistence, `data/` writes.
- Latency measurement/observers/storage/overlays (Gate C).
- Freeze injection processor (Gate D), freeze detection (Gate E), review UI/timeline (Gate F).
- Database, Redis, S3, auth, deployment, Docker, CI, custom WebRTC, custom VAD, tool/function calling.
- Git commit/push/branch operations.

## REQUIRED VERIFICATION
- Dependency/install verification: `uv sync` in `server/`, `npm ci` (or `npm install` producing lockfile) in `web/`; confirm Python is 3.12 in the venv.
- Backend import/startup check: import the bot module; start the server briefly and confirm it listens and the offer endpoint exists; confirm missing-key error lists names only (e.g. run with an empty env override — never print real values).
- Frontend type check: `npx tsc --noEmit` (or a `typecheck` script).
- Frontend production build: `npm run build`.
- Secret-boundary inspection: grep the web source and `.next` build output for provider key names/values patterns; confirm `server/.env` is git-ignored and untracked.
- Realtime wiring inspection: pipeline order transport.input → STT → user aggregator → LLM → TTS → transport.output → assistant aggregator; Silero VAD configured.
- Human microphone/speaker test: performed by the user later — not claimable by agents.

## RETURN FORMAT
Per `.claude/agents/software-engineer.md` (STATUS / SUMMARY / FILES CHANGED / COMMANDS / TESTS RUN / RESULTS / RISKS / BLOCKERS / QA SHOULD VERIFY). Include the exact backend and frontend startup commands, the frontend URL, and the chosen pinned versions + Gemini model + Cartesia voice.
