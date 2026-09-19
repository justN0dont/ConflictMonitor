/**
 * ConnectivityRail - measured internet disruption, one row per watched country.
 *
 * This is the cyber domain done honestly. The Norse-style attack map animated a
 * vendor's own honeypot hits as missile arcs; nothing here draws an attack at
 * all, because nobody can see attacks from the outside. What IS externally
 * measurable is ABSENCE: governments cut connectivity before and during
 * operations, and four independent sensors notice when they do.
 *
 * THE FOUR SENSORS ARE NEVER AVERAGED - the backend keeps them apart and this
 * rail keeps them apart, because AGREEMENT BETWEEN THEM IS THE CONFIDENCE
 * SIGNAL. A majority of the sensors that can see it is a disruption; two that
 * are outvoted is partial; one depressed alone is a measurement artifact and
 * says so out loud, which is why the subtitle always names WHICH sensors moved
 * rather than reporting a single score. A mean of the four would turn "gtr
 * dipped, nothing else did" into "25% degraded", which is a number that reads
 * like evidence and is not.
 *
 * THE PERCENTAGE IS NOT THE HEADLINE, because measurement says it is
 * anti-correlated with importance. Measured 2026-09-19: Lebanon's worst sensor
 * read -4.5% against a typical 24h swing of 12.4% (0.4x normal - a calm
 * evening), while Russia's read -0.2% against a swing of 0.1% (2.6x normal -
 * the most unusual reading on the board). So a nominal row DEMOTES its number
 * to muted 11px, and a fired row leads with MULTIPLES OF THAT SENSOR'S OWN
 * NORMAL SWING - the quantity the backend actually decides on. The percentage
 * rides underneath as the human-readable magnitude, never as the verdict.
 *
 * TWO CLOCKS, ALWAYS LABELLED. A trailing 24h baseline cannot see a sustained
 * decline, because after a day of outage the baseline IS the outage: Cuba's gtr
 * fell 64% across the week while the 24h comparison read nominal. So every row
 * that fires on either clock prints BOTH windows with their names on them -
 * "24h: quiet · 7d: -37% gtr" - and NO NUMBER ANYWHERE IN THIS RAIL IS PRINTED
 * WITHOUT ITS WINDOW: a fired row's headline carries a caption ("NORMAL, 7D"),
 * a calm row's demoted magnitude carries the word "24h" inline. The reader is
 * never left to work out which clock a number came off.
 *
 * ROWS ARE ORDERED BY STATE FIRST, then by severity inside a state. Ranking the
 * calm countries against each other would build a leaderboard out of diurnal
 * noise, so nominal rows sort by name and nothing about their order is meant to
 * be read as a ranking.
 *
 * Rows use the same component and the same states as IndicatorRail, so DEGRADED
 * means here exactly what it means there: coverage insufficient to evaluate,
 * never a calm reading. A sensor that returned nothing is UNAVAILABLE.
 *
 * State-first ordering is load-bearing: ten countries do not fit the panel. It
 * is not sufficient on its own, so the footer counts what is off screen and how
 * many countries nothing is measuring - the absence signal must never be the
 * thing that scrolled away.
 *
 * Nothing in this rail animates: no draw-in, no transition on the sparkline
 * geometry, no pulsing chip. prefers-reduced-motion has nothing to switch off.
 */
import { ShieldCheck } from "lucide-react";
import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import type {
  ConnectivitySensor,
  ConnectivityStatus,
  CountryConnectivity,
  RadarOutage,
  SensorKey,
} from "../hooks/useTracking";
import { STATUS } from "../lib/tokens";
import { useNow } from "../lib/useFeedAges";
import { IndicatorRow, STATE_STATUS, type RailState, type Row } from "./IndicatorRow";
import { Sparkline } from "./Sparkline";

/** Fixed order. Ties for "worst sensor" break toward the leftmost. */
const SENSOR_ORDER: SensorKey[] = ["bgp", "ping-slash24", "merit-nt", "gtr"];

/** Short names for a 400px column. "telescope" is merit-nt, a network telescope. */
const SENSOR_LABEL: Record<SensorKey, string> = {
  bgp: "bgp",
  "ping-slash24": "ping",
  "merit-nt": "telescope",
  gtr: "gtr",
};

/** Which baseline a number came from. Never left for the reader to infer. */
type Baseline = "short" | "long";

