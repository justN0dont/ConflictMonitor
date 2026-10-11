/**
 * C83: the map drew an empty Gulf as an empty sea. These pin the coverage
 * geometry (first match wins, like the backend), which regions are tinted,
 * hatched or not drawn, and what a label says. C88: the boxes hatched the
 * land inside them; the sea file's provenance check and the box fallback.
 */
import { describe, expect, it } from "vitest";
import type { RegionCurrency } from "./feed";
import {
  SEA_REGIONS,
  area,
  contains,
  coverageAreas,
  coverageFeatureCollection,
  coverageFill,
  coverageLabel,
  coverageNote,
  effectiveAreas,
  rect,
  ringArea,
  ringBounds,
  seaGeometry,
  subtractRect,
  type MultiPolygonCoords,
  type Rect,
  type SeaFile,
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

/** No sea file at all: every region is drawn as its box. */
const NO_SEA: SeaFile = { provenance: { generated: "", registry: [] }, features: [] };

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

  it("says when the region is drawn as its box, after everything else", () => {
    expect(coverageLabel(region("levant", [30, 37, 32, 37], { name: "Levant", count: 9 }), true)).toBe(
      "LEVANT\n9 · LIVE\nDRAWN AS ITS BOX · registry changed since the sea file was cut",
    );
  });
});

describe("coverageAreas and the feature collection, drawn as boxes", () => {
  const regions = [
    region("gulf", [23.5, 30.5, 47.5, 57], { state: "unavailable", count: null }),
    region("oman_arabia", [10, 26, 51, 65]),
    region("off", [0, 5, 0, 5], { state: "unconfigured", count: null }),
  ];
  const areas = coverageAreas(regions, NO_SEA);

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

// ---------------------------------------------------------------------------
// C88: the sea file against the registry
// ---------------------------------------------------------------------------

/** Ray cast over one closed [lon, lat] ring. */
function inRing(ring: number[][], lon: number, lat: number): boolean {
  let inside = false;
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
    const [xi, yi] = ring[i];
    const [xj, yj] = ring[j];
    if (yi > lat !== yj > lat && lon < ((xj - xi) * (lat - yi)) / (yj - yi) + xi) inside = !inside;
  }
  return inside;
}

/** Inside some part's exterior and outside that part's holes. */
function inSea(sea: MultiPolygonCoords, lat: number, lon: number): boolean {
  return sea.some((poly) => inRing(poly[0], lon, lat) && !poly.slice(1).some((hole) => inRing(hole, lon, lat)));
}

describe("the sea file (tools/sea_regions.py) against the registry", () => {
  const regions = BOXES.map(([k, b]) => region(k, b));
  const seas = seaGeometry(regions);

  it("was cut from the registry the frontend expects, in its order, so every region has a shape", () => {
    expect(SEA_REGIONS.provenance.registry.map((r) => [r.key, r.bbox])).toEqual(BOXES);
    expect(seas.map((s) => s !== null)).toEqual(BOXES.map(() => true));
  });

  it("holds closed rings of at least four points that stay inside their region's box", () => {
    seas.forEach((sea, i) => {
      const box = rect(BOXES[i][1]);
      expect(sea!.length).toBeGreaterThan(0);
      for (const poly of sea!) {
        for (const ring of poly) {
          expect(ring.length).toBeGreaterThanOrEqual(4);
          expect(ring[0]).toEqual(ring[ring.length - 1]);
          for (const [lon, lat] of ring) expect(contains(box, lat, lon)).toBe(true);
        }
      }
    });
  });

  it("draws the water and not the land", () => {
    const [gulf, omanArabia, redSea, , , , caspian] = seas as MultiPolygonCoords[];
    expect(inSea(redSea, 20, 38)).toBe(true); // mid Red Sea
    expect(inSea(redSea, 12.5, 45)).toBe(true); // Gulf of Aden
    expect(inSea(redSea, 24.1, 32.9)).toBe(false); // Aswan
    expect(inSea(redSea, 22.8, 32.6)).toBe(false); // Lake Nasser: inland water inside a box is not drawn
    expect(inSea(gulf, 26.5, 52)).toBe(true); // the Persian Gulf, to the first match
    expect(inSea(omanArabia, 26.5, 52)).toBe(false);
    expect(inSea(gulf, 25.4, 49.6)).toBe(false); // Al-Hofuf
    expect(inSea(omanArabia, 24, 60)).toBe(true); // Gulf of Oman
    expect(inSea(caspian, 42, 50.5)).toBe(true); // the Caspian: Natural Earth draws it as ocean
  });

  it("gives up the shape, region by region, as soon as the live registry stops matching the file", () => {
    const moved = regions.map((r, i) => (i === 3 ? { ...r, bbox: [30, 37, 32, 38] as [number, number, number, number] } : r));
    const s = seaGeometry(moved);
    expect(s.slice(0, 3).every((x) => x !== null)).toBe(true);
    expect(s.slice(3).every((x) => x === null)).toBe(true);
    // The order changed, or a region renamed: the same from that point on.
    expect(seaGeometry([regions[1], regions[0], ...regions.slice(2)]).every((x) => x === null)).toBe(true);
    expect(seaGeometry(regions.map((r, i) => (i === 7 ? { ...r, key: "w_med" } : r)))[7]).toBeNull();
  });

  it("says on the label and in the legend when a region is drawn as its box", () => {
    const areas = coverageAreas(regions);
    expect(areas.every((a) => a.sea !== null)).toBe(true);
    expect(areas[0].label).not.toContain("DRAWN AS ITS BOX");
    expect(coverageNote(areas)).not.toContain("drawn as the whole box");
    const stale = coverageAreas(regions.map((r, i) => (i === 7 ? { ...r, bbox: [25, 45, -6, 25] as [number, number, number, number] } : r)));
    expect(stale[7].sea).toBeNull();
    expect(stale[7].pieces.length).toBeGreaterThan(0);
    expect(stale[7].label).toContain("DRAWN AS ITS BOX");
    expect(coverageNote(stale)).toContain("1 region drawn as the whole box");
  });

  it("anchors the label at the north-west corner of the largest sea part and ships the sea shape, not the box", () => {
    const areas = coverageAreas(regions);
    const largest = areas[0].sea!.reduce((a, b) => (ringArea(b[0]) > ringArea(a[0]) ? b : a));
    const bounds = ringBounds(largest[0]);
    expect(areas[0].anchor).toEqual({ lat: bounds.latMax, lon: bounds.lonMin });
    expect(bounds.lonMin).toBeGreaterThan(47.5); // the water starts east of the box's west edge, in Kuwait Bay
    const fc = coverageFeatureCollection(areas);
    const poly = fc.features.find((f) => f.properties.key === "gulf" && f.geometry.type === "MultiPolygon")!;
    expect(poly.geometry.coordinates).toBe(areas[0].sea);
  });
});
