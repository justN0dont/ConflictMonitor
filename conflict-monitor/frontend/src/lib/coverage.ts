/**
 * Coverage drawn on the map (C83): where the AIS subscription can see and
 * where it cannot, from the per-region currency the vessels envelope already
 * carries (feeds.RegionTracker, since `76c0c95`).
 *
 * The geometry is the region boxes themselves, resolved exactly as the backend
 * attributes a position to a region: first match wins, in registry order
 * (feeds.region_of). So each box is drawn minus every box before it, and no
 * square degree is claimed by two regions. The boxes are the units the monitor
 * measures by, not seas. Drawn as boxes they hatched the land inside them,
 * which read as a claim about the land (C88), so each region is drawn cut to
 * the sea: data/sea_regions.json holds every box minus the boxes before it,
 * cut to Natural Earth's ocean polygon by tools/sea_regions.py, with the
 * source, commit, checksum and the registry it was cut from inside the file.
 * A region gets that shape only while the live registry still matches the
 * file up to that region (seaGeometry); otherwise it is drawn as its box
 * again, and its label and the legend say so.
 *
 * A region that can vouch for a count (live or stale, the envelope's own rule
 * for printing one) is tinted. A region that cannot is hatched with the lane's
 * "nothing vouched" hatch, and its label carries the state word and the
 * measured known gap. The hatch is the hole; it is never a state colour. A
 * region whose feed is not configured is not drawn: the legend says so, and a
 * map hatched end to end would say "outage" about a key that was never set.
 */
import seaFile from "../data/sea_regions.json";
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

/** GeoJSON MultiPolygon coordinates: polygons of closed [lon, lat] rings, the exterior first. */
export type MultiPolygonCoords = number[][][][];

/** tools/sea_regions.py's output: the registry's boxes cut to the sea, and where they came from. */
export interface SeaFile {
  provenance: { generated: string; registry: { key: string; bbox: number[] }[] };
  features: { properties: { key: string }; geometry: { coordinates: MultiPolygonCoords } }[];
}

export const SEA_REGIONS: SeaFile = seaFile;

/**
 * Each region's sea shape from the file, or null where the renderer must draw
 * the box: a region's shape depends on every box before it, so it holds only
 * while the file's registry matches the live one, in order, up to and
 * including that region - the same key and the same box. A registry edit can
 * never be drawn with yesterday's shape.
 */
export function seaGeometry(regions: readonly RegionCurrency[], file: SeaFile = SEA_REGIONS): (MultiPolygonCoords | null)[] {
  const byKey = new Map(file.features.map((f) => [f.properties.key, f.geometry.coordinates]));
  let matched = true;
  return regions.map((r, i) => {
    const cut = file.provenance.registry[i];
    matched = matched && cut !== undefined && cut.key === r.key && cut.bbox.length === 4 && cut.bbox.every((v, k) => v === r.bbox[k]);
    return matched ? (byKey.get(r.key) ?? null) : null;
  });
}

/** The bounding box of a [lon, lat] ring. */
export function ringBounds(ring: readonly (readonly number[])[]): Rect {
  const r = { latMin: Infinity, latMax: -Infinity, lonMin: Infinity, lonMax: -Infinity };
  for (const [lon, lat] of ring) {
    r.latMin = Math.min(r.latMin, lat);
    r.latMax = Math.max(r.latMax, lat);
    r.lonMin = Math.min(r.lonMin, lon);
    r.lonMax = Math.max(r.lonMax, lon);
  }
  return r;
}

/** Shoelace area of a closed [lon, lat] ring in square degrees, sign dropped. */
export function ringArea(ring: readonly (readonly number[])[]): number {
  let s = 0;
  for (let i = 0; i + 1 < ring.length; i++) s += ring[i][0] * ring[i + 1][1] - ring[i + 1][0] * ring[i][1];
  return Math.abs(s) / 2;
}

export type CoverageFill = "observed" | "hole" | "off";

/** Tinted when the region can vouch for a count; hatched when it cannot; not drawn when never configured. */
export function coverageFill(state: FeedState): CoverageFill {
  if (state === "live" || state === "stale") return "observed";
  if (state === "unconfigured") return "off";
  return "hole";
}

/**
 * Name, count and state word; the known gap on its own line whenever the
 * region is not live; and, when the region is drawn as its box because the
 * sea file no longer matches it, a line that says so.
 */
export function coverageLabel(r: RegionCurrency, asBox = false): string {
  const lines = [r.name.toUpperCase(), `${r.count == null ? "—" : r.count} · ${FEED_WORD[r.state]}`];
  if (r.state !== "live" && r.known_gap) lines.push(`KNOWN GAP · ${r.known_gap}`);
  if (asBox) lines.push("DRAWN AS ITS BOX · registry changed since the sea file was cut");
  return lines.join("\n");
}