const WINDOW_LABEL: Record<Baseline, string> = { short: "24h", long: "7d" };
const WINDOW_CAPS: Record<Baseline, string> = { short: "24H", long: "7D" };

/**
 * Why a country has nothing to report, in the backend's own words. "No sensor
 * returned data" would be a guess: a sensor that stopped reporting an hour ago,
 * one whose normal reading is too small to carry a percentage, and IODA being
 * down are three different facts and the payload distinguishes them.
 */
const REASON_NOTE: Record<string, string> = {
  fetch_failed: "IODA did not answer",
  no_series: "IODA holds no series for this country",
  stale_series: "no sensor has reported recently",
  all_null: "every sensor returned empty buckets",
  bad_values: "every sensor returned unusable values",
  insufficient_points: "too little history to judge",
  low_baseline: "every sensor reads too small to score",
  score_failed: "every sensor failed to score",
};

/**
 * Cloudflare's cause vocabulary, shortened to sit beside a country name. The
 * cause is most of the reason to show the annotation at all: routing intact
 * plus traffic gone is a power grid, while a government pulling the plug
 * withdraws routes. Two different events, and the chip must not blur them into
 * one word "outage".
 */
const CAUSE_SHORT: Record<string, string> = {
  POWER_OUTAGE: "POWER",
  GOVERNMENT_DIRECTED: "GOVT",
  CYBERATTACK: "CYBER",
  NATURAL_DISASTER: "DISASTER",
  CABLE_CUT: "CABLE",
  MILITARY_ACTION: "MILITARY",
  TECHNICAL_PROBLEM: "TECHNICAL",
  UNKNOWN: "UNKNOWN",
};

/**
 * A reading older than two backend poll cycles means a cycle was missed, so the
 * row is reporting the last thing IODA said, not the current state. Matching
 * IndicatorRail: a stale reading is never allowed to pass as a live one.
 */
const STALE_CYCLES = 2;

/** Minus sign, not a hyphen: it sits in a column of tabular figures. */
const MINUS = "−";

/** Narrower than the default, so the value column can carry its caption. */
const SPARK_WIDTH = 56;

/**
 * The backend's two firing rules, which this rail needs for two jobs: putting a
 * flat sensor's absolute verdict and a noisy one's relative verdict on ONE scale
 * for ordering, and scaling each sparkline's box to the rule that judged the
 * sensor it draws. -2% is exactly where the relative rule fires at -4.0, so the
 * two scales meet without a step.
 */
const Z_DEPRESSED = 4.0;
const FLAT_ABSOLUTE = 0.02;
const FLAT_TO_Z = Z_DEPRESSED / FLAT_ABSOLUTE;

/** Median of a non-empty list. Used on a sparkline's own buckets, never on data. */
function mid(xs: number[]): number {
  const s = [...xs].sort((a, b) => a - b);
  const h = s.length >> 1;
  return s.length % 2 ? s[h] : (s[h - 1] + s[h]) / 2;
}

/**
 * Does this trace have anything in it but the reading being judged? Measured on
 * the drawn buckets: MAD about their own median, as a fraction of baseline.
 *
 * MEASURED 2026-09-19 across all 30 available short sensors, this quantity:
 * bgp 0.00-0.05%, ping 0.13-7.5%, gtr 9.9-37.7%. The gap either side of a
 * twentieth of the -2% flat rule is a factor of 2.6 up and 2 down, so the cut is
 * not delicate - it separates "this sensor sits on one number" from "this sensor
 * has a day and a night in it".
 */
function traceIsFlat(s: ConnectivitySensor, rule: number): boolean {
  const b = s.baseline;
  if (b == null || !(b > 0)) return false;
  const r = (s.spark ?? [])
    .filter((v): v is number => v != null && Number.isFinite(v))
    .map((v) => v / b);
  if (r.length < 4) return false;
  const m = mid(r);
  return mid(r.map((x) => Math.abs(x - m))) <= Math.abs(rule) / 20;
}

/**
 * The deviation at which one sensor fires, as a negative fraction: its own swing
 * times the z threshold, or the flat rule's -2%. null keeps the sparkline's wide
 * default box.
 *
 * A box built round the threshold is only honest where THE DRAWN SERIES IS THE
 * JUDGED SERIES. On the 7d baseline it is - seven daily medians against the
 * median of six of them. On the 24h baseline it is not: the trace is raw samples
 * against one number, so it still carries the whole diurnal cycle that the
 * same-clock-hour comparison takes out. Measured today, every non-bgp 24h trace
 * spans 1.5-7x its own firing threshold, so that box would be blown out by the
 * data every time and the sparkline would min-max itself - drawing a calm
 * evening as a collapse, which is the same lie in the other direction. The one
 * 24h trace that does get the tight box is the one with nothing else in it,
 * which is exactly the case a wide box draws as a straight line on a lit row.
 */
