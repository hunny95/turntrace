/**
 * Frontend data model + runtime validators for the Gate F review API
 * (server/session_api.py). Every API response is parsed through one of
 * these functions so a malformed backend response yields a handled
 * `null`/thrown ParseError, never a crash deep in a component.
 */

export interface SessionSummary {
  id: string;
  startedAt: string;
  durationMs: number | null;
  turnCount: number;
  latencyCount: number;
  hasRecording: boolean;
  analysisStatus: "ok" | "missing" | "error";
  freezeDetected: boolean | null;
  freezeStartMs: number | null;
}

export interface RecordingMetadata {
  sampleRate: number;
  channels: number;
  channelLayout: string[];
}

export interface TranscriptEntry {
  turnId?: number | null;
  role: "user" | "assistant";
  text: string;
  timestampMs: number;
}

export interface LatencyRecord {
  turnId: number;
  userStopMs: number;
  botStartMs: number;
  latencyMs: number;
}

export interface SessionJson {
  id: string;
  startedAt: string;
  durationMs: number;
  recording: RecordingMetadata | null;
  transcript: TranscriptEntry[];
  latencies: LatencyRecord[];
}

export interface FreezeEvidence {
  audibleAssistantTurnIdsBefore: number[];
  silentAssistantTurnIds: number[];
  continuationTurnIds: number[];
}

export interface FreezeDetected {
  detected: true;
  label: string;
  reason: string;
  startMs: number;
  endMs: number;
  startTurnId: number;
  evidence: FreezeEvidence;
  audio?: { speechThresholdRms: number; frameMs: number };
}

export interface FreezeNegative {
  detected: false;
  reason: string;
  detail: string;
}

export type FreezeResult = FreezeDetected | FreezeNegative;

export type AnalysisResult =
  | { status: "missing" }
  | { status: "error"; error: string }
  | { status: "ok"; version?: number; freeze: FreezeResult };

export interface SessionDetail {
  session: SessionJson;
  analysis: AnalysisResult;
}

export class ParseError extends Error {}

function fail(message: string): never {
  throw new ParseError(message);
}

