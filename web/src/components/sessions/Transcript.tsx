"use client";

import { useMemo } from "react";
import type { AnalysisResult, LatencyRecord, TranscriptEntry } from "@/lib/sessionTypes";
import {
  formatMs,
  formatLatencySeconds,
  transcriptSeekMs,
  isSilentAssistantTurn,
  latencyForTurn,
  activeRowIndex,
} from "@/lib/timeline";
import styles from "./Transcript.module.css";

interface Props {
  transcript: TranscriptEntry[];
  latencies: LatencyRecord[];
  analysis: AnalysisResult;
  currentMs: number;
  onSeek: (ms: number) => void;
}

export default function Transcript({ transcript, latencies, analysis, currentMs, onSeek }: Props) {
  const chronological = useMemo(
    () => [...transcript].sort((a, b) => a.timestampMs - b.timestampMs),
    [transcript]
  );
  const activeIndex = useMemo(() => activeRowIndex(chronological, currentMs), [chronological, currentMs]);

  if (chronological.length === 0) {
    return <p className={styles.empty}>No transcript entries.</p>;
  }

  return (
    <div className={styles.list} role="list" aria-label="Session transcript">
      {chronological.map((entry, i) => {
        const latency = entry.role === "assistant" ? latencyForTurn(latencies, entry.turnId) : undefined;
        const silent = entry.role === "assistant" && isSilentAssistantTurn(analysis, entry.turnId);
        return (
          <button
            key={`${entry.timestampMs}-${i}`}
            type="button"
            role="listitem"
            className={`${styles.row} ${i === activeIndex ? styles.rowActive : ""}`}
            onClick={() => onSeek(transcriptSeekMs(entry.timestampMs))}
          >
            <span className={styles.timestamp}>{formatMs(entry.timestampMs)}</span>
            <span className={`${styles.roleCol} ${entry.role === "user" ? styles.roleUser : styles.roleAssistant}`}>
              <span>{entry.role === "user" ? "USER" : "ASSISTANT"}</span>
              {typeof entry.turnId === "number" && <span className={styles.turnTag}>TURN {entry.turnId}</span>}
            </span>
            <span>
              <span className={styles.text}>{entry.text}</span>
              {latency && (
                <span className={styles.latencyTag}>LATENCY {formatLatencySeconds(latency.latencyMs)}</span>
              )}
              {silent && <span className={styles.silentTag}>No bot audio observed</span>}
            </span>
          </button>
        );
      })}
    </div>
  );
}
