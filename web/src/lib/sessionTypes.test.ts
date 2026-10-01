import { test } from "node:test";
import assert from "node:assert/strict";

import {
  parseSessionSummaries,
  parseSessionDetail,
  parseAnalysisResult,
  ParseError,
} from "./sessionTypes.ts";

test("parseSessionSummaries accepts a well-formed list", () => {
  const raw = [
    {
      id: "0b1c2d3e-4f50-4a61-8b72-93a4b5c6d7e8",
      startedAt: "2026-01-01T00:00:00.000Z",
      durationMs: 1000,
      turnCount: 2,
      latencyCount: 1,
      hasRecording: true,
      analysisStatus: "ok",
      freezeDetected: false,
      freezeStartMs: null,
    },
  ];
  const parsed = parseSessionSummaries(raw);
  assert.equal(parsed.length, 1);
  assert.equal(parsed[0].id, raw[0].id);
});

test("parseSessionSummaries rejects a malformed payload", () => {
  assert.throws(() => parseSessionSummaries({ not: "an array" }), ParseError);
  assert.throws(() => parseSessionSummaries([{ id: 123 }]), ParseError);
  assert.throws(() => parseSessionSummaries(null), ParseError);
});

test("parseAnalysisResult handles all four states and rejects garbage", () => {
  assert.deepEqual(parseAnalysisResult({ status: "missing" }), { status: "missing" });
  assert.deepEqual(parseAnalysisResult({ status: "error", error: "x" }), { status: "error", error: "x" });
  const ok = parseAnalysisResult({
    status: "ok",
    version: 1,
    freeze: { detected: false, reason: "x", detail: "y" },
  });
  assert.equal(ok.status, "ok");
  assert.throws(() => parseAnalysisResult({ status: "ok", freeze: { detected: "yes" } }), ParseError);
  assert.throws(() => parseAnalysisResult("nope"), ParseError);
  assert.throws(() => parseAnalysisResult({ status: "weird" }), ParseError);
});

test("parseSessionDetail rejects malformed nested transcript entries", () => {
  const raw = {
    session: {
      id: "x",
      startedAt: "2026-01-01T00:00:00.000Z",
      durationMs: 1000,
      recording: null,
      transcript: [{ role: "unknown-role", text: "a", timestampMs: 0 }],
      latencies: [],
    },
    analysis: { status: "missing" },
  };
  assert.throws(() => parseSessionDetail(raw), ParseError);
});

test("parseSessionDetail accepts a full well-formed detail response", () => {
  const raw = {
    session: {
      id: "x",
      startedAt: "2026-01-01T00:00:00.000Z",
      durationMs: 1000,
      recording: { sampleRate: 24000, channels: 2, channelLayout: ["user", "bot"] },
      transcript: [
        { turnId: 1, role: "user", text: "hi", timestampMs: 0 },
        { role: "assistant", text: "hello", timestampMs: 100 },
      ],
      latencies: [{ turnId: 1, userStopMs: 0, botStartMs: 200, latencyMs: 200 }],
    },
    analysis: { status: "missing" },
  };
  const parsed = parseSessionDetail(raw);
  assert.equal(parsed.session.transcript.length, 2);
  assert.equal(parsed.session.transcript[1].turnId, undefined);
});
