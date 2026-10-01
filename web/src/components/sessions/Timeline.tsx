"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { computePeaks, sharedPeakScale } from "@/lib/waveform";
import { spanPercent, formatLatencySeconds, formatMs } from "@/lib/timeline";
import type { FreezeDetected } from "@/lib/sessionTypes";
import type { LatencyRecord } from "@/lib/sessionTypes";
import WaveformCanvas from "./WaveformCanvas";
import styles from "./Timeline.module.css";

interface Props {
  durationMs: number;
  currentMs: number;
  onSeek: (ms: number) => void;
  /** Decoded per-channel PCM, already mapped by channelLayout -- null when
   * the channel has no data (missing/undecodable recording). */
  userSamples: Float32Array | null;
  botSamples: Float32Array | null;
  decodeUnavailable: boolean;
  latencies: LatencyRecord[];
  freeze: FreezeDetected | null;
}

const SEEK_STEP_MS = 5000;
const MIN_BINS = 80;
const MAX_BINS = 1200;

export default function Timeline({
  durationMs,
  currentMs,
  onSeek,
  userSamples,
  botSamples,
  decodeUnavailable,
  latencies,
  freeze,
}: Props) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const [width, setWidth] = useState(600);

  useEffect(() => {
    const el = containerRef.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver((entries) => {
      const w = entries[0]?.contentRect.width;
      if (w) setWidth(w);
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  const bins = Math.max(MIN_BINS, Math.min(MAX_BINS, Math.round(width / 2)));

  const { userPeaks, botPeaks, scale } = useMemo(() => {
    const channels = [userSamples ?? new Float32Array(0), botSamples ?? new Float32Array(0)];
    const [u, b] = computePeaks(channels, bins);
    return { userPeaks: u, botPeaks: b, scale: sharedPeakScale([u, b]) };
  }, [userSamples, botSamples, bins]);

  const handleSeekAtClientX = useCallback(
    (clientX: number) => {
      const el = containerRef.current;
      if (!el || durationMs <= 0) return;
      const rect = el.getBoundingClientRect();
      const fraction = Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
      onSeek(fraction * durationMs);
    },
    [durationMs, onSeek]
  );

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (e.key === "ArrowRight" || e.key === "ArrowUp") {
        e.preventDefault();
        onSeek(Math.min(durationMs, currentMs + SEEK_STEP_MS));
      } else if (e.key === "ArrowLeft" || e.key === "ArrowDown") {
        e.preventDefault();
        onSeek(Math.max(0, currentMs - SEEK_STEP_MS));
      } else if (e.key === "Home") {
        e.preventDefault();
        onSeek(0);
      } else if (e.key === "End") {
        e.preventDefault();
        onSeek(durationMs);
      }
    },
    [currentMs, durationMs, onSeek]
  );

  const playheadPercent = durationMs > 0 ? Math.max(0, Math.min(100, (currentMs / durationMs) * 100)) : 0;

  const renderOverlays = () => (
    <div className={styles.overlayLayer}>
      {latencies.map((latency) => {
        const span = spanPercent(latency.userStopMs, latency.botStartMs, durationMs);
        return (
          <div
            key={latency.turnId}
            className={styles.latencySpan}
            style={{ left: `${span.startPercent}%`, width: `${Math.max(0, span.endPercent - span.startPercent)}%` }}
          >
            <span className={styles.latencyLabel}>
              T{latency.turnId} · {formatLatencySeconds(latency.latencyMs)}
            </span>
          </div>
        );
      })}
      {freeze && (
        <div
          className={styles.freezeRegion}
          style={{
            left: `${spanPercent(freeze.startMs, freeze.endMs, durationMs).startPercent}%`,
            width: `${
              spanPercent(freeze.startMs, freeze.endMs, durationMs).endPercent -
              spanPercent(freeze.startMs, freeze.endMs, durationMs).startPercent
            }%`,
          }}
        >
          <span className={styles.freezeLabel}>Detected bot-audio freeze</span>
        </div>
      )}
      <div className={styles.playhead} style={{ left: `${playheadPercent}%` }} />
    </div>
  );

  return (
    <div
      ref={containerRef}
      className={styles.timeline}
      role="slider"
      tabIndex={0}
      aria-label="Session timeline"
      aria-valuemin={0}
      aria-valuemax={durationMs}
      aria-valuenow={Math.round(currentMs)}
      aria-valuetext={formatMs(currentMs)}
      onKeyDown={handleKeyDown}
      onClick={(e) => handleSeekAtClientX(e.clientX)}
    >
      <div className={styles.lane}>
        <span className={styles.laneLabel}>USER</span>
        {userSamples ? (
          <WaveformCanvas peaks={userPeaks} scale={scale} color="var(--tt-accent-user)" bins={bins} />
        ) : (
          <div className={styles.laneUnavailable}>{decodeUnavailable ? "Waveform unavailable" : "No recording"}</div>
        )}
        {renderOverlays()}
      </div>
      <div className={styles.lane}>
        <span className={styles.laneLabel}>BOT</span>
        {botSamples ? (
          <WaveformCanvas peaks={botPeaks} scale={scale} color="var(--tt-accent-bot)" bins={bins} />
        ) : (
          <div className={styles.laneUnavailable}>{decodeUnavailable ? "Waveform unavailable" : "No recording"}</div>
        )}
        {renderOverlays()}
      </div>
    </div>
  );
}