function isString(v: unknown): v is string {
  return typeof v === "string";
}
function isNumber(v: unknown): v is number {
  return typeof v === "number" && Number.isFinite(v);
}
function isBool(v: unknown): v is boolean {
  return typeof v === "boolean";
}
function isObject(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

export function parseSessionSummary(raw: unknown): SessionSummary {
  if (!isObject(raw)) fail("summary: not an object");
  if (!isString(raw.id)) fail("summary: id");
  if (!isString(raw.startedAt)) fail("summary: startedAt");
  if (raw.durationMs !== null && !isNumber(raw.durationMs)) fail("summary: durationMs");
  if (!isNumber(raw.turnCount)) fail("summary: turnCount");
  if (!isNumber(raw.latencyCount)) fail("summary: latencyCount");
  if (!isBool(raw.hasRecording)) fail("summary: hasRecording");
  if (raw.analysisStatus !== "ok" && raw.analysisStatus !== "missing" && raw.analysisStatus !== "error") {
    fail("summary: analysisStatus");
  }
  if (raw.freezeDetected !== null && !isBool(raw.freezeDetected)) fail("summary: freezeDetected");
  if (raw.freezeStartMs !== null && !isNumber(raw.freezeStartMs)) fail("summary: freezeStartMs");

  return {
    id: raw.id,
    startedAt: raw.startedAt,
    durationMs: raw.durationMs as number | null,
    turnCount: raw.turnCount,
    latencyCount: raw.latencyCount,
    hasRecording: raw.hasRecording,
    analysisStatus: raw.analysisStatus,
    freezeDetected: raw.freezeDetected as boolean | null,
    freezeStartMs: raw.freezeStartMs as number | null,
  };
}

export function parseSessionSummaries(raw: unknown): SessionSummary[] {
  if (!Array.isArray(raw)) fail("summaries: not an array");
  return raw.map(parseSessionSummary);
}

function parseRecordingMetadata(raw: unknown): RecordingMetadata | null {
  if (raw === null) return null;
  if (!isObject(raw)) fail("recording: not an object");
  if (!isNumber(raw.sampleRate)) fail("recording: sampleRate");
  if (!isNumber(raw.channels)) fail("recording: channels");
  if (!Array.isArray(raw.channelLayout) || !raw.channelLayout.every(isString)) {
    fail("recording: channelLayout");
  }
  return {
    sampleRate: raw.sampleRate,
    channels: raw.channels,
    channelLayout: raw.channelLayout as string[],
  };
}

function parseTranscriptEntry(raw: unknown): TranscriptEntry {
  if (!isObject(raw)) fail("transcript entry: not an object");
  if (raw.role !== "user" && raw.role !== "assistant") fail("transcript entry: role");
  if (!isString(raw.text)) fail("transcript entry: text");
  if (!isNumber(raw.timestampMs)) fail("transcript entry: timestampMs");
  const turnId = "turnId" in raw ? raw.turnId : undefined;
  if (turnId !== undefined && turnId !== null && !isNumber(turnId)) fail("transcript entry: turnId");
  return {
    role: raw.role,
    text: raw.text,
    timestampMs: raw.timestampMs,
    turnId: turnId as number | null | undefined,
  };
}

function parseLatencyRecord(raw: unknown): LatencyRecord {
  if (!isObject(raw)) fail("latency: not an object");
  if (!isNumber(raw.turnId)) fail("latency: turnId");
  if (!isNumber(raw.userStopMs)) fail("latency: userStopMs");
  if (!isNumber(raw.botStartMs)) fail("latency: botStartMs");
  if (!isNumber(raw.latencyMs)) fail("latency: latencyMs");
  return {
    turnId: raw.turnId,
    userStopMs: raw.userStopMs,
    botStartMs: raw.botStartMs,
    latencyMs: raw.latencyMs,
  };
}

function parseFreezeResult(raw: unknown): FreezeResult {
  if (!isObject(raw)) fail("freeze: not an object");
  if (raw.detected === true) {
    if (!isString(raw.label)) fail("freeze: label");
    if (!isString(raw.reason)) fail("freeze: reason");
    if (!isNumber(raw.startMs)) fail("freeze: startMs");
    if (!isNumber(raw.endMs)) fail("freeze: endMs");
    if (!isNumber(raw.startTurnId)) fail("freeze: startTurnId");
    if (!isObject(raw.evidence)) fail("freeze: evidence");
    const ev = raw.evidence;
    const isNumArray = (v: unknown): v is number[] => Array.isArray(v) && v.every(isNumber);
    if (!isNumArray(ev.audibleAssistantTurnIdsBefore)) fail("freeze: evidence.audibleAssistantTurnIdsBefore");
    if (!isNumArray(ev.silentAssistantTurnIds)) fail("freeze: evidence.silentAssistantTurnIds");
    if (!isNumArray(ev.continuationTurnIds)) fail("freeze: evidence.continuationTurnIds");
    let audio: FreezeDetected["audio"];
    if (isObject(raw.audio) && isNumber(raw.audio.speechThresholdRms) && isNumber(raw.audio.frameMs)) {
      audio = { speechThresholdRms: raw.audio.speechThresholdRms, frameMs: raw.audio.frameMs };
    }
    return {
      detected: true,
      label: raw.label,
      reason: raw.reason,
      startMs: raw.startMs,
      endMs: raw.endMs,
      startTurnId: raw.startTurnId,
      evidence: {
        audibleAssistantTurnIdsBefore: ev.audibleAssistantTurnIdsBefore,
        silentAssistantTurnIds: ev.silentAssistantTurnIds,
        continuationTurnIds: ev.continuationTurnIds,
      },
      audio,
    };
  }
  if (raw.detected === false) {
    if (!isString(raw.reason)) fail("freeze: reason");
    if (!isString(raw.detail)) fail("freeze: detail");
    return { detected: false, reason: raw.reason, detail: raw.detail };
  }
  fail("freeze: detected");
}

export function parseAnalysisResult(raw: unknown): AnalysisResult {
  if (!isObject(raw)) fail("analysis: not an object");
  if (raw.status === "missing") return { status: "missing" };
  if (raw.status === "error") {
    if (!isString(raw.error)) fail("analysis: error");
    return { status: "error", error: raw.error };
  }
  if (raw.status === "ok") {
    const freeze = parseFreezeResult(raw.freeze);
    const version = isNumber(raw.version) ? raw.version : undefined;
    return { status: "ok", version, freeze };
  }
  fail("analysis: status");
}

function parseSessionJson(raw: unknown): SessionJson {
  if (!isObject(raw)) fail("session: not an object");
  if (!isString(raw.id)) fail("session: id");
  if (!isString(raw.startedAt)) fail("session: startedAt");
  if (!isNumber(raw.durationMs)) fail("session: durationMs");
  if (!Array.isArray(raw.transcript)) fail("session: transcript");
  if (!Array.isArray(raw.latencies)) fail("session: latencies");
  return {
    id: raw.id,
    startedAt: raw.startedAt,
    durationMs: raw.durationMs,
    recording: parseRecordingMetadata(raw.recording ?? null),
    transcript: raw.transcript.map(parseTranscriptEntry),
    latencies: raw.latencies.map(parseLatencyRecord),
  };
}

export function parseSessionDetail(raw: unknown): SessionDetail {
  if (!isObject(raw)) fail("detail: not an object");
  return {
    session: parseSessionJson(raw.session),
    analysis: parseAnalysisResult(raw.analysis),
  };
}
