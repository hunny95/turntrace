/**
 * Pure waveform math for the session review timeline: per-channel per-bin
 * peaks from decoded PCM, and the shared normalization scale used to
 * render both lanes on one visual axis. No canvas/DOM access here -- the
 * rendering component (WaveformCanvas) consumes this and draws.
 */

export interface ChannelPeaks {
  /** Per-bin max absolute sample value (full-scale range, typically [-1,1]). */
  max: Float32Array;
  /** Per-bin RMS sample value, same range. */
  rms: Float32Array;
}

/** Splits each channel's samples into `bins` equal-width windows and
 * returns each window's max-abs and RMS. `bins` is floored to >= 1.
 * Channels are mapped by the caller (recording.channelLayout), never by
 * assumed index -- this function is layout-agnostic. */
export function computePeaks(channelData: Float32Array[], bins: number): ChannelPeaks[] {
  const binCount = Math.max(1, Math.floor(bins));
  return channelData.map((data) => {
    const max = new Float32Array(binCount);
    const rms = new Float32Array(binCount);
    const len = data.length;
    if (len === 0) return { max, rms };

    for (let b = 0; b < binCount; b++) {
      const start = Math.floor((b / binCount) * len);
      const end = Math.max(start + 1, Math.min(len, Math.floor(((b + 1) / binCount) * len)));
      let peak = 0;
      let sumSquares = 0;
      let count = 0;
      for (let i = start; i < end; i++) {
        const value = data[i];
        const abs = Math.abs(value);
        if (abs > peak) peak = abs;
        sumSquares += value * value;
        count += 1;
      }
      max[b] = peak;
      rms[b] = count > 0 ? Math.sqrt(sumSquares / count) : 0;
    }
    return { max, rms };
  });
}

/** One shared amplitude scale for every lane, so near-silent channels
 * (e.g. bot audio before/after a freeze) render visually flat instead of
 * being auto-scaled up to fill the lane. `floor` is the minimum scale
 * (default 0.05 full-scale) so a completely silent recording doesn't
 * divide by ~0. */
export function sharedPeakScale(peaks: ChannelPeaks[], floor = 0.05): number {
  let globalPeak = 0;
  for (const channel of peaks) {
    for (const value of channel.max) {
      if (value > globalPeak) globalPeak = value;
    }
  }
  return Math.max(globalPeak, floor);
}

/** Normalizes an amplitude value to [0,1] against a shared scale. */
export function normalizeAmplitude(value: number, scale: number): number {
  if (!(scale > 0)) return 0;
  return Math.max(0, Math.min(1, value / scale));
}
