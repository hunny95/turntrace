"use client";

import { useCallback, useEffect, useRef, useState, type SyntheticEvent } from "react";
import { fetchSessionDetail, fetchRecordingArrayBuffer, ApiError } from "@/lib/sessionsApi";
import type { SessionDetail } from "@/lib/sessionTypes";
import {
  formatMs,
  analysisState,
  ANALYSIS_STATE_LABEL,
  turnCount,
  averageLatencyMs,
  formatLatencySeconds,
} from "@/lib/timeline";
import { nextPlaybackStatus, isActive, type MediaEventName, type PlaybackStatus } from "@/lib/playback";
import Timeline from "./Timeline";
import Transcript from "./Transcript";
import styles from "./SessionDetailView.module.css";

interface Props {
  /** Rendered with `key={id}` by the caller, so every id change is a fresh
   * mount -- no in-effect state resets are needed here. */
  id: string;
}

type DetailState =
  | { kind: "loading" }
  | { kind: "error"; message: string }
  | { kind: "ready"; detail: SessionDetail };

type AudioState =
  | { kind: "idle" }
  | { kind: "loading" }
  | { kind: "error" }
  | { kind: "ready"; url: string; userSamples: Float32Array | null; botSamples: Float32Array | null };

const SPEEDS = [1, 1.5, 2] as const;

