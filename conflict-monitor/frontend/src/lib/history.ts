/**
 * The timeline over the whole table (C81; Phase 3, the feed-liveness lane).
 *
 * The axis spans every row in the table (/events/time-range), but until this
 * module the ticks on it came from the 200 newest events the client held and
 * a scrub filtered those same 200. On the five-month archive that drew most of
 * the axis as quiet and a window five months back as empty - "nothing
 * happened" standing in for "nothing was fetched". Now:
 *
 *   - the axis shows /events/density, counts over the whole table;
 *   - a lane under it shows /health/lane, whether each ingest feed was
 *     listening - so a quiet day and a day the monitor was down are
 *     different shapes;
 *   - a scrub fetches the window it shows (useHistory), and says LOADING
 *     while it has not, and "+" when the server's cap made the count a floor.
 *
 * Everything here is pure so vitest can pin it; the hooks only do I/O.
 */

export interface DensityBucket {
  /** ISO start of the bucket. */
  at: string;
  total: number;
  /** Rows with a geometry: placed somewhere. total - located could not be placed. */
  located: number;
}

export interface LaneBucket {
  /** Epoch seconds, start of the bucket. */
  at: number;
  /** A FeedState, or "unknown" (nothing vouched), or "not_retained" (pruned). */
  state: string;
  attempts: number;
  successes: number;
}

export interface Range {
  start: number;
  end: number;
}

/** Bucket widths the axis may ask for, in seconds. */
export const BUCKET_LADDER_S = [60, 300, 900, 1800, 3600, 3 * 3600, 6 * 3600, 12 * 3600, 86400, 7 * 86400];

/** The smallest bucket on the ladder that keeps `spanMs` under `maxBuckets` buckets. */
export function bucketSecondsFor(spanMs: number, maxBuckets: number): number {
  for (const b of BUCKET_LADDER_S) if (spanMs / 1000 / b <= maxBuckets) return b;
  return BUCKET_LADDER_S[BUCKET_LADDER_S.length - 1];
}

export interface Bar {
  leftPct: number;
  widthPct: number;
  total: number;
  located: number;
  /** total / the tallest bucket: linear, so twice the bar is twice the events. */
  heightFrac: number;
}

/** Density buckets as bars on an axis [rangeStart, rangeStart + fullSpan]. */
export function densityBars(
  buckets: readonly DensityBucket[],
  bucketS: number,
  rangeStart: number,
  fullSpan: number,
): Bar[] {
  const max = Math.max(0, ...buckets.map((b) => b.total));
  if (max === 0) return [];
  const widthPct = ((bucketS * 1000) / fullSpan) * 100;
  return buckets
    .map((b) => ({
      leftPct: ((Date.parse(b.at) - rangeStart) / fullSpan) * 100,
      widthPct,
      total: b.total,
      located: b.located,
      heightFrac: b.total / max,
    }))
    .filter((b) => b.leftPct + b.widthPct > 0 && b.leftPct < 100);
}

export interface LaneBox {
  leftPct: number;
  widthPct: number;
  state: string;
  startMs: number;
  endMs: number;
}

/** Adjacent same-state buckets merged into boxes, clipped to the axis. */
export function laneBoxes(
  buckets: readonly LaneBucket[],
  bucketS: number,
  rangeStart: number,
  fullSpan: number,
): LaneBox[] {
  const runs: { state: string; startMs: number; endMs: number }[] = [];
  for (const b of buckets) {
    const startMs = b.at * 1000;
    const endMs = startMs + bucketS * 1000;
    const last = runs[runs.length - 1];
    if (last && last.state === b.state && last.endMs === startMs) last.endMs = endMs;
    else runs.push({ state: b.state, startMs, endMs });
  }
  const axisEnd = rangeStart + fullSpan;
  return runs
    .map((r) => {
      const s = Math.max(r.startMs, rangeStart);
      const e = Math.min(r.endMs, axisEnd);
      return { ...r, leftPct: ((s - rangeStart) / fullSpan) * 100, widthPct: ((e - s) / fullSpan) * 100 };
    })
    .filter((r) => r.widthPct > 0);
}

export type LaneFill = "good" | "warning" | "critical" | "off" | "unknown" | "not_retained";

/**
 * What a lane state is drawn as. Only a recorded feed state gets a status
 * colour; "unknown" and "not_retained" are holes, in two hatches, and must
 * never be painted as if someone had been looking.
 */
export function laneFill(state: string): LaneFill {
  switch (state) {
    case "live":
      return "good";
    case "stale":
    case "retrying":
    case "pending":
      return "warning";
    case "unavailable":
    case "dead":
    case "auth_failed":
      return "critical";
    case "unconfigured":
      return "off";
    case "not_retained":
      return "not_retained";
    default:
      return "unknown";
  }
}

/** What one window fetch brought back, and the span it answers for. */
export interface Fetched<E> {
  /** The span that was asked for. */
  start: number;
  end: number;
  /** The server returns the newest `limit` rows of the span; at the cap the
   *  rest is simply not there. */
  truncated: boolean;
  /** The oldest timestamp returned (ms), when truncated: the span is answered
   *  exactly from here to `end`, and not before. */
  oldest: number | null;
  /** Whether the span was exactly the window asked for, with no padding. */
  exact: boolean;
  events: E[];
}

/**
 * The span to fetch for a window: the window plus one window on each side,
 * so playback and the arrow keys reuse one answer. Never past `liveMs`.
 */
export function fetchSpan(range: Range, liveMs: number): Range {
  const w = range.end - range.start;
  return { start: range.start - w, end: Math.min(liveMs, range.end + w) };
}

export function windowUrl(apiBase: string, span: Range, limit: number): string {
  const after = new Date(span.start).toISOString();
  const before = new Date(span.end).toISOString();
  return `${apiBase}/events?after=${after}&before=${before}&limit=${limit}`;
}

/** Events with a timestamp inside [start, end], in the order given. */
export function inRange<E extends { timestamp: string }>(events: readonly E[], range: Range): E[] {
  return events.filter((e) => {
    const t = Date.parse(e.timestamp);
    return t >= range.start && t <= range.end;
  });
}

export interface WindowState<E> {
  events: E[];
  /** The window is answered: nothing more will come for it without a new fetch. */
  loaded: boolean;
  /** The count is a lower bound: the cap cut the window's older part off. */
  floor: boolean;
}

/**
 * What a fetch can say about `range`. A fetch answers exactly for
 * [oldest-returned-or-start, end]. A window inside that is loaded and exact.
 * A window reaching before the oldest returned row of a truncated answer is
 * loaded only as a floor - and only once the fetch was exact, because a
 * padded fetch that hit the cap has not yet tried the window on its own.
 * Anything else is not loaded: the events shown are whatever the old answer
 * had there, which may be nothing, and the caller must say so.
 */
export function windowState<E extends { timestamp: string }>(
  fetched: Fetched<E> | null,
  range: Range,
): WindowState<E> {
  if (fetched === null) return { events: [], loaded: false, floor: false };
  const known = fetched.truncated && fetched.oldest !== null ? fetched.oldest : fetched.start;
  const inside = range.end <= fetched.end && range.start >= known;
  const cappedBefore =
    fetched.truncated && range.end <= fetched.end && range.start < known && range.start >= fetched.start;
  return {
    events: inRange(fetched.events, range),
    loaded: inside || (cappedBefore && fetched.exact),
    floor: cappedBefore,
  };
}

/** Whether the next fetch should pad: not after an answer that hit the cap. */
export function shouldPad<E>(last: Fetched<E> | null): boolean {
  return !(last && last.truncated);
}
