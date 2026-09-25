# TurnTrace

**Voice Agent Session Inspector**

TurnTrace records realtime AI voice-agent sessions and lets you review each one afterwards: recording, transcript, per-turn response latency, and a detected bot-audio freeze, all on one timeline.

It is an independent open-source engineering experiment.

## What it does

- **Realtime voice session:** talk to a Pipecat voice bot from the browser.
- **Recording and transcript:** every call is saved as a stereo WAV (user left, bot right) plus a finalized transcript.
- **Per-turn latency:** time from when the user actually stopped speaking to the first bot audio sent to the browser.
- **Freeze simulation:** a deterministic fault injector lets the first two replies play normally, then drops all bot audio for the rest of the call. The call stays connected.
- **Independent freeze detection:** a post-call detector finds the freeze using only the recording and transcript. It never reads the simulator's state.
- **Review UI:** a Next.js page shows waveforms, latency spans, the freeze region and a clickable transcript, with synced playback.

## Architecture

```text
Browser microphone / speaker (Next.js)
        ⇅ SmallWebRTC
Pipecat backend (Python 3.12)
  transport.input()
  → Deepgram STT
  → user turn aggregation (Silero VAD + Smart Turn)
  → failed-turn context cleanup
  → Gemini (Flash-class)
  → Cartesia TTS (websocket)
  → FreezeGate            # fault injector: drops bot audio once frozen
  → transport.output()    # WebRTC audio to the browser
  → session clock + latency tracker
  → AudioBufferProcessor  # stereo recording of emitted audio
  → assistant aggregation # transcript

On disconnect:
  recording.wav + session.json
  → freeze detector (RMS energy + transcript rules)
  → analysis.json

Next.js review:
  read-only session API → waveform → latency overlay
                        → freeze overlay → transcript
```

## Key design decisions

- **WebRTC for browser media.** Pipecat's SmallWebRTC transport gets the browser's WebRTC stack for free: getUserMedia echo cancellation, jitter buffering and Opus. Streaming raw PCM over a WebSocket would mean rebuilding that.
- **Stereo user/bot WAV.** Separate channels let each side's audio be analyzed without source separation.
- **Recorder placed after the output transport.** In Pipecat 1.11 the output transport forwards a bot audio frame only after writing it to the client. The bot channel is therefore what the user heard, not what TTS generated.
- **One recording-aligned monotonic clock.** Time zero is the first audio frame the recorder sees, which is WAV sample 0. Transcript, latency and freeze timestamps all use this clock, in milliseconds.
- **Latency = physical user speech end → first emitted bot audio.** The start is the VAD stop time minus its stop delay. So the endpointing wait, STT, LLM, TTS and output are all included.
- **Custom turn-aware latency tracker.** Pipecat's `UserBotLatencyObserver` measures the same points, but it reports no turn identity and stays armed after a failed turn. That could charge a later reply to an unanswered question. The tracker gives each turn an id and never invents a latency for a turn with no audible reply.
- **Drop frozen audio; don't send silence.** Silent frames would still trigger "bot started speaking" and produce a fake latency. Dropping them keeps the browser, recording and latency consistent.
- **Simulator and detector are independent.** No simulator state reaches any artifact. The detector does not import the simulator or config, and a test enforces this.
- **Deterministic detector.** It uses RMS energy on the bot channel inside response windows backed by the transcript. A freeze needs earlier audible speech, a later reply with text but no audio, no recovery, and continued activity afterwards. There is no ML or LLM, and ordinary trailing silence is never examined.
- **Raw and derived artifacts are kept apart.** `session.json` holds the raw record. `analysis.json` is derived, and can be regenerated or deleted.
- **Local filesystem storage**, which is enough for a single-machine inspector.

## Setup

Requirements: Python 3.12 with [uv](https://docs.astral.sh/uv/), Node.js 22.18+ (the frontend tests use Node's built-in TypeScript support), and API keys for Deepgram, Google Gemini and Cartesia.

```bash
cp .env.example server/.env
# edit server/.env and fill in:
# DEEPGRAM_API_KEY=your-deepgram-key
# GOOGLE_API_KEY=your-google-key
# CARTESIA_API_KEY=your-cartesia-key
# FREEZE_AFTER_ASSISTANT_TURNS=2   (0 disables the simulator)
```

Keys are read only by the backend. The server fails fast, naming only the missing variables. Never put provider keys in `NEXT_PUBLIC_*` variables.

## Run

```bash
# terminal 1 — backend (http://localhost:7860)
cd server
uv sync
uv run python server.py --port 7860
```

```bash
# terminal 2 — frontend (http://localhost:3000)
cd web
npm install
npm run dev
```

- **Live call:** open `http://localhost:3000`, click **Connect**, and allow microphone access. With the default config, the third reply onward is silent.
- **Review:** open `http://localhost:3000/sessions` and select a session. Reviewing makes no provider calls.
- **Backend URL:** the frontend uses `http://localhost:7860` by default. Override it with `NEXT_PUBLIC_TURNTRACE_API_URL` in `web/.env.local`.

The detector runs automatically after each call. To re-run it by hand:

```bash
cd server
uv run python -m freeze_detector <session-uuid>   # --no-write to only print
```

## Tests

None of the tests call a provider.

```bash
(cd server && uv run pytest)      # 147 tests
(cd web && npm test)              # 39 tests (node --test)
(cd web && npm run typecheck && npm run lint && npm run build)
```

## Session artifacts

```text
data/sessions/<uuid>/
├── recording.wav    # stereo 24 kHz 16-bit, L=user R=bot
├── session.json     # id, startedAt, durationMs, recording metadata, transcript, latencies
└── analysis.json    # detector output: status, freeze region, per-turn evidence
```

`data/` is local and git-ignored. Session ids are server-generated UUIDs. The read-only API (`GET /api/sessions`, `/api/sessions/{id}`, `/api/sessions/{id}/recording`) accepts only canonical lowercase UUIDs and builds file paths on the server.

## Known limitations

- The freeze detector's RMS threshold is tuned to this pipeline's TTS output. A much quieter voice or a gain change could need re-tuning.
- A silent final reply followed immediately by hang-up is intentionally not reported as a freeze. This avoids false positives, at some cost to recall.
- Heavy barge-in overlap can make turn association ambiguous, both for the freeze count and for detection windows.
- Assigning a turn id to the assistant reply relies on a timing margin, not a formal Pipecat ordering guarantee.
- Recordings are held in memory until the call ends, and the review UI loads the whole WAV. That is fine for sessions of a few minutes.
- Latency is end-to-end only; it is not broken down by stage.
- It runs on one local machine only, with no auth, database or deployment. The API relies on binding to localhost and on CORS; it has no Host-header check.

## Security

- Provider credentials live only in `server/.env`, which is git-ignored and never sent to the browser.
- CORS is restricted to the configured frontend origin.
- Recordings and transcripts stay under the git-ignored `data/` directory. Don't put confidential information in demo calls.

## License

MIT — see [LICENSE](LICENSE).
