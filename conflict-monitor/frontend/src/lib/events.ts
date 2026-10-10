/**
 * The client's event store: what it holds, what arrived, and whether its count
 * can be read as a total.
 *
 * C50: "new" used to be derived by diffing whatever list a component was handed
 * - by length growth in the feed, by id set in the map - so it went silent once
 * the store reached its cap, and flashed old events whenever a scrub widened
 * the window. An arrival is a fact about the live stream, recorded when the
 * WebSocket delivers it. REST backfill never arrives.
 */
import type { ConflictEvent } from "../types/event";

export type ArrivalLog = ReadonlyMap<number, number>;

const byTimeDesc = (a: ConflictEvent, b: ConflictEvent) =>
  new Date(b.timestamp).getTime() - new Date(a.timestamp).getTime();

/**
 * Apply one pushed event to the store, newest first, capped at `max`.
 *
 * `known`: the id was already held. The backend re-broadcasts an event when a
 * duplicate report merges into it (news_feeds.py, telegram.py), and the store
 * used to prepend it again, so one event sat in the list twice. It now
 * replaces the held copy. `evicted`: something fell off the end, so the store
 * no longer holds everything it was sent.
 */
export function mergeEvent(
  held: readonly ConflictEvent[],
  event: ConflictEvent,
  max: number,
): { events: ConflictEvent[]; known: boolean; evicted: boolean } {
  const known = held.some((e) => e.id === event.id);
  const next = known ? held.map((e) => (e.id === event.id ? event : e)) : [event, ...held];
  next.sort(byTimeDesc);
  return { events: next.slice(0, max), known, evicted: next.length > max };
}

/** Record that `id` arrived over the live stream at `now`; forget anything older than `keepMs`. */
export function logArrival(log: ArrivalLog, id: number, now: number, keepMs = 60_000): Map<number, number> {
  const next = new Map([...log].filter(([, at]) => now - at < keepMs));
  next.set(id, now);
  return next;
}

/** The ids among `ids` that arrived within the last `windowMs`. */
export function freshIds(log: ArrivalLog, ids: Iterable<number>, now: number, windowMs: number): Set<number> {
  const out = new Set<number>();
  for (const id of ids) {
    const at = log.get(id);
    if (at != null && now - at < windowMs) out.add(id);
  }
  return out;
}
