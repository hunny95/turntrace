import { test } from "node:test";
import assert from "node:assert/strict";
import { nextPlaybackStatus, isActive, type MediaEventName, type PlaybackStatus } from "./playback.ts";

function run(events: MediaEventName[], start: PlaybackStatus = "paused"): PlaybackStatus {
  return events.reduce(nextPlaybackStatus, start);
}

test("play request is buffering until the element reports playing", () => {
  assert.equal(run(["play"]), "buffering");
  assert.equal(run(["play", "playing"]), "playing");
});

test("a mid-playback stall leaves the playing state", () => {
  assert.equal(run(["play", "playing", "waiting"]), "buffering");
  assert.equal(run(["play", "playing", "stalled"]), "buffering");
  assert.equal(run(["play", "playing", "timeupdate", "waiting", "timeupdate"]), "buffering");
  assert.equal(isActive("buffering"), true);
});

test("stall recovery returns to playing only on the playing event", () => {
  assert.equal(run(["play", "playing", "waiting", "canplay"]), "buffering");
  assert.equal(run(["play", "playing", "waiting", "canplay", "playing"]), "playing");
});

test("stalls while paused or ended do not claim activity", () => {
  assert.equal(run(["stalled"]), "paused");
  assert.equal(run(["play", "playing", "ended", "waiting"]), "ended");
  assert.equal(isActive("paused"), false);
  assert.equal(isActive("ended"), false);
});

test("media error is terminal until a new play request", () => {
  assert.equal(run(["play", "playing", "error"]), "error");
  assert.equal(run(["play", "playing", "error", "pause"]), "error");
  assert.equal(isActive("error"), false);
  assert.equal(run(["play", "playing", "error", "play"]), "buffering");
});

test("pause and ended stop activity", () => {
  assert.equal(run(["play", "playing", "pause"]), "paused");
  assert.equal(run(["play", "playing", "ended"]), "ended");
});
