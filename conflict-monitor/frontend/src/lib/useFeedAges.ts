/**
 * Data currency, measured client-side on purpose.
 *
 * The backend does not stamp per-feed freshness, so the only thing this client
 * honestly knows is "when did a payload for this feed last arrive here". Each
 * poll in useTracking replaces its array/object, so a change of identity IS an
 * arrival; a failed poll leaves the identity alone and the age keeps climbing.
 *
 * null means "nothing has arrived since this page loaded" - which is not the
 * same as "fresh", and must never render as 0s.
 *
 * The 1s tick lives inside this hook so each consumer re-renders on its own
 * instead of re-rendering the whole app (and the map) every second.
 */
import { useEffect, useRef, useState } from "react";

export interface FeedAges {
  /** seconds since the last aircraft payload arrived, or null */
  adsb: number | null;
  /** seconds since the last vessel payload arrived, or null */
  ais: number | null;
  /** seconds since the last TLE payload arrived, or null */
  tle: number | null;
  /** seconds since the last GNSS-integrity payload arrived, or null */
  gnss: number | null;
  /** seconds since the newest event's own timestamp, or null when none */
  stream: number | null;
}

/** Poll cadence in useTracking, so "stale" means "we missed one". */
export const STALE_AFTER: Record<keyof FeedAges, number> = {
  adsb: 90,          // polls every 15s
  ais: 60,           // polls every 10s
  tle: 7 * 3600,     // polls every 6h
  gnss: 90,          // polls every 15s
  stream: 15 * 60,   // events are event-driven; 15m of silence is worth saying
};

const UNSEEN = Symbol("unseen");

/** Wall-clock now, re-read every `intervalMs`. */
function useNow(intervalMs = 1000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(id);
  }, [intervalMs]);
  return now;
}

/** Timestamp of the last time `value` changed identity, or null if never. */
function useLastArrival(value: unknown): number | null {
  const [stamp, setStamp] = useState<number | null>(null);
  const seen = useRef<unknown>(UNSEEN);

  useEffect(() => {
    if (seen.current === value) return; // same payload (or a StrictMode remount)
    const first = seen.current === UNSEEN;
    seen.current = value;
    if (first) return; // mount-time initial value is not an arrival
    setStamp(Date.now());
  }, [value]);

  return stamp;
}

export interface FeedInputs {
  aircraft: unknown[];
  vessels: unknown[];
  tleData: unknown[];
  jammingStatus: unknown;
  /** ISO timestamp of the newest event seen, or null. */
  latestEventTs: string | null;
}

export function useFeedAges({
  aircraft,
  vessels,
  tleData,
  jammingStatus,
  latestEventTs,
}: FeedInputs): FeedAges {
  const now = useNow(1000);
  const adsbAt = useLastArrival(aircraft);
  const aisAt = useLastArrival(vessels);
  const tleAt = useLastArrival(tleData);
  const gnssAt = useLastArrival(jammingStatus);

  const since = (at: number | null) => (at == null ? null : (now - at) / 1000);
  const eventAt = latestEventTs ? new Date(latestEventTs).getTime() : NaN;

  return {
    adsb: since(adsbAt),
    ais: since(aisAt),
    tle: since(tleAt),
    gnss: since(gnssAt),
    stream: Number.isFinite(eventAt) ? Math.max(0, (now - eventAt) / 1000) : null,
  };
}
