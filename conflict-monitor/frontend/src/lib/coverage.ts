/**
 * Coverage drawn on the map (C83): where the AIS subscription can see and
 * where it cannot, from the per-region currency the vessels envelope already
 * carries (feeds.RegionTracker, since `76c0c95`).
 *
 * The geometry is the region boxes themselves, resolved exactly as the backend
 * attributes a position to a region: first match wins, in registry order
 * (feeds.region_of). So each box is drawn minus every box before it, and no
 * square degree is claimed by two regions. The boxes are the units the monitor
 * measures by, not seas: land inside one is the box's reach, not a claim about
 * the land. Sea polygons with provenance are DISPLAY-14 in GODS-EYE-VIEW.md.
 *
 * A region that can vouch for a count (live or stale, the envelope's own rule
 * for printing one) is tinted. A region that cannot is hatched with the lane's
 * "nothing vouched" hatch, and its label carries the state word and the
 * measured known gap. The hatch is the hole; it is never a state colour. A
 * region whose feed is not configured is not drawn: the legend says so, and a
 * map hatched end to end would say "outage" about a key that was never set.
 */
import { FEED_WORD, type FeedState, type RegionCurrency } from "./feed";

export interface Rect {
  latMin: number;
  latMax: number;
  lonMin: number;
  lonMax: number;
}

/** From the envelope's [lat_min, lat_max, lon_min, lon_max]. */
export function rect(bbox: readonly [number, number, number, number]): Rect {
  return { latMin: bbox[0], latMax: bbox[1], lonMin: bbox[2], lonMax: bbox[3] };
}

export function area(r: Rect): number {
  return (r.latMax - r.latMin) * (r.lonMax - r.lonMin);
}

export function contains(r: Rect, lat: number, lon: number): boolean {
  return lat >= r.latMin && lat <= r.latMax && lon >= r.lonMin && lon <= r.lonMax;
}

/** `a` minus `b`: up to four rectangles that tile `a` outside `b`. */
export function subtractRect(a: Rect, b: Rect): Rect[] {
  const lo = Math.max(a.lonMin, b.lonMin);
  const hi = Math.min(a.lonMax, b.lonMax);
  const bot = Math.max(a.latMin, b.latMin);
  const top = Math.min(a.latMax, b.latMax);
  if (lo >= hi || bot >= top) return [a];
  const out: Rect[] = [];
  if (a.latMax > top) out.push({ latMin: top, latMax: a.latMax, lonMin: a.lonMin, lonMax: a.lonMax });
  if (a.latMin < bot) out.push({ latMin: a.latMin, latMax: bot, lonMin: a.lonMin, lonMax: a.lonMax });
  if (a.lonMin < lo) out.push({ latMin: bot, latMax: top, lonMin: a.lonMin, lonMax: lo });
  if (a.lonMax > hi) out.push({ latMin: bot, latMax: top, lonMin: hi, lonMax: a.lonMax });
  return out;
}

/** Each box minus every box before it: the area the backend attributes to it. */
export function effectiveAreas(boxes: readonly Rect[]): Rect[][] {
  return boxes.map((box, i) => {
    let pieces = [box];
    for (const earlier of boxes.slice(0, i)) pieces = pieces.flatMap((p) => subtractRect(p, earlier));
    return pieces;
  });
}

export type CoverageFill = "observed" | "hole" | "off";

/** Tinted when the region can vouch for a count; hatched when it cannot; not drawn when never configured. */
export function coverageFill(state: FeedState): CoverageFill {
  if (state === "live" || state === "stale") return "observed";
  if (state === "unconfigured") return "off";
  return "hole";
}

/** Name, count and state word; the known gap on its own line whenever the region is not live. */
export function coverageLabel(r: RegionCurrency): string {
  const lines = [r.name.toUpperCase(), `${r.count == null ? "—" : r.count} · ${FEED_WORD[r.state]}`];
  if (r.state !== "live" && r.known_gap) lines.push(`KNOWN GAP · ${r.known_gap}`);
  return lines.join("\n");
}

export interface CoverageArea {
  key: string;
  name: string;
  state: FeedState;
  fill: CoverageFill;
  /** The region's box minus every box before it. */
  pieces: Rect[];
  label: string;
  /** North-west corner of the largest piece, where the label hangs like a chart
   *  annotation - the centre is where the traffic is; null when nothing is left. */
  anchor: { lat: number; lon: number } | null;
}

export function coverageAreas(regions: readonly RegionCurrency[]): CoverageArea[] {
  const pieces = effectiveAreas(regions.map((r) => rect(r.bbox)));
  return regions.map((r, i) => {
    const largest = pieces[i].reduce<Rect | null>((best, p) => (best === null || area(p) > area(best) ? p : best), null);
    return {
      key: r.key,
      name: r.name,
      state: r.state,
      fill: coverageFill(r.state),
      pieces: pieces[i],
      label: coverageLabel(r),
      anchor: largest && { lat: largest.latMax, lon: largest.lonMin },
    };
  });
}

/** A closed GeoJSON ring, [lon, lat], counter-clockwise. */
export function ring(r: Rect): [number, number][] {
  return [
    [r.lonMin, r.latMin],
    [r.lonMax, r.latMin],
    [r.lonMax, r.latMax],
    [r.lonMin, r.latMax],
    [r.lonMin, r.latMin],
  ];
}

/**
 * One collection for the 2D map: a MultiPolygon per drawn region and a Point
 * per label, both carrying `key`, `fill` and `label`, so layers filter by
 * geometry type and fill.
 */
export function coverageFeatureCollection(areas: readonly CoverageArea[]) {
  const drawn = areas.filter((a) => a.fill !== "off" && a.pieces.length > 0);
  return {
    type: "FeatureCollection" as const,
    features: [
      ...drawn.map((a) => ({
        type: "Feature" as const,
        properties: { key: a.key, fill: a.fill, label: a.label },
        geometry: { type: "MultiPolygon" as const, coordinates: a.pieces.map((p) => [ring(p)]) },
      })),
      ...drawn
        .filter((a) => a.anchor !== null)
        .map((a) => ({
          type: "Feature" as const,
          properties: { key: a.key, fill: a.fill, label: a.label },
          geometry: { type: "Point" as const, coordinates: [a.anchor!.lon, a.anchor!.lat] },
        })),
    ],
  };
}

/** The hatch tile every renderer draws a hole with: the lane's 45° hatch, needs a document. */
export const HATCH_SIZE = 12;

export function hatchCanvas(): HTMLCanvasElement {
  const c = document.createElement("canvas");
  c.width = c.height = HATCH_SIZE;
  const ctx = c.getContext("2d")!;
  ctx.strokeStyle = "rgba(200,214,229,0.45)";
  ctx.lineWidth = 1;
  ctx.beginPath();
  // Three 45° segments at 6 px spacing, so the tile repeats seamlessly.
  ctx.moveTo(0, 6);
  ctx.lineTo(6, 0);
  ctx.moveTo(0, 12);
  ctx.lineTo(12, 0);
  ctx.moveTo(6, 12);
  ctx.lineTo(12, 6);
  ctx.stroke();
  return c;
}
