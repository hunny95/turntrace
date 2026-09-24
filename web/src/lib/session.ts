/**
 * Framework-free realtime session lifecycle controller.
 *
 * Fixes the Gate A error-lifecycle bug: a non-fatal RTVI error (e.g. a
 * transient Gemini 503) must never present a disconnected/"Connect" UI while
 * the underlying client/mic/transport is still live. Connection status is
 * driven only by connect/disconnect lifecycle events, never by onError.
 *
 * See .claude/tasks/002-gate-a-error-lifecycle-fix.md for the incident.
 *
 * The `SessionClient` interface is the minimal subset of
 * `@pipecat-ai/client-js`'s `PipecatClient` this module needs, so tests can
 * inject a fake client with no network/WebRTC involved.
 */

export type ConnectionStatus = "disconnected" | "connecting" | "connected" | "error";

export interface SessionState {
  status: ConnectionStatus;
  /** Non-fatal, auto-clearing notice shown while the session stays connected. */
  notice: string | null;
  /** Terminal error text shown once the session has torn down. */
  errorText: string | null;
}

export interface RTVIErrorData {
  fatal?: boolean;
}

export interface RTVIErrorMessage {
  data: unknown;
}

/** Minimal event-emitter surface `SessionController` needs from a client. */
export interface SessionClient {
  connect(params: unknown): Promise<unknown>;
  disconnect(): Promise<void>;
  on(event: string, handler: (...args: unknown[]) => void): void;
}

export interface SessionClientCallbacks {
  onConnected: () => void;
  onDisconnected: () => void;
  onError: (message: RTVIErrorMessage) => void;
}

export type ClientFactory = (callbacks: SessionClientCallbacks) => SessionClient;

export interface SessionControllerOptions {
  /** Builds a fresh client wired to the given lifecycle callbacks. */
  createClient: ClientFactory;
  /** Attach (non-null) or detach (null) the remote bot audio track. */
  attachRemoteAudio: (track: MediaStreamTrack | null) => void;
  /** Called whenever session state changes. */
  onStateChange: (state: SessionState) => void;
  /** Event name used by the client for a remote audio track starting. */
  trackStartedEvent: string;
  /** Event names that should clear a non-fatal notice ("try again" banner). */
  noticeClearingEvents: string[];
}

const INITIAL_STATE: SessionState = {
  status: "disconnected",
  notice: null,
  errorText: null,
};

const NON_FATAL_NOTICE = "Assistant temporarily unavailable. Please try again.";
const FATAL_ERROR_TEXT = "Connection error";

export class SessionController {
  private client: SessionClient | null = null;
  private state: SessionState = { ...INITIAL_STATE };
  private readonly options: SessionControllerOptions;
  // Set just before we ask a client to disconnect because of a fatal RTVI
  // error, so the onDisconnected callback that follows (whether triggered by
  // our explicit call or the library's own auto-disconnect-on-fatal) reports
  // "error" with text instead of a plain "disconnected".
  private pendingErrorText: string | null = null;

  constructor(options: SessionControllerOptions) {
    this.options = options;
  }

  getState(): SessionState {
    return this.state;
  }

  private setState(patch: Partial<SessionState>): void {
    this.state = { ...this.state, ...patch };
    this.options.onStateChange(this.state);
  }

  /** Disconnects the client, detaches audio, and clears live-session state. */
  private teardown(status: ConnectionStatus, errorText: string | null): void {
    this.client = null;
    this.options.attachRemoteAudio(null);
    this.setState({ status, notice: null, errorText });
  }

  async connect(connectParams: unknown): Promise<void> {
    // Never allow two live clients: tear down any existing one first.
    if (this.client) {
      const stale = this.client;
      this.client = null;
      try {
        await stale.disconnect();
      } catch {
        // Best-effort; we're discarding this client regardless.
      }
    }

    this.setState({ status: "connecting", notice: null, errorText: null });

    const client = this.options.createClient({
      onConnected: () => {
        if (this.client !== client) return; // stale client, ignore
        this.setState({ status: "connected", errorText: null });
      },
      onDisconnected: () => {
        if (this.client !== client) return; // already torn down / superseded
        const errorText = this.pendingErrorText;
        this.pendingErrorText = null;
        this.teardown(errorText ? "error" : "disconnected", errorText);
      },
      onError: (message) => {
        if (this.client !== client) return; // stale client, ignore
        const data = message?.data as RTVIErrorData | undefined;
        if (data?.fatal === true) {
          void this.teardownFatal(client);
        } else {
          // Non-fatal: keep status/connection untouched, show a dismissible
          // notice only. Never surface raw provider error text.
          this.setState({ notice: NON_FATAL_NOTICE });
        }
      },
    });

    this.client = client;

    client.on(this.options.trackStartedEvent, (...args: unknown[]) => {
      if (this.client !== client) return; // stale client, ignore late tracks
      const [track, participant] = args as [
        MediaStreamTrack,
        { local?: boolean } | undefined,
      ];
      if (participant?.local) return;
      if (track.kind !== "audio") return;
      this.options.attachRemoteAudio(track);
    });

    for (const eventName of this.options.noticeClearingEvents) {
      client.on(eventName, () => {
        if (this.client !== client) return;
        if (this.state.notice) this.setState({ notice: null });
      });
    }

    try {
      await client.connect(connectParams);
    } catch (err) {
      if (this.client !== client) return; // superseded while connecting
      this.teardown("error", err instanceof Error ? err.message : "Failed to connect");
    }
  }

  /** Fatal RTVI error: explicitly disconnect (idempotent with the client's own). */
  private async teardownFatal(client: SessionClient): Promise<void> {
    this.pendingErrorText = FATAL_ERROR_TEXT;
    try {
      await client.disconnect();
    } catch {
      // Best-effort; we tear down our state regardless.
    }
    // If onDisconnected already ran (from this call, or the library's own
    // auto-disconnect-on-fatal racing ahead of it), it already tore down
    // state using pendingErrorText and cleared this.client. Only force a
    // teardown here if that never happened.
    if (this.client !== client) return;
    const errorText = this.pendingErrorText;
    this.pendingErrorText = null;
    this.teardown("error", errorText ?? FATAL_ERROR_TEXT);
  }

  async disconnect(): Promise<void> {
    const client = this.client;
    if (!client) return;
    try {
      await client.disconnect();
    } finally {
      if (this.client === client) {
        this.teardown("disconnected", null);
      }
    }
  }
}
