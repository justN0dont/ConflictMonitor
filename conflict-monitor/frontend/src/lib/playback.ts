/**
 * Timeline playback (FINDINGS C44).
 *
 * The window at any moment is a function of the wall time elapsed since
 * playback was anchored, never of how many ticks have fired. The old loop added
 * a fixed step per 100 ms tick, so its real rate was 10x its "1x" label in a
 * visible tab and something else again in a throttled one. A tick now only
 * decides how often the screen is redrawn, not how far the data moves.
 *
 * Two clocks, never mixed in one expression: elapsed time comes from
 * performance.now() (monotonic, unaffected by the system clock being set), and
 * the live edge from Date.now() (the only clock event timestamps share).
 */

export interface Speed {
  /** Data milliseconds played per wall millisecond. */
  rate: number;
  label: string;
}

/** The rates the old tick loop really played at, now labelled as what they are. */
export const SPEEDS: readonly Speed[] = [
  { rate: 10, label: "10 s/s" },
  { rate: 20, label: "20 s/s" },
  { rate: 50, label: "50 s/s" },
  { rate: 100, label: "100 s/s" },
  { rate: 300, label: "5 min/s" },
];

/**
 * The window `rate` plays into after `nowMs - anchorWallMs` of wall time, having
 * started at `anchorStartMs` and keeping its width `windowMs` - or null once its
 * end would reach `liveMs`, where playback hands back to LIVE.
 *
 * `anchorWallMs` and `nowMs` are performance.now() readings; `anchorStartMs`
 * and `liveMs` are epoch milliseconds.
 */
export function windowAt(
  anchorWallMs: number,
  anchorStartMs: number,
  windowMs: number,
  rate: number,
  nowMs: number,
  liveMs: number,
): { start: number; end: number } | null {
  const start = anchorStartMs + rate * Math.max(0, nowMs - anchorWallMs);
  const end = start + windowMs;
  return end >= liveMs ? null : { start, end };
}