function firingThreshold(
  s: ConnectivitySensor | null | undefined,
  b: Baseline,
): number | null {
  if (!s) return null;
  const rule =
    s.z_basis === "flat-sensor-absolute"
      ? -FLAT_ABSOLUTE
      : s.z_basis === "relative" && s.typical != null
        ? -Z_DEPRESSED * s.typical
        : null;
  if (rule == null) return null;
  if (b === "long") return rule;
  return traceIsFlat(s, rule) ? rule : null;
}

/** "-9.4%/day" - the week's slope, which the backend measures and never votes with. */
function formatTrend(t: number | null | undefined): string | null {
  if (t == null || !Number.isFinite(t)) return null;
  const pct = t * 100;
  if (Math.abs(pct) < 0.05) return "flat";
  return (pct < 0 ? MINUS : "+") + Math.abs(pct).toFixed(1) + "%/day";
}

/** "-41%" / "-5.9%" / "0%" / "—". Small moves keep a decimal so 0.2% is not "0%". */
function formatDeviation(d: number | null | undefined): string {
  if (d == null || !Number.isFinite(d)) return "—";
  const pct = d * 100;
  if (Math.abs(pct) < 0.05) return "0%";
  const sign = pct < 0 ? MINUS : "+";
  return sign + Math.abs(pct).toFixed(Math.abs(pct) < 10 ? 1 : 0) + "%";
}

/**
 * "4.9×" - multiples of this sensor's own normal swing, which is the unit the
 * backend's threshold is actually expressed in. The sign is dropped because the
 * state word and the percentage beneath already say which way it moved.
 */
function formatZ(z: number): string {
  return Math.abs(z).toFixed(1) + "×";
}

/** The sensor set for one baseline. `sensors_long` is optional in the payload. */
function setFor(
  c: CountryConnectivity,
  b: Baseline,
): Partial<Record<SensorKey, ConnectivitySensor>> {
  return (b === "long" ? c.sensors_long : c.sensors) ?? {};
}

function availableCount(c: CountryConnectivity, b: Baseline): number {
  return b === "long" ? c.sensors_long_available ?? 0 : c.sensors_available ?? 0;
}

function depressedCount(c: CountryConnectivity, b: Baseline): number {
  return b === "long" ? c.sensors_long_depressed ?? 0 : c.sensors_depressed ?? 0;
}

/** Most negative deviation among the sensors `pick` accepts, or null. */
function mostNegative(
  set: Partial<Record<SensorKey, ConnectivitySensor>>,
  pick: (s: ConnectivitySensor) => boolean,
): SensorKey | null {
  let worst: SensorKey | null = null;
  for (const key of SENSOR_ORDER) {
    const s = set[key];
    if (!s || !pick(s) || s.deviation == null) continue;
    const best = worst == null ? null : set[worst]?.deviation ?? null;
    if (best == null || s.deviation < best) worst = key;
  }
  return worst;
}

/**
 * The sensor a fired row speaks for: the most depressed one on the baseline
 * that fired. Headline, percentage and trace all come from THIS sensor, so the
 * picture and the number are never two sensors telling two different stories.
 */
const leadDepressed = (set: Partial<Record<SensorKey, ConnectivitySensor>>) =>
  mostNegative(set, (s) => s.depressed);

/** The sensor a calm row draws: the most depressed AVAILABLE one. */
const worstAvailable = (set: Partial<Record<SensorKey, ConnectivitySensor>>) =>
  mostNegative(set, (s) => s.available);

/** The one reason every sensor shares, or null when they disagree. */
function sharedReason(c: CountryConnectivity): string | null {
  const reasons = SENSOR_ORDER.map((k) => c.sensors?.[k]?.reason).filter(Boolean);
  if (reasons.length !== SENSOR_ORDER.length) return null;
  return reasons.every((r) => r === reasons[0]) ? (reasons[0] as string) : null;
}

/** Names of the depressed sensors on one baseline, in fixed order. */
function depressedNames(c: CountryConnectivity, b: Baseline): string[] {
  const set = setFor(c, b);
  return SENSOR_ORDER.filter((k) => set[k]?.depressed).map((k) => SENSOR_LABEL[k]);
}

