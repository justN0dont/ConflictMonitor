import { useEffect, useRef, useState } from "react";
import type { ConflictEvent } from "../types/event";
import {
  bucketSecondsFor,
  fetchSpan,
  shouldPad,
  windowState,
  windowUrl,
  type DensityBucket,
  type Fetched,
  type LaneBucket,
  type WindowState,
} from "../lib/history";

const API_BASE = (import.meta as any).env?.VITE_API_URL || "http://localhost:8000";
// The route's own cap (routes/events.py, limit le=500). An answer of exactly
// this many rows is taken as truncated: the 500th may be the last row or the
// first of thousands, and the honest reading of the two is the same.
const WINDOW_LIMIT = 500;
// Drag and playback move the window many times a second; the fetch waits for
// a pause, and a fetch in flight is abandoned when the window moves on.
const SETTLE_MS = 150;

/**
 * The events in a scrubbed window, fetched from the server rather than
 * filtered out of the 200 the live store holds (C81). LIVE (range null) owns
 * nothing here. The rules for what an answer covers are in lib/history.ts.
 */
export function useHistory(range: { start: Date; end: Date } | null): WindowState<ConflictEvent> {
  const [fetched, setFetched] = useState<Fetched<ConflictEvent> | null>(null);
  const fetchedRef = useRef(fetched);
  fetchedRef.current = fetched;

  const start = range?.start.getTime() ?? null;
  const end = range?.end.getTime() ?? null;

  useEffect(() => {
    if (start === null || end === null) {
      setFetched(null);
      return;
    }
    const r = { start, end };
    if (windowState(fetchedRef.current, r).loaded) return;
    const ctrl = new AbortController();
    const timer = window.setTimeout(() => {
      const span = shouldPad(fetchedRef.current) ? fetchSpan(r, Date.now()) : r;
      fetch(windowUrl(API_BASE, span, WINDOW_LIMIT), { signal: ctrl.signal })
        .then((res) => res.json())
        .then((data: ConflictEvent[]) => {
          const truncated = data.length >= WINDOW_LIMIT;
          const oldest = truncated ? Math.min(...data.map((e) => Date.parse(e.timestamp))) : null;
          setFetched({
            start: span.start,
            end: span.end,
            truncated,
            oldest,
            exact: span.start === r.start && span.end === r.end,
            events: data,
          });
        })
        .catch(() => {
          /* aborted, or the backend is down: the window stays "not loaded" */
        });
    }, SETTLE_MS);
    return () => {
      window.clearTimeout(timer);
      ctrl.abort();
    };
  }, [start, end]);

  return range === null
    ? { events: [], loaded: true, floor: false }
    : windowState(fetched, { start: range.start.getTime(), end: range.end.getTime() });
}

export interface Lane {
  feed: string;
  label: string;
  bucketS: number;
  buckets: LaneBucket[];
}

export interface TimelineAxis {
  density: { bucketS: number; buckets: DensityBucket[] } | null;
  lanes: Lane[];
}

/** The ingest feeds: the ones whose listening the events on the axis depend on. */
const LANE_FEEDS: { feed: string; label: string }[] = [
  { feed: "telegram", label: "TG" },
  { feed: "rss", label: "RSS" },
];
// About one bucket per two pixels of a wide track.
const AXIS_BUCKETS = 600;

/**
 * The axis under the timeline: event density over the whole table and a
 * liveness lane per ingest feed, both at one bucket size chosen for the span.
 * Refetched when the table's bounds move, which /events/time-range reports
 * every 30 s; identical bounds do not refetch.
 */
export function useTimelineAxis(timeRange: { earliest: Date; latest: Date } | null): TimelineAxis {
  const [axis, setAxis] = useState<TimelineAxis>({ density: null, lanes: [] });
  const earliest = timeRange?.earliest.getTime() ?? null;
  const latest = timeRange?.latest.getTime() ?? null;

  useEffect(() => {
    if (earliest === null || latest === null) return;
    const bucketS = bucketSecondsFor(Math.max(latest - earliest, 60_000), AXIS_BUCKETS);
    const ctrl = new AbortController();
    const after = earliest / 1000;
    const before = latest / 1000 + bucketS; // cover the bucket the newest event sits in
    const density = fetch(`${API_BASE}/events/density?bucket_s=${bucketS}`, { signal: ctrl.signal })
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => (d ? { bucketS: d.bucket_s as number, buckets: d.buckets as DensityBucket[] } : null))
      .catch(() => null);
    const lanes = Promise.all(
      LANE_FEEDS.map(({ feed, label }) =>
        fetch(`${API_BASE}/health/lane?feed=${feed}&after=${after}&before=${before}&bucket_s=${bucketS}`, {
          signal: ctrl.signal,
        })
          .then((r) => (r.ok ? r.json() : null))
          .then((d) => (d ? { feed, label, bucketS, buckets: d.buckets as LaneBucket[] } : null))
          .catch(() => null),
      ),
    );
    Promise.all([density, lanes]).then(([d, ls]) => {
      if (ctrl.signal.aborted) return;
      setAxis({ density: d, lanes: ls.filter((l): l is Lane => l !== null) });
    });
    return () => ctrl.abort();
  }, [earliest, latest]);

  return axis;
}
