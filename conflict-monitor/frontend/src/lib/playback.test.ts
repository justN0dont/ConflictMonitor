/**
 * C44: playback advanced a fixed step per 100 ms tick, so its rate depended on
 * how many ticks fired - 10x its label in a visible tab, less in a throttled
 * one. These pin the replacement: position is a function of elapsed wall time.
 */
import { describe, expect, it } from "vitest";
import { SPEEDS, windowAt } from "./playback";

const HOUR = 3_600_000;
const START = Date.UTC(2026, 8, 20, 0, 0, 0);
const LIVE = START + 48 * HOUR;

describe("windowAt (C44)", () => {
  it("depends on elapsed time, not on how many ticks fired", () => {
    // One tick at +2000 ms, or twenty ticks on the way there: the same window.
    const once = windowAt(1000, START, 6 * HOUR, 10, 3000, LIVE);
    let many = null;
    for (let t = 1100; t <= 3000; t += 100) many = windowAt(1000, START, 6 * HOUR, 10, t, LIVE);
    expect(many).toEqual(once);
  });

  it("plays at the rate its label names", () => {
    for (const { rate, label } of SPEEDS) {
      const w = windowAt(0, START, HOUR, rate, 1000, LIVE)!;
      const dataSecondsPerSecond = (w.start - START) / 1000;
      const named = label.endsWith("min/s") ? parseFloat(label) * 60 : parseFloat(label);
      expect(dataSecondsPerSecond).toBe(named);
    }
  });

  it("keeps the window's width", () => {
    const w = windowAt(0, START, 6 * HOUR, 300, 5000, LIVE)!;
    expect(w.end - w.start).toBe(6 * HOUR);
  });

  it("never passes live: reaching the edge hands back to LIVE", () => {
    // 5 min/s from 42 h before live with a 6 h window reaches live at 504 s.
    expect(windowAt(0, START, 6 * HOUR, 300, 503_000, LIVE)).not.toBeNull();
    expect(windowAt(0, START, 6 * HOUR, 300, 504_000, LIVE)).toBeNull();
    expect(windowAt(0, START, 6 * HOUR, 300, 10_000_000, LIVE)).toBeNull();
  });

  it("does not run backwards on a clock read before the anchor", () => {
    expect(windowAt(5000, START, HOUR, 10, 4000, LIVE)).toEqual({ start: START, end: START + HOUR });
  });
});