/**
 * One window of the two-clock line: "24h: quiet" / "7d: -37% gtr +1". The
 * window name is never omitted - two unlabelled numbers side by side is exactly
 * the reading this line exists to prevent.
 */
function windowPhrase(c: CountryConnectivity, b: Baseline): string {
  const label = WINDOW_LABEL[b];
  if (availableCount(c, b) === 0) return label + ": not measured";
  const n = depressedCount(c, b);
  if (n === 0) return label + ": quiet";
  const set = setFor(c, b);
  const lead = leadDepressed(set);
  if (!lead) return label + ": " + n + " down";
  return label + ": " + formatDeviation(set[lead]?.deviation) + " " + SENSOR_LABEL[lead];
}

/** The ongoing Cloudflare annotation for this country, if there is one. */
function ongoingOutage(c: CountryConnectivity): RadarOutage | null {
  const corr = c.corroboration;
  if (!corr?.available || !corr.ongoing_outage) return null;
  return (corr.outages ?? []).find((o) => o.ongoing) ?? null;
}

/**
 * The corroboration chip: a SECOND organisation, with a different method,
 * seeing the same country down. Deliberately not part of "2/3" - a sensor count
 * and an independent confirmation are different kinds of evidence, and adding
 * them together would destroy both. Past annotations are not drawn: a shutdown
 * that ended in July corroborates nothing about today.
 *
 * "ONGOING" IS ONLY AN ABSENT END DATE, so the chip carries HOW LONG on its
 * face. Nothing else on screen said whether the second organisation saw this
 * yesterday or in March, and an annotation nobody closed would have gone on
 * corroborating indefinitely in complete silence. The alternative - timing them
 * out after N days - would throw away exactly the long state-ordered shutdowns
 * this panel exists for, so the reader gets the duration and judges it. Days,
 * not the ISO date, because the date costs 30px the name column does not have:
 * with it, CUBA rendered as "C".
 */
function CorroborationChip({
  outage,
  color,
  stale,
  asOf,
}: {
  outage: RadarOutage;
  color: string;
  /** These are the last good annotations, not this poll's. */
  stale: boolean;
  asOf: number;
}) {
  const cause = outage.cause ? CAUSE_SHORT[outage.cause] ?? "OUTAGE" : "OUTAGE";
  const since = outage.start ? outage.start.slice(0, 10) : null;
  const started = outage.start ? Date.parse(outage.start) : NaN;
  const days = Number.isFinite(started)
    ? (Date.now() - started) / 86400000
    : null;
  const ran = days == null ? null : days < 1 ? "<1D" : Math.floor(days) + "D";
  return (
    <span
      className="flex shrink-0 items-center gap-[3px] whitespace-nowrap rounded-[2px] border px-[3px] py-[1px] text-[8px] font-semibold tracking-[0.08em] text-[var(--text-primary)]"
      style={{ borderColor: color }}
      title={
        "Cloudflare Radar reports this country down since " +
        (since ?? "an unknown date") +
        (outage.scope ? ", " + outage.scope.toLowerCase() : "") +
        (outage.cause
          ? ", cause " + outage.cause.toLowerCase().replace(/_/g, " ")
          : "") +
        ". Ongoing means Cloudflare has not closed the annotation" +
        (asOf > 0
          ? ", as of " + new Date(asOf * 1000).toISOString().slice(0, 16).replace("T", " ") + "Z"
          : "") +
        (stale ? " (LAST GOOD - this poll's Radar call failed)" : "") +
        ". A second organisation, a different method - reported beside the sensor count, never added to it."
      }
    >
      <ShieldCheck size={9} strokeWidth={2.25} style={{ color }} aria-hidden="true" />
      CORROBORATED · {cause}
      {stale ? " · LAST GOOD" : ran ? " · " + ran : ""}
    </span>
  );
}

interface Built {
  row: Row;
  country: CountryConnectivity;
  /** The sensor whose trace is drawn, and the baseline it was measured on. */
  sensor: SensorKey | null;
  baseline: Baseline;
  /** Tiny line under the value naming what it is and which window it came from. */
  caption: string | null;
  badge: ReactNode | null;
  lit: boolean;
  /** Nothing is wrong here, so the number must not be the loudest thing in it. */
  quiet: boolean;
  /** 0 disruption - 1 partial - 2 cannot see - 3 nominal. */
  rank: number;
  /** Ordering INSIDE an exception rank. Calm rows never use it. */
  severity: number;
}

