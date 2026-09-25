/**
 * Pure, framework-free helpers behind the session review timeline,
 * transcript, and summary UI. Kept dependency-free and testable with
 * `node --test` (see timeline.test.ts).
 */

import type { AnalysisResult, LatencyRecord, TranscriptEntry } from "./sessionTypes";

/** "mm:ss.s" — minutes:seconds.tenths, from a non-negative millisecond value. */
export function formatMs(ms: number): string {
  const clamped = Math.max(0, ms);
  const totalTenths = Math.round(clamped / 100);
  const tenths = totalTenths % 10;
  const totalSeconds = Math.floor(totalTenths / 10);
  const seconds = totalSeconds % 60;
  const minutes = Math.floor(totalSeconds / 60);
  const pad2 = (n: number) => String(n).padStart(2, "0");
  return `${pad2(minutes)}:${pad2(seconds)}.${tenths}`;
}

/** Human latency label, e.g. `latencyMs=2300` -> "2.30 s". Never recomputed
 * from timestamps -- always from a persisted LatencyRecord's latencyMs. */
export function formatLatencySeconds(latencyMs: number): string {
  return `${(latencyMs / 1000).toFixed(2)} s`;
}

export interface Span {
  startPercent: number;
  endPercent: number;
}

/** Maps [startMs, endMs] onto a [0,100] percent span of durationMs,
 * clamped at both ends. Returns a zero-width span at 0 if durationMs <= 0. */
export function spanPercent(startMs: number, endMs: number, durationMs: number): Span {
  if (!(durationMs > 0)) return { startPercent: 0, endPercent: 0 };
  const clamp = (ms: number) => Math.max(0, Math.min(100, (ms / durationMs) * 100));
  const startPercent = clamp(startMs);
  const endPercent = clamp(endMs);
  return { startPercent, endPercent: Math.max(startPercent, endPercent) };
}

/** A transcript row's seek target: 500ms before its timestamp, per the
 * documented note that assistant timestampMs is finalization time (~end
 * of audio), so seeking to the raw timestamp would skip past the turn. */
export function transcriptSeekMs(timestampMs: number): number {
  return Math.max(0, timestampMs - 500);
}

/** Index of the active transcript row: the last entry (by array order,
 * assumed pre-sorted by timestampMs) with timestampMs <= currentMs. -1 if
 * none (before the first entry, or an empty transcript). */
export function activeRowIndex(entries: TranscriptEntry[], currentMs: number): number {
  let active = -1;
  for (let i = 0; i < entries.length; i++) {
    if (entries[i].timestampMs <= currentMs) active = i;
    else break;
  }
  return active;
}

export type AnalysisState = "detected" | "negative" | "missing" | "error";

export function analysisState(analysis: AnalysisResult): AnalysisState {
  if (analysis.status === "missing") return "missing";
  if (analysis.status === "error") return "error";
  return analysis.freeze.detected ? "detected" : "negative";
}

export const ANALYSIS_STATE_LABEL: Record<AnalysisState, string> = {
  detected: "Freeze detected",
  negative: "No persistent freeze detected",
  missing: "Not analyzed",
  error: "Analysis failed",
};

/** Same counting rule as server/session_api.py `_turn_count`: distinct
 * user-entry turnIds for modern (turnId-tagged) transcripts, or a plain
 * count of user entries for legacy transcripts with no turnId key at all. */
export function turnCount(transcript: TranscriptEntry[]): number {
  const userEntries = transcript.filter((e) => e.role === "user");
  const isModern = transcript.some((e) => "turnId" in e && e.turnId !== undefined);
  if (isModern) {
    const ids = new Set(
      userEntries.map((e) => e.turnId).filter((id): id is number => id !== null && id !== undefined)
    );
    return ids.size;
  }
  return userEntries.length;
}

/** Mean of persisted latencyMs values, or null when there are none (render
 * "-" in that case; never invent a value). */
export function averageLatencyMs(latencies: LatencyRecord[]): number | null {
  if (latencies.length === 0) return null;
  const total = latencies.reduce((sum, l) => sum + l.latencyMs, 0);
  return total / latencies.length;
}

/** True only when a detected freeze's evidence marks this assistant
 * turnId as silent -- never inferred from a missing latency record. */
export function isSilentAssistantTurn(analysis: AnalysisResult, turnId: number | null | undefined): boolean {
  if (turnId === null || turnId === undefined) return false;
  if (analysis.status !== "ok" || !analysis.freeze.detected) return false;
  return analysis.freeze.evidence.silentAssistantTurnIds.includes(turnId);
}

/** Latency record matching a transcript entry's turnId, or undefined. */
export function latencyForTurn(
  latencies: LatencyRecord[],
  turnId: number | null | undefined
): LatencyRecord | undefined {
  if (turnId === null || turnId === undefined) return undefined;
  return latencies.find((l) => l.turnId === turnId);
}
