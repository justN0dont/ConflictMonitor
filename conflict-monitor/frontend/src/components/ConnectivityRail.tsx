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
 * SIGNAL. Three or four depressed together is a disruption. One depressed alone
 * is a measurement artifact and says so out loud, which is why the subtitle
 * always names WHICH sensors moved rather than reporting a single score. A mean
 * of the four would turn "gtr dipped, nothing else did" into "25% degraded",
 * which is a number that reads like evidence and is not.
 *
 * Rows use the same component and the same states as IndicatorRail, so DEGRADED
 * means here exactly what it means there: coverage insufficient to evaluate,
 * never a calm reading. A sensor that returned nothing is UNAVAILABLE.
 *
 * Worst-first ordering is load-bearing: nine countries do not fit the panel. It
 * is not sufficient on its own, so the footer counts what is off screen and how
 * many countries nothing is measuring - the absence signal must never be the
 * thing that scrolled away.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import type {
  ConnectivitySensor,
  ConnectivityStatus,
  CountryConnectivity,
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
 * A reading older than two backend poll cycles means a cycle was missed, so the
 * row is reporting the last thing IODA said, not the current state. Matching
 * IndicatorRail: a stale reading is never allowed to pass as a live one.
 */
const STALE_CYCLES = 2;

/** Minus sign, not a hyphen: it sits in a column of tabular figures. */
const MINUS = "−";

/** "-41%" / "-5.9%" / "0%" / "—". Small moves keep a decimal so 0.2% is not "0%". */
function formatDeviation(d: number | null | undefined): string {
  if (d == null || !Number.isFinite(d)) return "—";
  const pct = d * 100;
  if (Math.abs(pct) < 0.05) return "0%";
  const sign = pct < 0 ? MINUS : "+";
  return sign + Math.abs(pct).toFixed(Math.abs(pct) < 10 ? 1 : 0) + "%";
}

/**
 * The sensor the headline number came from: the most depressed AVAILABLE one,
 * which is exactly how the backend computes worst_deviation. The sparkline then
 * draws that same series, so the picture and the number are never two different
 * sensors telling two different stories.
 */
function worstSensor(c: CountryConnectivity): SensorKey | null {
  let worst: SensorKey | null = null;
  for (const key of SENSOR_ORDER) {
    const s: ConnectivitySensor | undefined = c.sensors?.[key];
    if (!s?.available || s.deviation == null) continue;
    const best = worst == null ? null : c.sensors[worst].deviation;
    if (best == null || s.deviation < best) worst = key;
  }
  return worst;
}

/** The one reason every sensor shares, or null when they disagree. */
function sharedReason(c: CountryConnectivity): string | null {
  const reasons = SENSOR_ORDER.map((k) => c.sensors?.[k]?.reason).filter(Boolean);
  if (reasons.length !== SENSOR_ORDER.length) return null;
  return reasons.every((r) => r === reasons[0]) ? (reasons[0] as string) : null;
}

interface Built {
  row: Row;
  country: CountryConnectivity;
  sensor: SensorKey | null;
  lit: boolean;
  /** 0 disruption - 1 partial - 2 cannot see - 3 nominal. */
  rank: number;
}

function buildRow(
  c: CountryConnectivity,
  nowSec: number,
  pollInterval: number,
): Built {
  const available = c.sensors_available;
  const depressed = c.sensors_depressed;
  const pollAge = c.as_of > 0 ? Math.max(0, nowSec - c.as_of) : null;
  const stale = pollAge != null && pollAge > pollInterval * STALE_CYCLES;

  const named = (pick: (s: ConnectivitySensor) => boolean) =>
    SENSOR_ORDER.filter((k) => c.sensors?.[k] && pick(c.sensors[k])).map(
      (k) => SENSOR_LABEL[k],
    );
  const down = named((s) => s.depressed);
  const drawn = worstSensor(c);
  const up = named((s) => s.available);

  // The age of the NUMBER, not the age of the poll. IODA's ingest runs behind
  // wall clock (gtr by well over an hour), so reporting only "we polled 2s ago"
  // would present an hour-old reading as current - the same false-nominal the
  // backend refuses when a sensor goes quiet.
  const dataAge = drawn ? c.sensors[drawn].data_age ?? 0 : 0;
  const age = pollAge == null ? null : pollAge + dataAge;

  // The subtitle carries the corroboration, in words, every time.
  let note: string;
  if (available === 0) {
    const why = REASON_NOTE[sharedReason(c) ?? ""] ?? "no sensor returned data";
    note = why + " — not measured";
  } else if (available === 1) {
    note = "only " + up[0] + " measuring — cannot corroborate";
  } else if (depressed === 0) {
    // Calm countries drawing the same trace is real, not decorative - but only
    // if the row says which sensor is on screen.
    // "0/4 depressed", not "0/4 sensors depressed": with the longest sensor
    // name the fuller wording wraps to a second line and that row alone stands
    // 15px taller than its neighbours.
    note = "0/" + available + " depressed" +
      (drawn ? " · worst: " + SENSOR_LABEL[drawn] : "");
  } else if (depressed === 1) {
    note = "1/" + available + " sensors — " + down[0] + " only, likely artifact";
  } else if (depressed === 2) {
    note = "2/" + available + " sensors agree — partial: " + down.join(", ");
  } else {
    note = depressed + "/" + available + " sensors agree: " + down.join(", ");
  }

  // State and rank come off the backend's verdict, never off a score this
  // component invents. "Cannot see" outranks nominal: that is the whole design.
  let state: RailState;
  let rank: number;
  if (c.state === "disruption") {
    state = "TRIPPED";
    rank = 0;
  } else if (depressed === 2) {
    // Two independent sensors agreeing is not enough to call a disruption, and
    // it is far too much to render as a calm row - which is what happened while
    // this shared the nominal state with a country at -1%.
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

  return {
    row: {
      name: c.name.toUpperCase(),
      value: formatDeviation(c.worst_deviation),
      state,
      note,
      age,
    },
    country: c,
    sensor: drawn,
    // A corroborated depression gets its trace lit too: the cliff is the
    // evidence, and drawing it in the same muted ink as a flat line was the
    // reason a 40% two-sensor drop read as calm.
    lit: state === "TRIPPED" || state === "PARTIAL",
    rank,
  };
}

/** The whole section has nothing to report - one honest row, not nine blanks. */
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
        .map((c) => buildRow(c, nowSec, status.poll_interval))
        .sort(
          (a, b) =>
            a.rank - b.rank ||
            // Most negative first. A country with nothing measured has no
            // deviation at all, so it sorts after the ones that do.
            (a.country.worst_deviation ?? 1) - (b.country.worst_deviation ?? 1) ||
            a.row.name.localeCompare(b.row.name),
        ),
    [countries, nowSec, status.poll_interval],
  );

  // How many rows are off screen right now. Measured, not estimated: the panel
  // holds about four of nine rows, and a list that silently truncates is the
  // wrong default for a panel whose whole thesis is that absence must be seen.
  const scroller = useRef<HTMLDivElement>(null);
  const [offscreen, setOffscreen] = useState(0);
  useEffect(() => {
    const el = scroller.current;
    if (!el) return;
    const measure = () => {
      // Rects, not offsetTop: the scroller is not a positioned ancestor, so
      // offsetTop is measured from somewhere else entirely and reported all
      // nine rows as hidden.
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
          rows.map(({ row, country, sensor, lit }) => {
            const s = sensor ? country.sensors[sensor] : null;
            return (
              <IndicatorRow
                key={country.code}
                row={row}
                spark={
                  <Sparkline
                    values={s?.spark ?? []}
                    baseline={s?.baseline ?? null}
                    lit={lit}
                    color={STATUS[STATE_STATUS[row.state]].color}
                    title={
                      sensor
                        ? SENSOR_LABEL[sensor] +
                          " — 24h, as a fraction of its level at this hour yesterday"
                        : "no sensor available"
                    }
                  />
                }
              />
            );
          })
        )}
      </div>

      <div className="shrink-0 border-t border-[var(--border)] px-3 py-[6px] text-[9px] leading-[1.4] text-[var(--text-muted)]">
        3+ sensors depressed together is a disruption. Two is partial. One alone
        is an artifact.
        <br />
        Value and trace are the worst sensor against its own level at this hour
        yesterday. A silent sensor is unavailable, never normal.
      </div>
    </div>
  );
}
