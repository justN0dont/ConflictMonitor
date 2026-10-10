/**
 * C81: the timeline scrubbed the 200 events the client held while its axis
 * spanned the whole table. These pin what replaced that: an axis made of
 * server counts, a lane that draws holes as holes, and a window that says
 * whether it is loaded and whether its count is a floor.
 */
import { describe, expect, it } from "vitest";
import {
  bucketSecondsFor,
  densityBars,
  fetchSpan,
  inRange,
  laneBoxes,
  laneFill,
  shouldPad,
  windowState,
  windowUrl,
  type Fetched,
} from "./history";

const HOUR = 3_600_000;
const T0 = Date.UTC(2026, 2, 1, 0, 0, 0); // 1 Mar 2026
const T0_S = T0 / 1000;

describe("bucketSecondsFor", () => {
  it("picks the smallest ladder step that fits the budget", () => {
    expect(bucketSecondsFor(6 * HOUR, 600)).toBe(60);
    expect(bucketSecondsFor(30 * 24 * HOUR, 600)).toBe(3 * 3600);
    expect(bucketSecondsFor(150 * 24 * HOUR, 600)).toBe(6 * 3600);
  });

  it("never goes below the ladder's top step", () => {
    expect(bucketSecondsFor(50 * 365 * 24 * HOUR, 600)).toBe(7 * 86400);
  });
});

describe("densityBars", () => {
  const buckets = [
    { at: new Date(T0).toISOString(), total: 4, located: 3 },
    { at: new Date(T0 + HOUR).toISOString(), total: 8, located: 2 },
    { at: new Date(T0 + 10 * HOUR).toISOString(), total: 1, located: 0 },
  ];

  it("is linear in the count: twice the events is twice the height", () => {
    const bars = densityBars(buckets, 3600, T0, 12 * HOUR);
    expect(bars.map((b) => b.heightFrac)).toEqual([0.5, 1, 0.125]);
    expect(bars.map((b) => b.located)).toEqual([3, 2, 0]);
  });

  it("places and sizes a bar by its bucket on the axis", () => {
    const [a, b] = densityBars(buckets, 3600, T0, 12 * HOUR);
    expect(a.leftPct).toBe(0);
    expect(b.leftPct).toBeCloseTo(100 / 12);
    expect(a.widthPct).toBeCloseTo(100 / 12);
  });

  it("draws nothing for no events rather than a row of zero-height bars", () => {
    expect(densityBars([], 3600, T0, HOUR)).toEqual([]);
  });

  it("drops buckets off the axis", () => {
    const bars = densityBars(buckets, 3600, T0, 2 * HOUR);
    expect(bars).toHaveLength(2);
  });
});

describe("laneBoxes", () => {
  it("merges adjacent same-state buckets and keeps a state change as an edge", () => {
    const buckets = [
      { at: T0_S, state: "live", attempts: 60, successes: 60 },
      { at: T0_S + 3600, state: "live", attempts: 60, successes: 60 },
      { at: T0_S + 7200, state: "unknown", attempts: 0, successes: 0 },
      { at: T0_S + 10800, state: "live", attempts: 60, successes: 60 },
    ];
    const boxes = laneBoxes(buckets, 3600, T0, 4 * HOUR);
    expect(boxes.map((b) => [b.state, b.leftPct, b.widthPct])).toEqual([
      ["live", 0, 50],
      ["unknown", 50, 25],
      ["live", 75, 25],
    ]);
  });

  it("does not merge across a gap in the buckets", () => {
    const buckets = [
      { at: T0_S, state: "live", attempts: 1, successes: 1 },
      { at: T0_S + 7200, state: "live", attempts: 1, successes: 1 },
    ];
    expect(laneBoxes(buckets, 3600, T0, 3 * HOUR)).toHaveLength(2);
  });

  it("clips the first bucket, which the server aligns down past the axis start", () => {
    const buckets = [{ at: T0_S - 1800, state: "live", attempts: 1, successes: 1 }];
    const [box] = laneBoxes(buckets, 3600, T0, HOUR);
    expect(box.leftPct).toBe(0);
    expect(box.widthPct).toBe(50);
  });
});