export interface CoverageArea {
  key: string;
  name: string;
  state: FeedState;
  fill: CoverageFill;
  /** The region's box minus every box before it. */
  pieces: Rect[];
  /** Those pieces cut to the sea (data/sea_regions.json); null when the file
   *  no longer matches the live registry and the box itself is drawn. */
  sea: MultiPolygonCoords | null;
  label: string;
  /** North-west corner of the largest piece - of the largest sea part when
   *  there is one - where the label hangs like a chart annotation: the centre
   *  is where the traffic is. Null when nothing is left. */
  anchor: { lat: number; lon: number } | null;
}

export function coverageAreas(regions: readonly RegionCurrency[], file: SeaFile = SEA_REGIONS): CoverageArea[] {
  const pieces = effectiveAreas(regions.map((r) => rect(r.bbox)));
  const seas = seaGeometry(regions, file);
  return regions.map((r, i) => {
    const sea = seas[i];
    const largestSea = sea && sea.length > 0 ? ringBounds(sea.reduce((a, b) => (ringArea(b[0]) > ringArea(a[0]) ? b : a))[0]) : null;
    const largest = largestSea ?? pieces[i].reduce<Rect | null>((best, p) => (best === null || area(p) > area(best) ? p : best), null);
    return {
      key: r.key,
      name: r.name,
      state: r.state,
      fill: coverageFill(r.state),
      pieces: pieces[i],
      sea,
      label: coverageLabel(r, sea === null),
      anchor: largest && { lat: largest.latMax, lon: largest.lonMin },
    };
  });
}

/** The legend's explanation of the layer, naming the box fallback whenever it is in force. */
export function coverageNote(areas: readonly CoverageArea[]): string {
  const boxes = areas.filter((a) => a.fill !== "off" && a.sea === null).length;
  const base =
    "AIS coverage by region, from the vessel feed's own per-region currency: tinted where the region can vouch for a count, hatched where it cannot. Each region is the feed's box, the unit the monitor measures by, cut to the sea along Natural Earth's coastline (public domain; data/sea_regions.json carries the source and commit).";
  if (boxes === 0) return base;
  return `${base} ${boxes} region${boxes === 1 ? "" : "s"} drawn as the whole box: the registry changed since the sea file was cut; re-run tools/sea_regions.py.`;
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
 * One collection for the 2D map: a MultiPolygon per drawn region (the sea
 * shape, or the box pieces when the file does not match) and a Point per
 * label, both carrying `key`, `fill` and `label`, so layers filter by
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
        geometry: { type: "MultiPolygon" as const, coordinates: a.sea ?? a.pieces.map((p) => [ring(p)]) },
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

/** Texels per degree of a sea patch on the globe: about 3.5 km, finer than the coastline's 0.01° tolerance. */
export const SEA_PX_PER_DEG = 32;

/**
 * One rectangular piece of a region as the texture of the globe's sphere
 * patch for it: the region's sea shape painted in the piece's own frame (x
 * east from lonMin, y south from latMax, the patch's uv), tinted or hatched,
 * transparent everywhere else; the canvas clips whatever falls outside the
 * piece. The hatch keeps one tile per 1.5° of arc, the density the plain
 * patches use. Needs a document.
 */
export function seaPatchCanvas(piece: Rect, sea: MultiPolygonCoords, fill: CoverageFill): HTMLCanvasElement {
  const c = document.createElement("canvas");
  c.width = Math.max(1, Math.ceil((piece.lonMax - piece.lonMin) * SEA_PX_PER_DEG));
  c.height = Math.max(1, Math.ceil((piece.latMax - piece.latMin) * SEA_PX_PER_DEG));
  const ctx = c.getContext("2d")!;
  if (fill === "hole") {
    const pattern = ctx.createPattern(hatchCanvas(), "repeat")!;
    pattern.setTransform(new DOMMatrix().scale((SEA_PX_PER_DEG * 1.5) / HATCH_SIZE));
    ctx.fillStyle = pattern;
  } else {
    ctx.fillStyle = "rgba(64,224,208,0.08)";
  }
  ctx.beginPath();
  for (const poly of sea) {
    for (const ring of poly) {
      ring.forEach(([lon, lat], i) => {
        const x = (lon - piece.lonMin) * SEA_PX_PER_DEG;
        const y = (piece.latMax - lat) * SEA_PX_PER_DEG;
        if (i === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      });
      ctx.closePath();
    }
  }
  ctx.fill("evenodd");
  return c;
}
