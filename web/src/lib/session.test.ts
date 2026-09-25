import { test } from "node:test";
import assert from "node:assert/strict";

import {
  SessionController,
  type SessionClient,
  type SessionClientCallbacks,
  type SessionState,
} from "./session.ts";

/** A fake client with manual control over when its callbacks fire. */
class FakeClient implements SessionClient {
  disconnectCalls = 0;
  connectCalls = 0;
  connectShouldReject: Error | null = null;
  listeners = new Map<string, Array<(...args: unknown[]) => void>>();
  callbacks: SessionClientCallbacks;

  constructor(callbacks: SessionClientCallbacks) {
    this.callbacks = callbacks;
  }

  async connect(): Promise<void> {
    this.connectCalls += 1;
    if (this.connectShouldReject) throw this.connectShouldReject;
  }

  async disconnect(): Promise<void> {
    this.disconnectCalls += 1;
    this.callbacks.onDisconnected();
  }

  on(event: string, handler: (...args: unknown[]) => void): void {
    const existing = this.listeners.get(event) ?? [];
    existing.push(handler);
    this.listeners.set(event, existing);
  }

  emit(event: string, ...args: unknown[]): void {
    for (const handler of this.listeners.get(event) ?? []) handler(...args);
  }
}

function makeController(clients: FakeClient[], attachedTracks: (MediaStreamTrack | null)[]) {
  const states: SessionState[] = [];
  const controller = new SessionController({
    createClient: (callbacks) => {
      const client = new FakeClient(callbacks);
      clients.push(client);
      return client;
    },
    attachRemoteAudio: (track) => attachedTracks.push(track),
    onStateChange: (state) => states.push(state),
    trackStartedEvent: "trackStarted",
    noticeClearingEvents: ["botStartedSpeaking", "userStartedSpeaking"],
  });
  return { controller, states };
}

test("non-fatal error keeps status connected and does not disconnect", async () => {
  const clients: FakeClient[] = [];
  const tracks: (MediaStreamTrack | null)[] = [];
  const { controller, states } = makeController(clients, tracks);

  await controller.connect({});
  clients[0].callbacks.onConnected();
  assert.equal(controller.getState().status, "connected");

  clients[0].callbacks.onError({ data: { error: "503 UNAVAILABLE", fatal: false } });

  assert.equal(controller.getState().status, "connected");
  assert.equal(clients[0].disconnectCalls, 0);
  assert.equal(controller.getState().notice, "Assistant temporarily unavailable. Please try again.");
  // Never leak raw provider error text.
  assert.ok(!states.some((s) => s.notice?.includes("UNAVAILABLE")));
});

test("fatal error disconnects, sets error status, and detaches audio", async () => {
  const clients: FakeClient[] = [];
  const tracks: (MediaStreamTrack | null)[] = [];
  const { controller } = makeController(clients, tracks);

  await controller.connect({});
  clients[0].callbacks.onConnected();

  clients[0].callbacks.onError({ data: { error: "fatal boom", fatal: true } });
  // Fatal teardown awaits client.disconnect() internally; flush microtasks.
  await Promise.resolve();
  await Promise.resolve();

  assert.equal(clients[0].disconnectCalls, 1);
  assert.equal(controller.getState().status, "error");
  assert.equal(controller.getState().errorText, "Connection error");
  assert.equal(tracks.at(-1), null);
});

test("disconnect calls client.disconnect, tears down, and ignores late events from old client", async () => {
  const clients: FakeClient[] = [];
  const tracks: (MediaStreamTrack | null)[] = [];
  const { controller } = makeController(clients, tracks);

  await controller.connect({});
  clients[0].callbacks.onConnected();

  await controller.disconnect();

  assert.equal(clients[0].disconnectCalls, 1);
  assert.equal(controller.getState().status, "disconnected");
  assert.equal(tracks.at(-1), null);

  // Late TrackStarted / bot events from the now-stale client must not
  // re-attach audio or otherwise mutate state.
  const fakeTrack = { kind: "audio" } as MediaStreamTrack;
  clients[0].emit("trackStarted", fakeTrack, { local: false });
  clients[0].callbacks.onConnected();

  assert.equal(controller.getState().status, "disconnected");
  assert.equal(tracks.at(-1), null);
});

test("connecting while a client exists disconnects the old client first", async () => {
  const clients: FakeClient[] = [];
  const tracks: (MediaStreamTrack | null)[] = [];
  const { controller } = makeController(clients, tracks);

  await controller.connect({});
  clients[0].callbacks.onConnected();

  await controller.connect({});

  assert.equal(clients.length, 2);
  assert.equal(clients[0].disconnectCalls, 1);
  assert.notEqual(clients[0], clients[1]);

  // The stale first client's onConnected firing later must not affect state.
  clients[0].callbacks.onConnected();
  assert.notEqual(controller.getState().status, "connected");
});

test("TrackStarted attaches remote (non-local) audio tracks only", async () => {
  const clients: FakeClient[] = [];
  const tracks: (MediaStreamTrack | null)[] = [];
  const { controller } = makeController(clients, tracks);

  await controller.connect({});
  const remoteTrack = { kind: "audio" } as MediaStreamTrack;
  const localTrack = { kind: "audio" } as MediaStreamTrack;

  clients[0].emit("trackStarted", localTrack, { local: true });
  clients[0].emit("trackStarted", remoteTrack, { local: false });

  assert.deepEqual(tracks, [remoteTrack]);
});
