/**
 * The feed envelope, mirrored from backend/app/feeds.py.
 *
 * The server computes a feed's state; the client never re-derives it. Two
 * things are done here and nowhere else: the words each state renders as
 * (a Record, so tsc fails if a state is added on one side only), and the age
 * between polls, which is the server's age plus the time since this client
 * received it - a backend that stops answering must keep ageing on screen.
 *
 * Decisions: docs/FINDINGS.md, Roadmap > Phase 2 > "feed_health — schema
 * decisions".
 */

export type FeedState =
  | "dead"
  | "auth_failed"
  | "unavailable"
  | "retrying"
  | "stale"
  | "pending"
  | "live"
  | "unconfigured";

export interface FeedEnvelope<T> {
  schema_version: 1;
  feed: string;
  state: FeedState;
  /** Why the state is not live, or null when it is. */
  reason: string | null;
  source: string | null;
  /** Set when a fallback source served this data, e.g. "adsb.lol". */
  fallback_from: string | null;
  synthetic: boolean;
  source_epoch: number | null;
  last_success_at: number | null;
  server_now: number;
  /** Seconds since the last successful poll, by the server's clock. */
  age_s: number | null;
  freshness: "current" | "old" | "unknown";
  /** null unless the feed is live or stale: a number nobody can vouch for is not printed. */
  count: number | null;
  /** Every zero is "unproven"; "absent" is reserved until the control ring exists. */
  verdict: "present" | "unproven";
  /** The last good items. Whether to DRAW them is decided by FEED_DRAWN. */
  items: T[];
  /** Present on a feed whose currency is kept per region (vessels). */
  regions?: RegionCurrency[];
}

/**
 * One region of a feed, mirrored from feeds.RegionTracker. Its state is never
 * better than the feed's: a live socket says nothing about waters it is not
 * delivering.
 */
export interface RegionCurrency {
  id: string;
  key: string;
  name: string;
  /** lat_min, lat_max, lon_min, lon_max */
  bbox: [number, number, number, number];
  /** "ao" is counted in the headline; "control" is outside the AO, kept to show the socket delivering. */
  role: "ao" | "control" | "outside";
  state: FeedState;
  reason: string | null;
  last_success_at: number | null;
  source_epoch: number | null;
  age_s: number | null;
  freshness: "current" | "old" | "unknown";
  /** null unless the region is live or stale, like the feed's own count. */
  count: number | null;
  /** A measured reason this region cannot be seen, e.g. no receivers. */
  known_gap: string | null;
}

/**
 * The vessel headline counts AO waters only. `count` sums the AO regions that
 * can vouch for a number and is null when none can; `dark` names the AO
 * regions that are not live, so the sum is never read as the whole AO;
 * `outside` is every other row the feed served.
 */
export function aoSummary(env: FeedEnvelope<unknown>): {
  count: number | null;
  dark: RegionCurrency[];
  outside: number | null;
} {
  const ao = (env.regions ?? []).filter((r) => r.role === "ao");
  const vouched = ao.filter((r) => r.count != null);
  const count = vouched.length ? vouched.reduce((n, r) => n + (r.count ?? 0), 0) : null;
  return {
    count,
    dark: ao.filter((r) => r.state !== "live"),
    outside: env.count == null ? null : env.count - (count ?? 0),
  };
}

/** One word per state, for rails and tooltips. */
export const FEED_WORD: Record<FeedState, string> = {
  dead: "COLLECTOR DOWN",
  auth_failed: "REFUSED",
  unavailable: "UNAVAILABLE",
  retrying: "RETRYING",
  stale: "STALE",
  pending: "NO DATA YET",
  live: "LIVE",
  unconfigured: "NOT CONFIGURED",
};

/**
 * Whether last-good items are drawn. Kept through retrying and stale; not
 * drawn once the feed is unavailable (the interim rule until Phase 3 decides
 * hide versus ghost), nor when the collector is dead or refused.
 */
export const FEED_DRAWN: Record<FeedState, boolean> = {
  dead: false,
  auth_failed: false,
  unavailable: false,
  retrying: true,
  stale: true,
  pending: false,
  live: true,
  unconfigured: false,
};

/** Placeholder until the first response: the client knows nothing yet. */
export function emptyEnvelope<T>(feed: string): FeedEnvelope<T> {
  return {
    schema_version: 1,
    feed,
    state: "pending",
    reason: "backend has not answered since load",
    source: null,
    fallback_from: null,
    synthetic: false,
    source_epoch: null,
    last_success_at: null,
    server_now: 0,
    age_s: null,
    freshness: "unknown",
    count: null,
    verdict: "unproven",
    items: [],
  };
}

/**
 * The feed's age now: the server's age at response time plus the seconds
 * since this client received the response. `receivedAt` is Date.now() at
 * receipt; only the elapsed interval on this machine is used, never the
 * browser's clock against the server's.
 */
export function feedAge(
  env: FeedEnvelope<unknown>,
  receivedAt: number | null,
  now: number,
): number | null {
  if (env.age_s == null || receivedAt == null) return null;
  return env.age_s + Math.max(0, (now - receivedAt) / 1000);
}
