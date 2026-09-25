import { test } from "node:test";
import assert from "node:assert/strict";

import {
  formatMs,
  formatLatencySeconds,
  spanPercent,
  transcriptSeekMs,
  activeRowIndex,
  analysisState,
  turnCount,
  averageLatencyMs,
  isSilentAssistantTurn,
  latencyForTurn,
} from "./timeline.ts";
import type { AnalysisResult, LatencyRecord, TranscriptEntry } from "./sessionTypes.ts";

// A) formatMs
test("formatMs(38397) -> 00:38.4", () => {
  assert.equal(formatMs(38397), "00:38.4");
});

test("formatMs handles minutes and zero", () => {
  assert.equal(formatMs(0), "00:00.0");
  assert.equal(formatMs(65000), "01:05.0");
  assert.equal(formatMs(-5), "00:00.0");
});

test("formatLatencySeconds formats from persisted latencyMs", () => {
  assert.equal(formatLatencySeconds(2300), "2.30 s");
  assert.equal(formatLatencySeconds(7900), "7.90 s");
});

// B) span 20000-25000 of 100000 -> 20%-25%
test("spanPercent basic span", () => {
  assert.deepEqual(spanPercent(20000, 25000, 100000), { startPercent: 20, endPercent: 25 });
});

// C) freeze 40000-100000 -> 40%-100%
test("spanPercent freeze-to-end span", () => {
  assert.deepEqual(spanPercent(40000, 100000, 100000), { startPercent: 40, endPercent: 100 });
});

test("spanPercent clamps out-of-range values", () => {
  assert.deepEqual(spanPercent(-5000, 150000, 100000), { startPercent: 0, endPercent: 100 });
  assert.deepEqual(spanPercent(0, 0, 0), { startPercent: 0, endPercent: 0 });
});

test("transcriptSeekMs subtracts 500ms and clamps at 0", () => {
  assert.equal(transcriptSeekMs(1000), 500);
  assert.equal(transcriptSeekMs(100), 0);
});

test("activeRowIndex finds the last entry at or before currentMs", () => {
  const entries: TranscriptEntry[] = [
    { role: "user", text: "a", timestampMs: 0 },
    { role: "assistant", text: "b", timestampMs: 1000 },
    { role: "user", text: "c", timestampMs: 2000 },
  ];
  assert.equal(activeRowIndex(entries, -1), -1);
  assert.equal(activeRowIndex(entries, 500), 0);
  assert.equal(activeRowIndex(entries, 1000), 1);
  assert.equal(activeRowIndex(entries, 5000), 2);
  assert.equal(activeRowIndex([], 100), -1);
});

// D) analysis states x4
test("analysisState maps all four states", () => {
  const detected: AnalysisResult = {
    status: "ok",
    freeze: {
      detected: true,
      label: "bot_audio_freeze",
      reason: "x",
      startMs: 0,
      endMs: 100,
      startTurnId: 1,
      evidence: { audibleAssistantTurnIdsBefore: [], silentAssistantTurnIds: [], continuationTurnIds: [] },
    },
  };
  const negative: AnalysisResult = {
    status: "ok",
    freeze: { detected: false, reason: "x", detail: "y" },
  };
  const missing: AnalysisResult = { status: "missing" };
  const error: AnalysisResult = { status: "error", error: "invalid_analysis_json" };

  assert.equal(analysisState(detected), "detected");
  assert.equal(analysisState(negative), "negative");
  assert.equal(analysisState(missing), "missing");
  assert.equal(analysisState(error), "error");
});

// E) entry without turnId -> no crash, turnCount rules
test("turnCount: modern transcript counts distinct user turnIds", () => {
  const transcript: TranscriptEntry[] = [
    { turnId: 1, role: "user", text: "a", timestampMs: 0 },
    { turnId: 1, role: "assistant", text: "b", timestampMs: 100 },
    { turnId: null, role: "assistant", text: "greeting", timestampMs: -1 },
    { turnId: 2, role: "user", text: "c", timestampMs: 200 },
  ];
  assert.equal(turnCount(transcript), 2);
});

test("turnCount: legacy transcript (no turnId key) counts user entries", () => {
  const transcript: TranscriptEntry[] = [
    { role: "user", text: "a", timestampMs: 0 },
    { role: "assistant", text: "b", timestampMs: 100 },
    { role: "user", text: "c", timestampMs: 200 },
  ];
  assert.equal(turnCount(transcript), 2);
});

test("entry without turnId renders without crashing latency/silent lookups", () => {
  const entry: TranscriptEntry = { role: "assistant", text: "hi", timestampMs: 10 };
  assert.equal(latencyForTurn([], entry.turnId), undefined);
  const missing: AnalysisResult = { status: "missing" };
  assert.equal(isSilentAssistantTurn(missing, entry.turnId), false);
});

// F) latency matched by turnId
test("latencyForTurn matches by turnId", () => {
  const latencies: LatencyRecord[] = [
    { turnId: 1, userStopMs: 100, botStartMs: 400, latencyMs: 300 },
    { turnId: 2, userStopMs: 500, botStartMs: 900, latencyMs: 400 },
  ];
  assert.equal(latencyForTurn(latencies, 2)?.latencyMs, 400);
  assert.equal(latencyForTurn(latencies, 3), undefined);
});

// G) silent indicator only from evidence
test("isSilentAssistantTurn: assistant without latency but not in evidence is not silent", () => {
  const analysis: AnalysisResult = {
    status: "ok",
    freeze: {
      detected: true,
      label: "bot_audio_freeze",
      reason: "x",
      startMs: 0,
      endMs: 100,
      startTurnId: 3,
      evidence: {
        audibleAssistantTurnIdsBefore: [1, 2],
        silentAssistantTurnIds: [3, 4],
        continuationTurnIds: [4],
      },
    },
  };
  assert.equal(isSilentAssistantTurn(analysis, 3), true);
  assert.equal(isSilentAssistantTurn(analysis, 2), false); // audible turn, not silent
  assert.equal(isSilentAssistantTurn(analysis, 99), false); // no latency, but not in evidence
});

test("isSilentAssistantTurn is false for negative/missing/error analysis", () => {
  const negative: AnalysisResult = { status: "ok", freeze: { detected: false, reason: "x", detail: "y" } };
  assert.equal(isSilentAssistantTurn(negative, 1), false);
  assert.equal(isSilentAssistantTurn({ status: "missing" }, 1), false);
  assert.equal(isSilentAssistantTurn({ status: "error", error: "x" }, 1), false);
});

// I) summary: turn count, latency count, average
test("averageLatencyMs: mean of persisted latencyMs, null when empty", () => {
  assert.equal(averageLatencyMs([]), null);
  const latencies: LatencyRecord[] = [
    { turnId: 1, userStopMs: 0, botStartMs: 2300, latencyMs: 2300 },
    { turnId: 2, userStopMs: 0, botStartMs: 7900, latencyMs: 7900 },
  ];
  assert.equal(averageLatencyMs(latencies), 5100);
});
