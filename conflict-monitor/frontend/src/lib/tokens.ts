/**
 * One source of truth for visual identity.
 *
 * Colour is computed, not chosen. The three categorical hues were validated
 * all-pairs against this app's dark panel surface (#0d1219):
 *
 *   node scripts/validate_palette.js "#3987e5,#d95926,#199e70" \
 *     --mode dark --surface "#0d1219" --pairs all      -> ALL CHECKS PASS
 *
 * A map is an all-pairs surface (any two types can sit next to each other), and
 * no four-hue set clears the all-pairs floors on this surface. So exactly three
 * hues exist; every other event type folds into "Other" in secondary ink rather
 * than inventing a fourth hue.
 *
 * Identity is never colour alone: each entry also carries a distinct shape.
 */
import {
  Banknote,
  CircleAlert,
  CircleCheck,
  CircleHelp,
  Crosshair,
  Diamond,
  Handshake,
  OctagonAlert,
  TriangleAlert,
  type LucideIcon,
} from "lucide-react";

export type EventKey = "military" | "diplomatic" | "economic" | "other";

export interface EventVisual {
  key: EventKey;
  label: string;
  /** Categorical hue, or secondary ink for "other". A literal CSS colour:
   *  three.js and Cesium parse this themselves and cannot read a `var()`. */
  color: string;
  Icon: LucideIcon;
}

/** Fixed order. Never cycled, never extended with a generated hue. */
export const EVENT_TYPES: EventVisual[] = [
  { key: "military", label: "Military", color: "#3987e5", Icon: Crosshair },
  { key: "diplomatic", label: "Diplomatic", color: "#d95926", Icon: Handshake },
  { key: "economic", label: "Economic", color: "#199e70", Icon: Banknote },
  { key: "other", label: "Other", color: "#8ea3bb", Icon: Diamond }, // --cat-other
];

const BY_KEY: Record<string, EventVisual> = Object.fromEntries(
  EVENT_TYPES.map((e) => [e.key, e]),
);

/**
 * Visual identity for an event type. Anything that is not one of the three
 * validated hues (cyber, humanitarian, protest, thermal_anomaly, null, a type
 * the backend adds tomorrow) folds to "Other".
 */
export function eventVisual(event_type: string | null | undefined): EventVisual {
  return BY_KEY[event_type ?? ""] ?? BY_KEY.other;
}

export type StatusKey = "good" | "warning" | "serious" | "critical" | "unknown";

export interface StatusVisual {
  color: string;
  Icon: LucideIcon;
  label: string;
}

/**
 * RESERVED for feed liveness and indicator state. Never reused for a category.
 * Always rendered as icon + text label, never colour alone.
 */
export const STATUS: Record<StatusKey, StatusVisual> = {
  good: { color: "#199e70", Icon: CircleCheck, label: "OK" },
  warning: { color: "#c98500", Icon: CircleAlert, label: "WARNING" },
  serious: { color: "#d95926", Icon: TriangleAlert, label: "SERIOUS" },
  critical: { color: "#e66767", Icon: OctagonAlert, label: "CRITICAL" },
  unknown: { color: "var(--text-secondary)", Icon: CircleHelp, label: "UNKNOWN" },
};

/**
 * How precise a resolved coordinate actually is, as reported by the backend
 * geocoder in `geo_precision`. One table, because two surfaces read it (the map
 * marks and the feed rows) and a second copy would go stale the way the four
 * EVENT_COLORS copies did.
 *
 * The rule is: MARK GEOMETRY MUST MATCH EVIDENCE GEOMETRY. A country centroid
 * is a claim about an area hundreds of km across, so it may not render as the
 * same crisp point as a named facility.
 */
export interface PrecisionVisual {
  /** Extra diameter in px added to the core mark to form a diffuse disc. */
  spread: number;
  /** CSS blur radius in px. 0 keeps the mark crisp. */
  blur: number;
  ring: "none" | "soft" | "dashed";
  /** Short human label, lower case. */
  label: string;
  /** True when the coordinate names an area, not a position. */
  coarse: boolean;
}

/** Keyed by the backend's geo_precision tier. */
export const GEO_PRECISION: Record<string, PrecisionVisual> = {
  facility: { spread: 0, blur: 0, ring: "none", label: "facility", coarse: false },
  city: { spread: 5, blur: 0, ring: "soft", label: "city", coarse: false },
  region_named: { spread: 22, blur: 6, ring: "dashed", label: "region-level", coarse: true },
  admin1: { spread: 30, blur: 10, ring: "none", label: "admin-level", coarse: true },
  country_centroid: { spread: 46, blur: 16, ring: "none", label: "country-level", coarse: true },
};

/**
 * null means the precision was never measured - a legacy row, or one written
 * before the geocoder reported tiers. Callers must render that as "unknown
 * extent", never as a precise point.
 */
export function geoPrecision(p: string | null | undefined): PrecisionVisual | null {
  return GEO_PRECISION[p ?? ""] ?? null;
}

/** "±500 m" / "±1,232 km". Returns null when no uncertainty was recorded. */
export function formatUncertainty(m: number | null | undefined): string | null {
  if (m == null || !Number.isFinite(m)) return null;
  return m < 1000
    ? `±${Math.round(m)} m`
    : `±${Math.round(m / 1000).toLocaleString()} km`;
}

/**
 * Age rendering, used everywhere an age is shown: "14s" / "3m41s" / "2h07m".
 * Returns an em dash for "we have never heard from this".
 */
export function formatAge(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds)) return "—";
  const s = Math.max(0, Math.floor(seconds));
  if (s < 60) return `${s}s`;
  if (s < 3600) {
    const m = Math.floor(s / 60);
    return `${m}m${String(s % 60).padStart(2, "0")}s`;
  }
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  return `${h}h${String(m).padStart(2, "0")}m`;
}
