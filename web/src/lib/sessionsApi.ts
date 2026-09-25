/** Thin fetch wrapper around the Gate F read-only session API
 * (server/session_api.py), parsing every response through sessionTypes.ts
 * so a malformed backend response surfaces as a handled ApiError instead
 * of a crash. Client-side fetch is acceptable here -- the backend is
 * local (see .claude/tasks/007-gate-f-session-review-ui.md). */

import { API_URL } from "./apiBase";
import {
  ParseError,
  parseSessionSummaries,
  parseSessionDetail,
  type SessionSummary,
  type SessionDetail,
} from "./sessionTypes";

export class ApiError extends Error {
  constructor(
    message: string,
    public readonly kind: "network" | "not_found" | "server" | "parse"
  ) {
    super(message);
  }
}

async function getJson(path: string): Promise<unknown> {
  let res: Response;
  try {
    res = await fetch(`${API_URL}${path}`);
  } catch {
    throw new ApiError("Could not reach the TurnTrace backend.", "network");
  }
  if (res.status === 404) {
    throw new ApiError("Session not found.", "not_found");
  }
  if (!res.ok) {
    throw new ApiError(`Backend returned ${res.status}.`, "server");
  }
  try {
    return await res.json();
  } catch {
    throw new ApiError("Backend returned an unreadable response.", "parse");
  }
}

export async function fetchSessionSummaries(): Promise<SessionSummary[]> {
  const raw = await getJson("/api/sessions");
  try {
    return parseSessionSummaries(raw);
  } catch (err) {
    throw new ApiError(err instanceof ParseError ? err.message : "Malformed response.", "parse");
  }
}

export async function fetchSessionDetail(id: string): Promise<SessionDetail> {
  const raw = await getJson(`/api/sessions/${encodeURIComponent(id)}`);
  try {
    return parseSessionDetail(raw);
  } catch (err) {
    throw new ApiError(err instanceof ParseError ? err.message : "Malformed response.", "parse");
  }
}

export function recordingUrl(id: string): string {
  return `${API_URL}/api/sessions/${encodeURIComponent(id)}/recording`;
}

export async function fetchRecordingArrayBuffer(id: string): Promise<ArrayBuffer> {
  let res: Response;
  try {
    res = await fetch(recordingUrl(id));
  } catch {
    throw new ApiError("Could not reach the TurnTrace backend.", "network");
  }
  if (!res.ok) {
    throw new ApiError("Recording not available.", "not_found");
  }
  return res.arrayBuffer();
}
