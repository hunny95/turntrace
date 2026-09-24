# TurnTrace — Claude Code Project Memory

## Mission
Build a small open-source voice-agent observability tool called **TurnTrace**.

Public positioning: "Voice Agent Session Inspector."
Present TurnTrace only as an independent open-source engineering experiment. Public repo content, commits, branch names, PR copy, UI, screenshots, and demo titles must not reference any external context for why it was built; the orchestrator keeps the exact banned-term list out of the repo.

## Required product behavior
1. Realtime voice agent built with Pipecat.
2. Deepgram for STT.
3. Gemini for LLM.
4. Cartesia for websocket-backed TTS unless a concrete blocker requires ElevenLabs.
5. Browser ↔ backend realtime transport via Pipecat SmallWebRTC.
6. Store a recording and transcript for each session.
7. Measure user-finished-speaking → first audible bot response latency per turn.
8. Display recording, transcript, and latency markers in a Next.js UI.
9. Deliberately inject a deterministic mid-call failure: after normal bot turns, bot audio stops and never resumes until call end.
10. Detect that freeze independently from recording/transcript evidence; detector must never read the simulator's state/flag/timestamp.
11. Display the detected freeze region on the recording timeline.

## Deliverables to preserve
- GitHub repository.
- A real pull request whose description explains:
  - implementation approach
  - approaches considered / trade-offs
  - future improvements
- Demo/Loom <= 5 minutes.
- Claude Code/Codex development transcript exported separately after secret review.

## Locked architecture
- Frontend: Next.js + TypeScript, package/app name `turntrace-web`.
- Display name: **TurnTrace**.
- Subtitle: **Voice Agent Session Inspector**.
- Backend: Python 3.12 + Pipecat.
- STT: Deepgram.
- LLM: Gemini Flash-class model; concise voice-oriented responses.
- TTS: Cartesia through Pipecat.
- Transport: SmallWebRTC.
- VAD/turn handling: Pipecat/Silero where supported.
- Recording: Pipecat AudioBufferProcessor; prefer 2 channels (user/bot) if current API supports it.
- Latency: Pipecat UserBotLatencyObserver if current API supports it.
- Persistence: local filesystem only.
- Session IDs: server-generated UUIDs.
- Freeze injection: custom Pipecat processor after TTS/before output.
- Freeze detection: deterministic post-call audio-energy/RMS + transcript/timing rules.
- No database, Redis, S3, auth system, cloud deployment, RAG, runtime agents, Laya/Jev, custom WebRTC, or custom VAD.

## Critical engineering rules
- Never invent Pipecat APIs. Inspect the installed version and current official examples/source before implementing.
- Prefer framework primitives over custom infrastructure.
- Work gate-by-gate; do not build the full system before verifying the voice path.
- Keep simulator and detector independent.
- Use session-relative monotonic timing.
- Do not fabricate latency for a turn where audible bot output never starts.
- Deterministic freeze injection, not random injection.
- Deterministic detection, not another LLM/model.
- Avoid speculative abstractions and unnecessary dependencies.
- When a check fails, find the concrete root cause and make the smallest fix.

## Freeze semantics
Target demo behavior:
- first 2 assistant responses audible
- response 3 onward: bot audio dropped permanently
- LLM/transcript/control path should remain alive where possible
- no crash and no automatic recovery

Suggested config:
`FREEZE_AFTER_ASSISTANT_TURNS=2`

Detector evidence should require:
- earlier audible bot speech exists
- user speaks after the last audible bot response
- assistant response exists/is expected
- corresponding bot audio is absent
- bot audio never returns before session end
- silence is long enough not to be ordinary trailing/inter-turn silence

## Storage
Use:
`data/sessions/<uuid>/recording.wav`
`data/sessions/<uuid>/session.json`

`data/`, recordings, `.env`, secrets, build artifacts, and dependency directories must not be tracked.

## Security
- Provider keys backend-only.
- Never print/read back secret values.
- Never put provider keys in Next.js public/client env vars.
- Restrict local CORS to the actual frontend origin.
- Validate UUIDs and construct server-side paths; no arbitrary filename endpoints.
- No credential/header logging.
- Before push: inspect git diff and tracked files for secrets and recordings.
- Exported Claude/Codex transcripts are separate delivery artifacts and require secret review before sharing.

## Git/PR workflow
- `main` contains only bootstrap initially.
- Functional work happens on `feat/voice-session-inspector`.
- Maintain one draft PR from feature branch → main.
- Orchestrator owns commits, pushes, PR creation/update.
- Software and QA subagents do not commit/push.
- Commit only after the relevant QA gate passes.
- Keep commits meaningful; do not commit every tiny edit.
- Do not merge the final PR automatically. Leave it ready for review unless the user explicitly asks to merge.

## Gates
A. Realtime voice: browser → Deepgram → Gemini → Cartesia → browser works for several turns.
B. Session artifacts: completed call creates transcript + playable WAV.
C. Latency: per-turn user→bot latency captured/persisted.
D. Freeze injection: normal turns then permanent bot-audio suppression while pipeline stays alive.
E. Independent detection: post-call detector finds freeze without simulator state; normal trailing silence is not a freeze.
F. Review UI: playback + transcript + latency overlay + freeze region.
G. Final quality: tests/build/security/PR notes/demo instructions/transcript instructions complete.

## Claude role policy
Main session = **Orchestrator**.
Use subagents only for bounded implementation or independent verification.

- `software-engineer`: implementation; Sonnet; no git commit/push.
- `qa-engineer`: read/test/verify; Sonnet; no code edits unless explicitly authorized.
- `final-reviewer`: one final principal/security/realtime review; Opus; read-only.

Do not spawn agents for trivial one-file changes. Do not run multiple code-editing agents in parallel.

## Handoff contract
Every delegated task must contain:
- GOAL
- WHY NOW
- ACCEPTANCE CRITERIA
- IN SCOPE
- OUT OF SCOPE
- REQUIRED VERIFICATION
- RETURN FORMAT

Every subagent returns:
- PASS / PARTIAL / FAIL
- files changed or inspected
- commands/tests run
- evidence/results
- blockers
- recommended next action

## Usage discipline
The user is on Claude Pro; conserve included usage.
- Main orchestrator: Opus 5.5 at medium effort normally; high only for architecture/hard debugging/final decisions.
- Software implementation: Sonnet 5 at medium effort.
- QA: Sonnet 5 low/medium.
- Final review: Opus 5.5 high once.
- Avoid xhigh/max unless genuinely stuck on a hard integration bug.
- Keep sessions focused; compact/clear stale context instead of carrying unrelated history.
