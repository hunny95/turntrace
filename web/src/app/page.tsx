"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { PipecatClient, RTVIEvent } from "@pipecat-ai/client-js";
import { SmallWebRTCTransport } from "@pipecat-ai/small-webrtc-transport";
import { SessionController, type SessionState } from "@/lib/session";
import { API_URL } from "@/lib/apiBase";
import styles from "./page.module.css";

export default function Home() {
  const [sessionState, setSessionState] = useState<SessionState>({
    status: "disconnected",
    notice: null,
    errorText: null,
  });
  // The remote bot audio track lives in ordinary React state, never in a
  // ref read during render or passed into the controller's constructor. A
  // separate effect below applies it to the <audio> element imperatively.
  const [remoteTrack, setRemoteTrack] = useState<MediaStreamTrack | null>(null);
  const audioElRef = useRef<HTMLAudioElement | null>(null);

  const [controller] = useState<SessionController>(
    () =>
      new SessionController({
        createClient: (callbacks) =>
          new PipecatClient({
            transport: new SmallWebRTCTransport(),
            enableMic: true,
            enableCam: false,
            callbacks,
          }),
        attachRemoteAudio: setRemoteTrack,
        onStateChange: setSessionState,
        trackStartedEvent: RTVIEvent.TrackStarted,
        noticeClearingEvents: [RTVIEvent.BotStartedSpeaking, RTVIEvent.UserStartedSpeaking],
      })
  );

  useEffect(() => {
    if (!audioElRef.current) return;
    audioElRef.current.srcObject = remoteTrack ? new MediaStream([remoteTrack]) : null;
  }, [remoteTrack]);

  useEffect(() => {
    return () => {
      void controller.disconnect();
    };
  }, [controller]);

  const handleConnect = useCallback(async () => {
    await controller.connect({ connection_url: `${API_URL}/api/offer` });
  }, [controller]);

  const handleDisconnect = useCallback(async () => {
    await controller.disconnect();
  }, [controller]);

  const { status, notice, errorText } = sessionState;
  const isConnected = status === "connected";
  const isConnecting = status === "connecting";

  const statusClassName =
    status === "connected"
      ? `${styles.status} ${styles.statusConnected}`
      : status === "error"
        ? `${styles.status} ${styles.statusError}`
        : styles.status;

  return (
    <div className={styles.page}>
      <main className={styles.main}>
        <h1 className={styles.title}>TurnTrace</h1>
        <p className={styles.subtitle}>Voice Agent Session Inspector</p>

        <span className={statusClassName}>{status}</span>

        {notice && <p className={styles.noticeText}>{notice}</p>}
        {errorText && <p className={styles.errorText}>{errorText}</p>}

        <button
          className={styles.button}
          onClick={isConnected ? handleDisconnect : handleConnect}
          disabled={isConnecting}
        >
          {isConnected ? "Disconnect" : isConnecting ? "Connecting…" : "Connect"}
        </button>

        <audio ref={audioElRef} autoPlay />
      </main>
    </div>
  );
}
