# TurnTrace

**Voice Agent Session Inspector**

TurnTrace is an open-source experiment for inspecting realtime AI voice-agent sessions, including recordings, transcripts, user-to-bot response latency, and silent-output failures. It records a conversation, preserves its transcript, measures user-to-bot response latency, and highlights a deliberately simulated silent-output failure on the session timeline.

## What it demonstrates

The voice path is intentionally simple:

```text
Browser microphone
  → Pipecat / SmallWebRTC
  → Deepgram STT
  → Gemini LLM
  → Cartesia TTS
  → browser speaker
```

Alongside the realtime path, TurnTrace records session artifacts used for debugging and review:

- conversation recording
- user/assistant transcript
- session-relative timestamps
- per-turn user-to-bot latency
- detected bot-audio freeze interval

A deterministic failure injector can stop bot audio mid-conversation while the rest of the pipeline remains alive. A separate post-call detector analyzes observable session artifacts rather than reading the injector's internal state.

## Architecture

```text
                         ┌────────────────────┐
                         │   Next.js client   │
                         │ mic / speaker / UI │
                         └─────────┬──────────┘
                                   │ SmallWebRTC
                                   ▼
┌──────────────────────────── Pipecat backend ────────────────────────────┐
│                                                                        │
│  audio → Deepgram STT → Gemini LLM → Cartesia TTS → FreezeGate → audio│
│                                                                        │
│           transcript / timing / recording / latency observation         │
└──────────────────────────────────┬─────────────────────────────────────┘
                                   │
                                   ▼
                         local session artifacts
                                   │
                                   ▼
                         post-call freeze detector
                                   │
                                   ▼
                         Next.js session review
```

## Design principles

- Use Pipecat primitives instead of rebuilding realtime voice infrastructure.
- Keep failure injection deterministic and reproducible.
- Keep freeze detection independent from failure-injection state.
- Prefer deterministic signal analysis over an LLM for a binary audio-failure condition.
- Store only the minimum artifacts needed for a local engineering demo.
- Keep provider credentials server-side.

## Technology

- Python 3.12
- Pipecat
- Deepgram STT
- Gemini
- Cartesia TTS
- SmallWebRTC
- Next.js + TypeScript
- Local filesystem session storage

## Repository layout

The implementation will use a lightweight monorepo:

```text
turntrace/
├── server/
├── web/
├── data/                 # ignored; local session artifacts
│   └── sessions/
│       └── <uuid>/
│           ├── recording.wav
│           └── session.json
├── docs/
├── .claude/
├── CLAUDE.md
└── README.md
```

## Local development

Backend (Python 3.12, managed by `uv`):

```bash
cd server
uv sync
uv run python server.py --port 7860
```

Requires `server/.env` (copy from `.env.example`) with `DEEPGRAM_API_KEY`,
`GOOGLE_API_KEY`, and `CARTESIA_API_KEY` set. The server fails fast with a
names-only error if any are missing.

Frontend (Next.js + TypeScript):

```bash
cd web
npm install
npm run dev
```

Open `http://localhost:3000`, click **Connect**, and allow microphone access.
The frontend talks to the backend at `http://localhost:7860` by default
(override with `NEXT_PUBLIC_TURNTRACE_API_URL` in `web/.env.local`).

## Status

Work in progress. See `docs/STATUS.md` for the current verified implementation gate.

## Security

- Provider credentials (Deepgram, Google, Cartesia) are read only by the backend from a local `.env`; they are never exposed to the browser or committed.
- Session recordings and transcripts are stored locally under `data/`, which is git-ignored.
- Session IDs are server-generated UUIDs; artifact paths are built server-side, never from client-supplied filenames.
- The backend allows CORS only from the configured frontend origin.
- Demo conversations should not contain confidential information.

## License

MIT — see [LICENSE](LICENSE).