function buildRow(
  c: CountryConnectivity,
  nowSec: number,
  pollInterval: number,
  longAsOf: number,
): Built {
  const available = c.sensors_available;
  const depressed = c.sensors_depressed;
  const longAvailable = availableCount(c, "long");
  const longDepressed = depressedCount(c, "long");
  const pollAge = c.as_of > 0 ? Math.max(0, nowSec - c.as_of) : null;
  const stale = pollAge != null && pollAge > pollInterval * STALE_CYCLES;

  const fired =
    c.state === "disruption_sudden" ||
    c.state === "disruption_sustained" ||
    c.state === "partial";

  // Which clock fired. "both" leads with the short one: a cut happening now
  // outranks the decline it sits inside, and the detail line prints both
  // windows anyway, so neither of them disappears.
  const firing: Baseline | null = !fired
    ? null
    : c.basis === "long"
      ? "long"
      : c.basis === "short" || c.basis === "both"
        ? "short"
        : longDepressed > depressed
          ? "long"
          : "short";

  const lead = firing ? leadDepressed(setFor(c, firing)) : null;
  const baseline: Baseline = lead && firing ? firing : "short";
  const sensor = lead ?? worstAvailable(c.sensors ?? {});
  const leadSensor = lead ? setFor(c, baseline)[lead] ?? null : null;

  // THE HEADLINE. On a fired row it is multiples of that sensor's own normal
  // swing, because -30% is a catastrophe on bgp and a normal evening on gtr and
  // one percentage cannot serve both. On a calm row it is the percentage again,
  // but demoted: a number nothing is wrong with must not shout.
  let value: string;
  let caption: string | null;
  if (leadSensor) {
    if (leadSensor.robust_z != null) {
      value = formatZ(leadSensor.robust_z);
      caption = "NORMAL, " + WINDOW_CAPS[baseline];
    } else {
      // A sensor with no swing to divide by has no z at all, so the row prints
      // the magnitude and SAYS which rule judged it, rather than printing a
      // number that does not exist.
      value = formatDeviation(leadSensor.deviation);
      caption = "FLAT SENSOR";
    }
  } else if (sensor) {
    // Demoted to one muted line, window included. Two lines (number over a
    // caption) cost 13px on every calm row, and this panel only has room for
    // four of them - the one thing it must never do is push a real disruption
    // off the bottom to make space for ten flat percentages.
    value = "24h " + formatDeviation(c.sensors?.[sensor]?.deviation);
    caption = null;
  } else {
    value = "—";
    caption = null;
  }

  // The age of the NUMBER, not the age of the poll. IODA's ingest runs behind
  // wall clock (gtr by well over an hour), so reporting only "we polled 2s ago"
  // would present an hour-old reading as current - the same false-nominal the
  // backend refuses when a sensor goes quiet.
  //
  // And a 7d number came off the HOURLY clock, not the 300s one. Adding the
  // short poll's age to a long sensor understated it by up to a full hour -
  // right before a refresh, a row printed 2h01m for a reading that was 2h17m
  // old, on the only row currently firing.
  const dataAge = sensor ? setFor(c, baseline)[sensor]?.data_age ?? 0 : 0;
  const clockAge =
    baseline === "long"
      ? longAsOf > 0
        ? Math.max(0, nowSec - longAsOf)
        : null
      : pollAge;
  const age = clockAge == null ? null : clockAge + dataAge;

  // The subtitle carries the corroboration, in words, every time.
  //
  // THE FIRED ROW SPEAKS FIRST. This chain used to test short-baseline coverage
  // before it tested the verdict, so a country that fired on the 7d clock while
  // its 24h fetches failed rendered TRIPPED with the subtitle "IODA did not
  // answer — not measured": the state and the sentence under it saying opposite
  // things. Coverage of the OTHER clock is a caveat on the finding, not a
  // replacement for it, so it goes on the end of the same line.
  let note: string;
  if (fired && firing) {
    note =
      depressedCount(c, firing) +
      "/" +
      availableCount(c, firing) +
      " down " +
      (firing === "long" ? "over 7d" : "on 24h") +
      ": " +
      depressedNames(c, firing).join(", ");
    // A depressed sensor with no normal swing carries no z, and the reader must
    // not be left to work out why the headline turned into a percentage.
    const flat = SENSOR_ORDER.find(
      (k) =>
        setFor(c, firing)[k]?.depressed &&
        setFor(c, firing)[k]?.z_basis === "flat-sensor-absolute",
    );
    if (flat) {
      note += " · " + SENSOR_LABEL[flat] + " is flat all week, so any move counts";
    }
    const other: Baseline = firing === "long" ? "short" : "long";
    if (availableCount(c, other) === 0) {
      note += " · " + WINDOW_LABEL[other] + " not measured";
    }
  } else if (available === 0) {
    const why = REASON_NOTE[sharedReason(c) ?? ""] ?? "no sensor returned data";
    note = why + " — not measured";
  } else if (available === 1) {
    const up = SENSOR_ORDER.filter((k) => c.sensors?.[k]?.available).map(
      (k) => SENSOR_LABEL[k],
    );
    note = "only " + up[0] + " measuring — cannot corroborate";
  } else if (depressed === 0 && longDepressed > 0) {
    // Nothing moved against yesterday and something is down against the week. A
    // trailing 24h baseline cannot see a sustained decline because it
    // normalises to the outage - so the row says which clock moved, even when
    // the country stays nominal on the sensor count.
    note =
      "24h quiet · " +
      longDepressed +
      "/" +
      longAvailable +
      " dipped over 7d: " +
      depressedNames(c, "long").join(", ");
  } else if (depressed > 0) {
    note =
      depressed +
      "/" +
      available +
      " sensors — " +
      depressedNames(c, "short")[0] +
      " only, likely artifact";
  } else {
    // Both clocks, in words, on the row that has no detail line. The drawn
    // sensor's name used to sit here too and no longer fits beside them - with
    // "telescope" in it the line wraps and that row alone stands 14px taller -
    // so the trace names itself in its own tooltip instead.
    //
    // "7d quiet" is a CLAIM ABOUT THE 7d BASELINE and this row only gets to make
    // it if that baseline was measured. Zero depressed sensors is produced both
    // by "measured and quiet" and by "not measured at all" - and an hourly
    // refresh that fails leaves every row silently asserting a quiet week.
    note =
      "0/" +
      available +
      " depressed · " +
      (longAvailable > 0 ? "24h and 7d quiet" : "24h quiet · 7d not measured");
  }

  // State and rank come off the backend's verdict, never off a score this
  // component invents. "Cannot see" outranks nominal: that is the whole design.
  let state: RailState;
  let rank: number;
  if (c.state === "disruption_sudden" || c.state === "disruption_sustained") {
    state = "TRIPPED";
    rank = 0;
  } else if (c.state === "partial") {
    // Two sensors down and at least as many that could have agreed and did not:
    // not enough to call a disruption, and far too much to render as a calm row
    // - which is what happened while this shared the nominal state with a
    // country at -1%.  Read off the backend's state, not off the short count: a
    // two-sensor agreement on the SEVEN-DAY baseline is the same fact on a
    // slower clock, and counting only sensors_depressed rendered Cuba's
    // sustained collapse as calm.
    state = "PARTIAL";
    rank = 1;
  } else if (available === 0) {
    state = "NOT-OBSERVED";
    rank = 2;
  } else if (available === 1) {
    state = "DEGRADED";
    rank = 2;
  } else if (stale) {
    state = "STALE";
    rank = 2;
  } else {
    state = "WATCH";
    rank = 3;
  }
  if (stale && state !== "STALE") note = note + " · from a missed poll";

  // Both clocks, labelled, on every row where either of them saw something. A
  // row where both are quiet gets no detail line and stays two lines tall, so
  // the taller rows in the panel are exactly the ones with something to say.
  const detail =
    depressed > 0 || longDepressed > 0
      ? windowPhrase(c, "short") + " · " + windowPhrase(c, "long")
      : undefined;

  const outage = ongoingOutage(c);
  const color = STATUS[STATE_STATUS[state]].color;

  return {
    row: { name: c.name.toUpperCase(), value, state, note, age, detail },
    country: c,
    sensor,
    baseline,
    caption,
    badge: outage ? (
      <CorroborationChip
        outage={outage}
        color={color}
        stale={c.corroboration?.stale ?? false}
        asOf={c.corroboration?.as_of ?? 0}
      />
    ) : null,
    // Every fired row gets its trace lit, corroborated or not: the cliff is the
    // evidence, and drawing it in the same muted ink as a flat line was the
    // reason a 40% two-sensor drop read as calm. Corroboration is the chip, and
    // only the chip - it is a different kind of evidence and never changes a
    // count, a colour or a rank.
    lit: state === "TRIPPED" || state === "PARTIAL",
    quiet: state === "WATCH",
    rank,
    // Multiples of normal, so a flat sensor's absolute rule and a noisy
    // sensor's relative one are ordered on one scale rather than by a
    // percentage that means opposite things on the two of them.
    severity: leadSensor
      ? Math.abs(leadSensor.robust_z ?? (leadSensor.deviation ?? 0) * FLAT_TO_Z)
      : 0,
  };
}