export default function SessionDetailView({ id }: Props) {
  const [detailState, setDetailState] = useState<DetailState>({ kind: "loading" });
  const [audioState, setAudioState] = useState<AudioState>({ kind: "idle" });
  const [playback, setPlayback] = useState<PlaybackStatus>("paused");
  const [currentMs, setCurrentMs] = useState(0);
  const [audioDurationMs, setAudioDurationMs] = useState<number | null>(null);
  const [speed, setSpeed] = useState<(typeof SPEEDS)[number]>(1);

  const audioRef = useRef<HTMLAudioElement | null>(null);

  // Load session.json + analysis.json.
  useEffect(() => {
    let cancelled = false;
    fetchSessionDetail(id)
      .then((detail) => {
        if (!cancelled) setDetailState({ kind: "ready", detail });
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        const message = err instanceof ApiError ? err.message : "Failed to load session.";
        setDetailState({ kind: "error", message });
      });
    return () => {
      cancelled = true;
    };
  }, [id]);

  const hasRecording = detailState.kind === "ready" && detailState.detail.session.recording !== null;

  // Load + decode the recording once the session is known to have one.
  useEffect(() => {
    if (!hasRecording || detailState.kind !== "ready") return;
    const recording = detailState.detail.session.recording;
    if (!recording) return;

    let cancelled = false;
    // Owned by this effect run: the <audio> element reads the blob lazily in
    // chunks for the whole playback, so it is revoked only when this recording
    // is replaced or the view unmounts -- never on ordinary state changes.
    let objectUrl: string | null = null;

    (async () => {
      setAudioState({ kind: "loading" });
      let buffer: ArrayBuffer;
      try {
        buffer = await fetchRecordingArrayBuffer(id);
      } catch {
        if (!cancelled) setAudioState({ kind: "error" });
        return;
      }
      if (cancelled) return;

      const blob = new Blob([buffer], { type: "audio/wav" });
      const url = URL.createObjectURL(blob);
      objectUrl = url;

      let userSamples: Float32Array | null = null;
      let botSamples: Float32Array | null = null;
      try {
        const AudioCtx =
          window.AudioContext ??
          (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext;
        const ctx = new AudioCtx();
        const decoded = await ctx.decodeAudioData(buffer.slice(0));
        const layout = recording.channelLayout;
        const userIndex = layout.indexOf("user");
        const botIndex = layout.indexOf("bot");
        if (userIndex >= 0 && userIndex < decoded.numberOfChannels) {
          userSamples = decoded.getChannelData(userIndex);
        }
        if (botIndex >= 0 && botIndex < decoded.numberOfChannels) {
          botSamples = decoded.getChannelData(botIndex);
        }
        void ctx.close();
      } catch {
        // Waveform decode failed; playback controls + transcript still work.
      }

      if (cancelled) return;
      setAudioState({ kind: "ready", url, userSamples, botSamples });
    })();

    return () => {
      cancelled = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- keyed on hasRecording flip, not the whole detailState object
  }, [hasRecording, id]);

  // Playhead: rAF while playing/buffering, cancelled on pause/unmount.
  const active = isActive(playback);
  useEffect(() => {
    if (!active) return;
    let rafId: number;
    const step = () => {
      const audio = audioRef.current;
      if (audio) setCurrentMs(audio.currentTime * 1000);
      rafId = requestAnimationFrame(step);
    };
    rafId = requestAnimationFrame(step);
    return () => cancelAnimationFrame(rafId);
  }, [active]);

  const togglePlay = useCallback(() => {
    const audio = audioRef.current;
    if (!audio) return;
    // Status follows native media events (see onMediaEvent), not these calls.
    if (audio.paused) {
      audio.play().catch(() => {
        // Rejections (e.g. play interrupted by pause) surface via media events.
      });
    } else {
      audio.pause();
    }
  }, []);

  const handleSeek = useCallback((ms: number) => {
    const audio = audioRef.current;
    if (audio) audio.currentTime = Math.max(0, ms) / 1000;
    setCurrentMs(Math.max(0, ms));
  }, []);

  const onMediaEvent = useCallback((e: SyntheticEvent<HTMLAudioElement>) => {
    setPlayback((prev) => nextPlaybackStatus(prev, e.type as MediaEventName));
    if (e.type === "timeupdate") setCurrentMs(e.currentTarget.currentTime * 1000);
  }, []);

  const handleSpeed = useCallback((value: (typeof SPEEDS)[number]) => {
    setSpeed(value);
    if (audioRef.current) audioRef.current.playbackRate = value;
  }, []);

  useEffect(() => {
    const handleKey = (e: KeyboardEvent) => {
      if (e.code !== "Space") return;
      const active = document.activeElement;
      const tag = active?.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA" || (active as HTMLElement | null)?.isContentEditable) return;
      e.preventDefault();
      togglePlay();
    };
    window.addEventListener("keydown", handleKey);
    return () => window.removeEventListener("keydown", handleKey);
  }, [togglePlay]);

  if (detailState.kind === "loading") {
    return <p className={styles.noticeText}>Loading session…</p>;
  }
  if (detailState.kind === "error") {
    return <p className={styles.analysisFailed}>{detailState.message}</p>;
  }

  const { session, analysis } = detailState.detail;
  const durationMs = session.durationMs || audioDurationMs || 0;
  const state = analysisState(analysis);
  const avgLatency = averageLatencyMs(session.latencies);
  const freeze = analysis.status === "ok" && analysis.freeze.detected ? analysis.freeze : null;

  const userSamples = audioState.kind === "ready" ? audioState.userSamples : null;
  const botSamples = audioState.kind === "ready" ? audioState.botSamples : null;
  const decodeUnavailable = audioState.kind === "ready" && !userSamples && !botSamples;

  return (
    <div>
      <div className={styles.header}>
        <span className={styles.id}>{session.id}</span>
      </div>

      <div className={styles.summary}>
        <span>
          Duration: <strong>{formatMs(durationMs)}</strong>
        </span>
        <span>
          Started: <strong>{new Date(session.startedAt).toLocaleString()}</strong>
        </span>
        <span>
          Turns: <strong>{turnCount(session.transcript)}</strong>
        </span>
        <span>
          Latencies measured: <strong>{session.latencies.length}</strong>
        </span>
        <span>
          Avg latency: <strong>{avgLatency !== null ? formatLatencySeconds(avgLatency) : "—"}</strong>
        </span>
        <span>
          Freeze: <strong>{ANALYSIS_STATE_LABEL[state]}</strong>
          {analysis.status === "error" && ` (${analysis.error})`}
        </span>
      </div>

      {!hasRecording && <p className={styles.noticeText}>No recording available for this session.</p>}
      {audioState.kind === "error" && <p className={styles.noticeText}>Recording could not be loaded.</p>}

      <div className={styles.controls}>
        <button
          type="button"
          className={styles.playButton}
          onClick={togglePlay}
          disabled={audioState.kind !== "ready"}
        >
          {active ? "Pause" : "Play"}
        </button>
        <span className={styles.timeReadout}>
          {formatMs(currentMs)} / {formatMs(durationMs)}
        </span>
        {playback === "buffering" && (
          <span className={styles.timeReadout} role="status">
            Buffering…
          </span>
        )}
        {playback === "error" && (
          <span className={styles.analysisFailed} role="status">
            Playback error
          </span>
        )}
        <div className={styles.speedGroup} role="group" aria-label="Playback speed">
          {SPEEDS.map((s) => (
            <button
              key={s}
              type="button"
              className={`${styles.speedButton} ${speed === s ? styles.speedButtonActive : ""}`}
              onClick={() => handleSpeed(s)}
              aria-pressed={speed === s}
            >
              {s}×
            </button>
          ))}
        </div>
      </div>

      {audioState.kind === "ready" && (
        <audio
          ref={audioRef}
          src={audioState.url}
          onPlay={onMediaEvent}
          onPlaying={onMediaEvent}
          onPause={onMediaEvent}
          onWaiting={onMediaEvent}
          onStalled={onMediaEvent}
          onCanPlay={onMediaEvent}
          onEnded={onMediaEvent}
          onError={onMediaEvent}
          onTimeUpdate={onMediaEvent}
          onLoadedMetadata={(e) => setAudioDurationMs(e.currentTarget.duration * 1000)}
        />
      )}

      <Timeline
        durationMs={durationMs}
        currentMs={currentMs}
        onSeek={handleSeek}
        userSamples={userSamples}
        botSamples={botSamples}
        decodeUnavailable={decodeUnavailable}
        latencies={session.latencies}
        freeze={freeze}
      />

      <div className={styles.section}>Freeze details</div>
      {state === "detected" && freeze && (
        <dl className={styles.freezeDetails}>
          <div className={styles.freezeRow}>
            <dt>Started:</dt>
            <dd>{formatMs(freeze.startMs)}</dd>
          </div>
          <div className={styles.freezeRow}>
            <dt>First affected turn:</dt>
            <dd>T{freeze.startTurnId}</dd>
          </div>
          <div className={styles.freezeRow}>
            <dt>Persists to end of session:</dt>
            <dd>{freeze.endMs >= durationMs ? "yes" : "no"}</dd>
          </div>
          <div className={styles.freezeRow}>
            <dt>Earlier audible turns:</dt>
            <dd>{freeze.evidence.audibleAssistantTurnIdsBefore.map((t) => `T${t}`).join(", ") || "none"}</dd>
          </div>
          <div className={styles.freezeRow}>
            <dt>Silent assistant turns:</dt>
            <dd>{freeze.evidence.silentAssistantTurnIds.map((t) => `T${t}`).join(", ") || "none"}</dd>
          </div>
          <div className={styles.freezeRow}>
            <dt>Continued activity turns:</dt>
            <dd>{freeze.evidence.continuationTurnIds.map((t) => `T${t}`).join(", ") || "none"}</dd>
          </div>
        </dl>
      )}
      {state === "negative" && <p className={styles.notAnalyzed}>{ANALYSIS_STATE_LABEL.negative}</p>}
      {state === "missing" && <p className={styles.notAnalyzed}>{ANALYSIS_STATE_LABEL.missing}</p>}
      {state === "error" && (
        <p className={styles.analysisFailed}>
          {ANALYSIS_STATE_LABEL.error}
          {analysis.status === "error" && ` (${analysis.error})`}
        </p>
      )}

      <div className={styles.section}>Transcript</div>
      <Transcript
        transcript={session.transcript}
        latencies={session.latencies}
        analysis={analysis}
        currentMs={currentMs}
        onSeek={handleSeek}
      />
    </div>
  );
}