describe("laneFill", () => {
  it("gives a hole no status colour", () => {
    expect(laneFill("unknown")).toBe("unknown");
    expect(laneFill("not_retained")).toBe("not_retained");
    expect(laneFill("some_state_added_tomorrow")).toBe("unknown");
  });

  it("maps the recorded states by severity", () => {
    expect(laneFill("live")).toBe("good");
    expect(["stale", "retrying", "pending"].map(laneFill)).toEqual(["warning", "warning", "warning"]);
    expect(["unavailable", "dead", "auth_failed"].map(laneFill)).toEqual(["critical", "critical", "critical"]);
    expect(laneFill("unconfigured")).toBe("off");
  });
});

describe("the window fetch", () => {
  const ev = (t: number) => ({ id: t, timestamp: new Date(t).toISOString() });
  const range = { start: T0 + 6 * HOUR, end: T0 + 12 * HOUR };

  it("pads one window on each side, never past live", () => {
    expect(fetchSpan(range, T0 + 100 * HOUR)).toEqual({ start: T0, end: T0 + 18 * HOUR });
    expect(fetchSpan(range, T0 + 13 * HOUR)).toEqual({ start: T0, end: T0 + 13 * HOUR });
  });

  it("asks the server for the span in ISO, with the limit it will hold", () => {
    expect(windowUrl("http://x", { start: T0, end: T0 + HOUR }, 500)).toBe(
      "http://x/events?after=2026-03-01T00:00:00.000Z&before=2026-03-01T01:00:00.000Z&limit=500",
    );
  });

  it("is not loaded before any answer, and shows nothing", () => {
    expect(windowState(null, range)).toEqual({ events: [], loaded: false, floor: false });
  });

  it("is loaded and exact when the window sits inside an untruncated answer", () => {
    const fetched: Fetched<ReturnType<typeof ev>> = {
      start: T0, end: T0 + 18 * HOUR, truncated: false, oldest: null, exact: false,
      events: [ev(T0 + 13 * HOUR), ev(T0 + 8 * HOUR), ev(T0 + HOUR)],
    };
    const w = windowState(fetched, range);
    expect(w.loaded).toBe(true);
    expect(w.floor).toBe(false);
    expect(w.events.map((e) => e.id)).toEqual([T0 + 8 * HOUR]);
  });

  it("is not loaded when the window leaves the answered span, and says so", () => {
    const fetched: Fetched<ReturnType<typeof ev>> = {
      start: T0, end: T0 + 10 * HOUR, truncated: false, oldest: null, exact: false,
      events: [ev(T0 + 8 * HOUR)],
    };
    const w = windowState(fetched, range);
    expect(w.loaded).toBe(false);
    // What it can show, it shows; what it cannot, it does not invent.
    expect(w.events.map((e) => e.id)).toEqual([T0 + 8 * HOUR]);
  });

  it("is a floor when the cap cut off the window's older part, loaded only once tried exactly", () => {
    const padded: Fetched<ReturnType<typeof ev>> = {
      start: T0, end: T0 + 18 * HOUR, truncated: true, oldest: T0 + 9 * HOUR, exact: false,
      events: [ev(T0 + 11 * HOUR), ev(T0 + 9 * HOUR)],
    };
    expect(windowState(padded, range)).toMatchObject({ loaded: false, floor: true });
    const exact: Fetched<ReturnType<typeof ev>> = { ...padded, start: range.start, end: range.end, exact: true };
    expect(windowState(exact, range)).toMatchObject({ loaded: true, floor: true });
  });

  it("is exact when the window sits inside the part a truncated answer covers", () => {
    const fetched: Fetched<ReturnType<typeof ev>> = {
      start: T0, end: T0 + 18 * HOUR, truncated: true, oldest: T0 + 2 * HOUR, exact: false,
      events: [ev(T0 + 7 * HOUR)],
    };
    expect(windowState(fetched, range)).toMatchObject({ loaded: true, floor: false });
  });

  it("stops padding after an answer that hit the cap", () => {
    expect(shouldPad(null)).toBe(true);
    expect(shouldPad({ start: 0, end: 1, truncated: false, oldest: null, exact: false, events: [] })).toBe(true);
    expect(shouldPad({ start: 0, end: 1, truncated: true, oldest: 0, exact: false, events: [] })).toBe(false);
  });

  it("inRange is inclusive at both ends", () => {
    const es = [ev(range.end), ev(range.start), ev(range.start - 1)];
    expect(inRange(es, range).map((e) => e.id)).toEqual([range.end, range.start]);
  });
});
