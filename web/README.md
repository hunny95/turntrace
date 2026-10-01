# turntrace-web

Next.js + TypeScript frontend for TurnTrace — Voice Agent Session Inspector.

It connects the browser microphone and speaker to the Pipecat backend over SmallWebRTC using `@pipecat-ai/client-js` and `@pipecat-ai/small-webrtc-transport`. No provider credentials live here; all provider calls happen in `../server`.

## Run

```bash
npm install
npm run dev        # http://localhost:3000
```

The backend URL defaults to `http://localhost:7860`; override with `NEXT_PUBLIC_TURNTRACE_API_URL` in `web/.env.local` (a URL only — never put secrets in `NEXT_PUBLIC_*` variables).

## Checks

```bash
npm run typecheck
npx eslint .
npm test           # node --test src/lib/*.test.ts
npm run build
```

## Layout

- `src/lib/session.ts` — session lifecycle controller: connection status follows the transport lifecycle; non-fatal provider errors surface as a recoverable notice; a single teardown path disconnects the client, stops the microphone, and detaches bot audio.
- `src/app/page.tsx` — minimal UI (Connect / status / Disconnect).
