/**
 * C83: the map drew an empty Gulf as an empty sea. These pin the coverage
 * geometry (first match wins, like the backend), which regions are tinted,
 * hatched or not drawn, and what a label says.
 */
import { describe, expect, it } from "vitest";
import type { RegionCurrency } from "./feed";
import {
  area,
  contains,
  coverageAreas,
  coverageFeatureCollection,
  coverageFill,
  coverageLabel,
  effectiveAreas,
  rect,
  subtractRect,
  type Rect,
} from "./coverage";

const R = (latMin: number, latMax: number, lonMin: number, lonMax: number): Rect => ({ latMin, latMax, lonMin, lonMax });

/** The registry as feeds.py holds it (2026-10-10), in its order. */
const BOXES: [string, [number, number, number, number]][] = [
  ["gulf", [23.5, 30.5, 47.5, 57.0]],
  ["oman_arabia", [10.0, 26.0, 51.0, 65.0]],
  ["red_sea", [10.0, 30.0, 32.0, 51.0]],
  ["levant", [30.0, 37.0, 32.0, 37.0]],
  ["e_med", [30.0, 41.0, 25.0, 32.0]],
  ["black_sea", [40.5, 45.0, 27.0, 42.0]],
  ["caspian", [36.0, 45.0, 46.0, 55.0]],
  ["wc_med", [25.0, 45.0, -5.0, 25.0]],
];

function region(key: string, bbox: [number, number, number, number], over: Partial<RegionCurrency> = {}): RegionCurrency {
  return {
    id: `vessels:${key}`,
    key,
    name: key.replace("_", " "),
    bbox,
    role: "ao",
    state: "live",
    reason: null,
    last_success_at: 1,
    source_epoch: 1,
    age_s: 1,
    freshness: "current",
    count: 3,
    known_gap: null,
    ...over,
  };
}

describe("subtractRect", () => {
  it("leaves a box alone when the other does not overlap it", () => {
    expect(subtractRect(R(0, 10, 0, 10), R(20, 30, 20, 30))).toEqual([R(0, 10, 0, 10)]);
    // Touching along an edge is not overlap.
    expect(subtractRect(R(0, 10, 0, 10), R(0, 10, 10, 20))).toEqual([R(0, 10, 0, 10)]);
  });

  it("removes a box entirely when the other covers it", () => {
    expect(subtractRect(R(2, 4, 2, 4), R(0, 10, 0, 10))).toEqual([]);
  });

  it("tiles what is left, without overlap and without loss", () => {
    const a = R(0, 10, 0, 10);
    const hole = R(3, 6, 4, 8);
    const pieces = subtractRect(a, hole);
    expect(pieces).toHaveLength(4);
    expect(pieces.reduce((s, p) => s + area(p), 0)).toBeCloseTo(area(a) - area(hole));
    for (let lat = 0.5; lat < 10; lat += 1) {
      for (let lon = 0.5; lon < 10; lon += 1) {
        const n = pieces.filter((p) => contains(p, lat, lon)).length;
        expect(n).toBe(contains(hole, lat, lon) ? 0 : 1);
      }
    }
  });

  it("cuts a corner into two pieces", () => {
    expect(subtractRect(R(0, 10, 0, 10), R(5, 15, 5, 15))).toHaveLength(2);
  });
});

describe("effectiveAreas", () => {
  const areas = effectiveAreas(BOXES.map(([, b]) => rect(b)));

  it("gives the southern Gulf to the Gulf, the first match, not to Oman & Arabia", () => {
    const inGulf = (p: Rect) => contains(p, 25, 54);
    expect(areas[0].some(inGulf)).toBe(true);
    expect(areas[1].some(inGulf)).toBe(false);
  });

  it("claims no square degree twice, over the whole registry", () => {
    for (let lat = 10.25; lat < 45; lat += 0.5) {
      for (let lon = -4.75; lon < 65; lon += 0.5) {
        const owners = areas.filter((pieces) => pieces.some((p) => contains(p, lat, lon))).length;
        expect(owners).toBeLessThanOrEqual(1);
      }
    }
  });

  it("keeps the first box whole", () => {
    expect(areas[0]).toEqual([rect(BOXES[0][1])]);
  });
});

describe("coverageFill", () => {
  it("tints only a region that can vouch for a count, like the count itself", () => {
    expect(coverageFill("live")).toBe("observed");
    expect(coverageFill("stale")).toBe("observed");
  });

  it("hatches every state that cannot vouch: the hatch is the hole, not a colour", () => {
    for (const s of ["unavailable", "pending", "retrying", "dead", "auth_failed"] as const) {
      expect(coverageFill(s)).toBe("hole");
    }
  });

  it("draws nothing for a feed that was never configured", () => {
    expect(coverageFill("unconfigured")).toBe("off");
  });
});

describe("coverageLabel", () => {
  it("names the region, its count and its state word", () => {
    expect(coverageLabel(region("levant", [30, 37, 32, 37], { name: "Levant", count: 9 }))).toBe("LEVANT\n9 · LIVE");
  });

  it("states the measured known gap whenever the region is not live, never a count it cannot vouch for", () => {
    const gulf = region("gulf", [23.5, 30.5, 47.5, 57], {
      name: "Persian Gulf & Hormuz",
      state: "unavailable",
      count: null,
      known_gap: "no free-tier receivers (C30)",
    });
    expect(coverageLabel(gulf)).toBe("PERSIAN GULF & HORMUZ\n— · UNAVAILABLE\nKNOWN GAP · no free-tier receivers (C30)");
  });

  it("does not raise a known gap over a region that is live now", () => {
    const gulf = region("gulf", [23.5, 30.5, 47.5, 57], { name: "Gulf", count: 4, known_gap: "no free-tier receivers (C30)" });
    expect(coverageLabel(gulf)).toBe("GULF\n4 · LIVE");
  });
});

describe("coverageAreas and the feature collection", () => {
  const regions = [
    region("gulf", [23.5, 30.5, 47.5, 57], { state: "unavailable", count: null }),
    region("oman_arabia", [10, 26, 51, 65]),
    region("off", [0, 5, 0, 5], { state: "unconfigured", count: null }),
  ];
  const areas = coverageAreas(regions);

  it("anchors the label at the north-west corner of the largest piece", () => {
    expect(areas[0].anchor).toEqual({ lat: 30.5, lon: 47.5 });
    // Oman & Arabia minus the Gulf: its largest piece is the band south of lat 23.5.
    expect(areas[1].anchor).toEqual({ lat: 23.5, lon: 51 });
  });

  it("emits a polygon and a label point per drawn region, and nothing for an unconfigured one", () => {
    const fc = coverageFeatureCollection(areas);
    const polys = fc.features.filter((f) => f.geometry.type === "MultiPolygon");
    const points = fc.features.filter((f) => f.geometry.type === "Point");
    expect(polys.map((f) => f.properties.key)).toEqual(["gulf", "oman_arabia"]);
    expect(points.map((f) => f.properties.fill)).toEqual(["hole", "observed"]);
    const g = polys[0].geometry;
    if (g.type !== "MultiPolygon") throw new Error("expected a MultiPolygon");
    expect(g.coordinates[0][0][0]).toEqual([47.5, 23.5]);
  });
});
