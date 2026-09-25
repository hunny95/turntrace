import { test } from "node:test";
import assert from "node:assert/strict";

import { computePeaks, sharedPeakScale, normalizeAmplitude } from "./waveform.ts";

// H) computePeaks: two channels, bin count, silence ~ 0, tone -> peaks,
// noise floor keeps tiny noise flat.

test("computePeaks returns one entry per channel with the requested bin count", () => {
  const user = new Float32Array(1000).fill(0);
  const bot = new Float32Array(1000).fill(0);
  const result = computePeaks([user, bot], 10);
  assert.equal(result.length, 2);
  assert.equal(result[0].max.length, 10);
  assert.equal(result[0].rms.length, 10);
  assert.equal(result[1].max.length, 10);
});

test("computePeaks: silence produces ~0 max and rms", () => {
  const silence = new Float32Array(2000).fill(0);
  const [peaks] = computePeaks([silence], 8);
  for (const v of peaks.max) assert.equal(v, 0);
  for (const v of peaks.rms) assert.equal(v, 0);
});

test("computePeaks: a full-scale tone produces high peaks", () => {
  const n = 4800;
  const tone = new Float32Array(n);
  for (let i = 0; i < n; i++) tone[i] = Math.sin((i / n) * Math.PI * 40) * 0.9;
  const [peaks] = computePeaks([tone], 16);
  const maxOfMax = Math.max(...peaks.max);
  assert.ok(maxOfMax > 0.5, `expected a strong peak, got ${maxOfMax}`);
});

test("computePeaks: bins beyond available samples stay defined (no NaN)", () => {
  const short = new Float32Array(3).fill(0.5);
  const [peaks] = computePeaks([short], 10);
  assert.equal(peaks.max.length, 10);
  assert.ok(peaks.max.every((v) => Number.isFinite(v)));
});

test("sharedPeakScale: tiny noise stays below the floor, keeping lanes visually flat", () => {
  const tinyNoise = { max: Float32Array.from([0.001, 0.002, 0.0005]), rms: Float32Array.from([0.001, 0.001, 0.0005]) };
  const scale = sharedPeakScale([tinyNoise]);
  assert.equal(scale, 0.05); // floor, not the tiny measured peak
  assert.ok(normalizeAmplitude(0.002, scale) < 0.1);
});

test("sharedPeakScale: real signal dominates the floor", () => {
  const loud = { max: Float32Array.from([0.8, 0.3]), rms: Float32Array.from([0.5, 0.2]) };
  const quiet = { max: Float32Array.from([0.01]), rms: Float32Array.from([0.005]) };
  const scale = sharedPeakScale([loud, quiet]);
  assert.ok(Math.abs(scale - 0.8) < 1e-6, `expected ~0.8, got ${scale}`);
});

test("normalizeAmplitude clamps to [0,1]", () => {
  assert.equal(normalizeAmplitude(1.5, 1), 1);
  assert.equal(normalizeAmplitude(-1, 1), 0);
  assert.equal(normalizeAmplitude(0.5, 1), 0.5);
});