/** The whole section has nothing to report - one honest row, not ten blanks. */
function silentRow(status: ConnectivityStatus["status"]): Row {
  return {
    name: "CONNECTIVITY",
    value: "—",
    state: "NOT-OBSERVED",
    note:
      status === "unavailable"
        ? "IODA did not answer — no country is being measured"
        : "no connectivity reading has arrived since load",
    age: null,
  };
}

/**
 * Attack targets - the closest honest thing to "live cyber attacks" this panel
 * can carry. It is a SHARE of the layer-7 attack traffic Cloudflare observed,
 * with a denominator and a stated window. It is not an event count and it is
 * not a map of arcs, and the label says which of those it is rather than
 * leaving the reader to assume the exciting one.
 */
function AttackTargets({ cf }: { cf: ConnectivityStatus["cloudflare"] }) {
  const attacks = cf.attacks;
  const range = attacks?.date_range ? attacks.date_range.toUpperCase() : "";
  // Keyed off the ATTACKS call, not off a combined status: Radar's two calls
  // fail independently, and the word that covered both of them rendered a
  // preserved answer as current 1D data with nothing to mark it.
  const stale = cf.attacks_status === "stale";
  let body: ReactNode;
  if (!cf.configured) {
    body = "Cloudflare Radar not configured — no attack share to report";
  } else if (!attacks && cf.attacks_status === "error") {
    body = "Cloudflare Radar did not answer" + (cf.error ? " — " + cf.error : "");
  } else if (!attacks || attacks.targets.length === 0) {
    body = "no attack share has arrived yet";
  } else {
    body = (
      <div className="flex flex-wrap gap-x-[6px] gap-y-0">
        {attacks.targets.map((t, i) => (
          <span
            key={(t.code ?? "?") + i}
            className="text-[8.5px] tabular-nums text-[var(--text-muted)]"
            style={{ fontFamily: "var(--font-mono)" }}
            title={
              (t.name ?? t.code ?? "unknown") +
              " — " +
              (t.share == null ? "no share" : t.share.toFixed(2) + "%") +
              " of the layer-7 attack traffic Cloudflare observed over " +
              (range || "the window") +
              ". A share of observed traffic, not a count of attacks."
            }
          >
            <span className="text-[var(--text-secondary)]">{t.code ?? "??"}</span>{" "}
            {t.share == null ? "—" : t.share.toFixed(1)}
          </span>
        ))}
      </div>
    );
  }
  return (
    <div className="shrink-0 border-t border-[var(--border)] px-3 pt-[5px] pb-[6px]">
      <div className="text-[8.5px] tracking-[0.1em] text-[var(--text-muted)]">
        ATTACK TARGETS · % SHARE OF OBSERVED L7 ATTACK TRAFFIC{range ? " · " + range : ""}
        {stale ? " · LAST GOOD" : ""}
      </div>
      <div className="mt-[3px] text-[9px] leading-[1.45] text-[var(--text-muted)]">
        {body}
      </div>
    </div>
  );
}

