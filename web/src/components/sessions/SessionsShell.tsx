"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { fetchSessionSummaries, ApiError } from "@/lib/sessionsApi";
import type { SessionSummary } from "@/lib/sessionTypes";
import { ANALYSIS_STATE_LABEL, formatMs } from "@/lib/timeline";
import SessionDetailView from "./SessionDetailView";
import styles from "./SessionsShell.module.css";

interface Props {
  selectedId: string | null;
}

type ListState =
  | { kind: "loading" }
  | { kind: "error"; message: string }
  | { kind: "ready"; sessions: SessionSummary[] };

function badgeClassName(row: SessionSummary): string {
  if (row.analysisStatus === "ok" && row.freezeDetected === true) {
    return `${styles.badge} ${styles.badgeDetected}`;
  }
  if (row.analysisStatus === "ok") return `${styles.badge} ${styles.badgeNegative}`;
  if (row.analysisStatus === "error") return `${styles.badge} ${styles.badgeError}`;
  return `${styles.badge} ${styles.badgeMissing}`;
}

function badgeLabel(row: SessionSummary): string {
  if (row.analysisStatus === "ok") {
    return row.freezeDetected === true ? ANALYSIS_STATE_LABEL.detected : ANALYSIS_STATE_LABEL.negative;
  }
  if (row.analysisStatus === "error") return ANALYSIS_STATE_LABEL.error;
  return ANALYSIS_STATE_LABEL.missing;
}

export default function SessionsShell({ selectedId }: Props) {
  const router = useRouter();
  const [state, setState] = useState<ListState>({ kind: "loading" });

  useEffect(() => {
    let cancelled = false;
    fetchSessionSummaries()
      .then((sessions) => {
        if (!cancelled) setState({ kind: "ready", sessions });
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        const message = err instanceof ApiError ? err.message : "Failed to load sessions.";
        setState({ kind: "error", message });
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <div className={styles.shell}>
      <aside className={styles.sidebar} aria-label="Saved sessions">
        <div className={styles.sidebarHeader}>Sessions</div>
        {state.kind === "loading" && <p className={styles.sidebarEmpty}>Loading…</p>}
        {state.kind === "error" && <p className={styles.sidebarEmpty}>{state.message}</p>}
        {state.kind === "ready" && state.sessions.length === 0 && (
          <p className={styles.sidebarEmpty}>No saved sessions yet — run a call on the Live page.</p>
        )}
        {state.kind === "ready" &&
          state.sessions.map((row) => (
            <button
              key={row.id}
              type="button"
              className={`${styles.sessionItem} ${row.id === selectedId ? styles.sessionItemActive : ""}`}
              onClick={() => router.push(`/sessions/${row.id}`)}
              aria-current={row.id === selectedId ? "true" : undefined}
            >
              <div className={styles.sessionId}>{row.id.slice(0, 8)}</div>
              <div className={styles.sessionMeta}>
                {new Date(row.startedAt).toLocaleString()} ·{" "}
                {row.durationMs !== null ? formatMs(row.durationMs) : "—"}
              </div>
              <div className={badgeClassName(row)}>{badgeLabel(row)}</div>
            </button>
          ))}
      </aside>
      <section className={styles.detail}>
        {selectedId ? (
          <SessionDetailView key={selectedId} id={selectedId} />
        ) : (
          <p className={styles.emptyPrompt}>Select a session.</p>
        )}
      </section>
    </div>
  );
}
