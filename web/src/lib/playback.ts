// Truthful playback status derived only from native HTMLMediaElement events.

export type PlaybackStatus = "paused" | "playing" | "buffering" | "ended" | "error";

export type MediaEventName =
  | "play"
  | "playing"
  | "pause"
  | "waiting"
  | "stalled"
  | "canplay"
  | "ended"
  | "error"
  | "timeupdate";

/**
 * - `play` means playback was requested; audio is not rendering until `playing`.
 * - `waiting` / `stalled` while active mean no progress for lack of data.
 *   While paused/ended they are ignored (e.g. background preload stalls).
 * - `canplay` / `timeupdate` do not change status: `playing` always follows a
 *   resumed stall, and `timeupdate` also fires on seeks while paused.
 */
export function nextPlaybackStatus(prev: PlaybackStatus, event: MediaEventName): PlaybackStatus {
  switch (event) {
    case "play":
      return prev === "playing" ? "playing" : "buffering";
    case "playing":
      return "playing";
    case "pause":
      return prev === "error" ? "error" : "paused";
    case "ended":
      return "ended";
    case "error":
      return "error";
    case "waiting":
    case "stalled":
      return prev === "playing" || prev === "buffering" ? "buffering" : prev;
    case "canplay":
    case "timeupdate":
      return prev;
  }
}

/** Whether the element is trying to play (the button offers Pause). */
export function isActive(status: PlaybackStatus): boolean {
  return status === "playing" || status === "buffering";
}