interface ConnectivityRailProps {
  countries: CountryConnectivity[];
  status: ConnectivityStatus;
}

export function ConnectivityRail({ countries, status }: ConnectivityRailProps) {
  // Ages must climb on their own: a frozen age is indistinguishable from a
  // fresh one, which is the failure this whole rail exists to prevent.
  const nowSec = useNow(1000) / 1000;

  const rows = useMemo(
    () =>
      countries
        .map((c) => buildRow(c, nowSec, status.poll_interval, status.long_as_of))
        .sort(
          (a, b) =>
            // STATE FIRST. Inside an exception, the most unusual reading leads.
            // Inside the calm states, nothing: ordering nine quiet countries by
            // how quiet they are manufactures a leaderboard out of diurnal
            // noise, so they sort by name and say nothing by their position.
            a.rank - b.rank ||
            (a.rank <= 1 ? b.severity - a.severity : 0) ||
            a.row.name.localeCompare(b.row.name),
        ),
    [countries, nowSec, status.poll_interval, status.long_as_of],
  );

  // How many rows are off screen right now. Measured, not estimated: the panel
  // holds about four of ten rows, and a list that silently truncates is the
  // wrong default for a panel whose whole thesis is that absence must be seen.
  const scroller = useRef<HTMLDivElement>(null);
  const [offscreen, setOffscreen] = useState(0);
  useEffect(() => {
    const el = scroller.current;
    if (!el) return;
    const measure = () => {
      // Rects, not offsetTop: the scroller is not a positioned ancestor, so
      // offsetTop is measured from somewhere else entirely and reported all
      // ten rows as hidden.
      const box = el.getBoundingClientRect();
      setOffscreen(
        Array.from(el.children).filter((k) => {
          const r = k.getBoundingClientRect();
          return r.top < box.top - 1 || r.bottom > box.bottom + 1;
        }).length,
      );
    };
    measure();
    el.addEventListener("scroll", measure, { passive: true });
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => {
      el.removeEventListener("scroll", measure);
      ro.disconnect();
    };
  });

  // Countries nothing can corroborate. This is the count that must survive the
  // fold, because "we cannot see Yemen" is the finding, not the footnote.
  const unseen = countries.filter((c) => c.sensors_available < 2).length;

  // The caption carries what the list cannot show, and it rides in the header
  // rather than in a line of its own: a fourth strip of chrome would cost a
  // whole row of the four this panel has room for.
  let caption = "IODA";
  if (countries.length) {
    caption =
      offscreen > 0
        ? `IODA · ${offscreen} OF ${rows.length} OFF SCREEN`
        : `IODA · ${countries.length} COUNTRIES · NOT AVERAGED`;
    if (unseen > 0) caption += ` · ${unseen} NOT MEASURED`;
  }

  return (
    <div className="panel flex h-full flex-col">
      <div className="flex shrink-0 items-center justify-between px-3 py-[7px]">
        <span className="text-[11px] font-semibold tracking-[0.18em] text-[var(--text-primary)]">
          CONNECTIVITY
        </span>
        <span className="text-[9px] tracking-[0.1em] text-[var(--text-muted)]">
          {caption}
        </span>
      </div>

      <div ref={scroller} className="min-h-0 flex-1 overflow-y-auto">
        {rows.length === 0 ? (
          <IndicatorRow row={silentRow(status.status)} />
        ) : (
          rows.map(({ row, country, sensor, baseline, caption: vc, badge, lit, quiet }) => {
            const s = sensor ? setFor(country, baseline)[sensor] ?? null : null;
            return (
              <IndicatorRow
                key={country.code}
                row={row}
                badge={badge}
                quiet={quiet}
                valueCaption={vc}
                spark={
                  <Sparkline
                    values={s?.spark ?? []}
                    baseline={s?.baseline ?? null}
                    lit={lit}
                    width={SPARK_WIDTH}
                    // The box is scaled to the rule that judged THIS sensor, so
                    // a flat sensor's -2% firing point sits where a noisy one's
                    // -50% does instead of drawing as a straight line.
                    threshold={firingThreshold(s, baseline)}
                    window={WINDOW_LABEL[baseline]}
                    color={STATUS[STATE_STATUS[row.state]].color}
                    title={
                      sensor
                        ? SENSOR_LABEL[sensor] +
                          (baseline === "long"
                            ? " — 7 daily medians, as a fraction of the six days before them" +
                              (formatTrend(s?.trend_per_day)
                                ? ". Week's slope " +
                                  formatTrend(s?.trend_per_day) +
                                  ", measured and reported — a steady decline is the one shape the z cannot judge"
                                : "")
                            : " — 24h, as a fraction of its level at this hour yesterday")
                        : "no sensor available"
                    }
                  />
                }
              />
            );
          })
        )}
      </div>

      <AttackTargets cf={status.cloudflare} />

      <div className="shrink-0 border-t border-[var(--border)] px-3 py-[6px] text-[9px] leading-[1.4] text-[var(--text-muted)]">
        Most of the sensors that can see = disruption · 2 that are outvoted =
        partial · 1 alone = artifact. 24h sees a cut, 7d a step down — never
        blended. × is multiples of that sensor's own normal swing.
      </div>
    </div>
  );
}
