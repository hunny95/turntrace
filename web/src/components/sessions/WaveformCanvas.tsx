"use client";

import { useEffect, useRef } from "react";
import { normalizeAmplitude, type ChannelPeaks } from "@/lib/waveform";

interface Props {
  peaks: ChannelPeaks | null;
  scale: number;
  color: string;
  /** Bins to recompute at, tied to the container's CSS pixel width. */
  bins: number;
}

/** One lane's waveform, drawn from precomputed per-bin peaks. Device-pixel-
 * ratio aware canvas; redraws whenever peaks/scale/bins change (the parent
 * recomputes peaks on ResizeObserver-driven width changes). */
export default function WaveformCanvas({ peaks, scale, color, bins }: Props) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const dpr = typeof window !== "undefined" ? window.devicePixelRatio || 1 : 1;
    const cssWidth = canvas.clientWidth || bins;
    const cssHeight = canvas.clientHeight || 48;
    canvas.width = Math.max(1, Math.round(cssWidth * dpr));
    canvas.height = Math.max(1, Math.round(cssHeight * dpr));
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, cssWidth, cssHeight);

    if (!peaks || peaks.max.length === 0) return;

    const mid = cssHeight / 2;
    const binWidth = cssWidth / peaks.max.length;
    ctx.fillStyle = color;
    for (let i = 0; i < peaks.max.length; i++) {
      const amp = normalizeAmplitude(peaks.max[i], scale);
      const h = Math.max(1, amp * mid);
      const x = i * binWidth;
      ctx.fillRect(x, mid - h, Math.max(1, binWidth - 0.5), h * 2);
    }
  }, [peaks, scale, color, bins]);

  return <canvas ref={canvasRef} style={{ width: "100%", height: "100%", display: "block" }} />;
}
