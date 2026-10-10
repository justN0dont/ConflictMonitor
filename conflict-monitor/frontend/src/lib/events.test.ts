/**
 * C50 and the event cap: "new" is an arrival over the live stream, never a
 * diff of whatever list a component was handed, and a capped store says its
 * count is a floor.
 */
import { describe, expect, it } from "vitest";
import type { ConflictEvent } from "../types/event";
import { freshIds, logArrival, mergeEvent } from "./events";

const T0 = Date.UTC(2026, 8, 26, 12, 0, 0);
const ev = (id: number, minutesAgo: number, summary = "") =>
  ({ id, timestamp: new Date(T0 - minutesAgo * 60_000).toISOString(), summary }) as unknown as ConflictEvent;
const held = (n: number) => Array.from({ length: n }, (_, i) => ev(i + 1, i));

describe("mergeEvent", () => {
  it("replaces a re-broadcast merge instead of holding the event twice", () => {
    const store = held(5);
    const merged = mergeEvent(store, ev(3, 2, "merged"), 200);
    expect(merged.known).toBe(true);
    expect(merged.events.filter((e) => e.id === 3)).toHaveLength(1);
    expect(merged.events.find((e) => e.id === 3)!.summary).toBe("merged");
    expect(merged.evicted).toBe(false);
  });

  it("evicts the oldest at the cap and says so", () => {
    const merged = mergeEvent(held(200), ev(999, -1), 200);
    expect(merged.events).toHaveLength(200);
    expect(merged.events[0].id).toBe(999);
    expect(merged.evicted).toBe(true);
  });
});

describe("freshIds (C50)", () => {
  it("flashes nothing when a scrub widens the window over backfilled events", () => {
    const log = new Map<number, number>(); // REST backfill never arrives
    const narrow = held(10).map((e) => e.id);
    const wide = held(200).map((e) => e.id);
    expect(freshIds(log, narrow, T0, 3000).size).toBe(0);
    expect(freshIds(log, wide, T0, 3000).size).toBe(0);
  });

  it("still flashes an arrival when the store is already at its cap", () => {
    const merged = mergeEvent(held(200), ev(999, -1), 200);
    const log = logArrival(new Map(), 999, T0);
    expect(freshIds(log, merged.events.map((e) => e.id), T0 + 500, 3000)).toEqual(new Set([999]));
  });

  it("stops flashing once the window has passed", () => {
    const log = logArrival(new Map(), 7, T0);
    expect(freshIds(log, [7], T0 + 2999, 3000).has(7)).toBe(true);
    expect(freshIds(log, [7], T0 + 3000, 3000).has(7)).toBe(false);
  });

  it("forgets old arrivals", () => {
    const log = logArrival(logArrival(new Map(), 1, T0), 2, T0 + 61_000);
    expect([...log.keys()]).toEqual([2]);
  });
});
