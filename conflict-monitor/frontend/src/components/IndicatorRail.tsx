/**
 * IndicatorRail - replaces EscalationGauge.
 *
 * The gauge averaged `severity` over the last 20 events. In the production
 * archive severity is 86.3% constant (always 5), so the needle measured ingest
 * chattiness and reported it as escalation. No formula rescues that field, so
 * nothing here reads severity at all.
 *
 * Every row is an observable we can actually see today, and every row states
 * its own coverage. Row zero is feed currency: before any analytic claim, the
 * thing that says whether we can see at all.
 *
 * DEGRADED means "coverage insufficient to evaluate". It is first-class,
 * because without it a dead sensor launders into a calm reading.
 */
import type { LucideIcon } from "lucide-react";
import type {
  Aircraft,
  JammingStatus,
  JammingZone,
  TLERecord,
  Vessel,
} from "../hooks/useTracking";
import { STATUS, type StatusKey, formatAge } from "../lib/tokens";
import { STALE_AFTER, useFeedAges, type FeedAges } from "../lib/useFeedAges";
import type { ConflictEvent } from "../types/event";

type RailState = "NOT-OBSERVED" | "WATCH" | "TRIPPED" | "STALE" | "DEGRADED";

const STATE_STATUS: Record<RailState, StatusKey> = {
  "NOT-OBSERVED": "unknown",
  WATCH: "good",
  TRIPPED: "critical",
  STALE: "warning",
  DEGRADED: "serious",
};

/**
 * WATCH is the nominal state and carries no hue at all.
 *
 * Two reasons. (1) status-good is #199e70, which is also the ECONOMIC series
 * hue: a green WATCH chip means the same green says "economic" beside an event
 * and "healthy" 300px below it. (2) Five rows of identical green ticks is a
 * wall, not a scan - nothing stands out because everything is lit. Colour is
 * reserved for the exception, so a TRIPPED, STALE or DEGRADED row is the only
 * thing burning in the rail.
 *
 * And per the palette rules, the hue only ever lands on the MARK (the glyph and
 * the row's left edge). Text always wears a text token.
 */
const NOMINAL: RailState = "WATCH";

interface Row {
  name: string;
  value: string;
  state: RailState;
  note: string;
  age: number | null;
  detail?: string;
}

interface IndicatorRailProps {
  /** Events inside the active time window. */
  events: ConflictEvent[];
  /** The whole retained stream, for stream currency. */
  allEvents: ConflictEvent[];
  aircraft: Aircraft[];
  vessels: Vessel[];
  tleData: TLERecord[];
  jammingStatus: JammingStatus;
  /** Cells the backend already judged to be over its integrity threshold. */
  jammingZones: JammingZone[];
  isConnected: boolean;
  demoMode: boolean;
}

function feedCurrencyRow(ages: FeedAges): Row {
  const feeds = [
    { key: "ADS-B", age: ages.adsb, stale: STALE_AFTER.adsb },
    { key: "AIS", age: ages.ais, stale: STALE_AFTER.ais },
    { key: "GNSS", age: ages.gnss, stale: STALE_AFTER.gnss },
    { key: "TLE", age: ages.tle, stale: STALE_AFTER.tle },
  ];
  const reporting = feeds.filter((f) => f.age != null);
  const fresh = reporting.filter((f) => (f.age as number) <= f.stale);
  const silent = feeds.length - reporting.length;

  let state: RailState;
  let note: string;
  if (reporting.length === 0) {
    state = "NOT-OBSERVED";
    note = "no feed has reported since load";
  } else if (silent > 0) {
    state = "DEGRADED";
    note = `${silent} of ${feeds.length} feeds silent since load`;
  } else if (fresh.length < reporting.length) {
    state = "STALE";
    note = `${reporting.length - fresh.length} feed(s) past poll interval`;
  } else {
    state = "WATCH";
    note = "every feed is arriving within its poll interval";
  }

  const worst = reporting.length
    ? Math.max(...reporting.map((f) => f.age as number))
    : null;

  return {
    name: "FEED CURRENCY",
    value: `${fresh.length}/${feeds.length}`,
    state,
    note,
    age: worst,
    detail: feeds.map((f) => `${f.key} ${formatAge(f.age)}`).join("  ·  "),
  };
}

function aircraftRow(aircraft: Aircraft[], ages: FeedAges): Row {
  const airborne = aircraft.filter((a) => !a.on_ground).length;
  let state: RailState;
  let note: string;
  if (ages.adsb == null) {
    state = "NOT-OBSERVED";
    note = "ADS-B has not reported since load";
  } else if (aircraft.length === 0) {
    state = "DEGRADED";
    note = "feed returned no aircraft — coverage unknown";
  } else if (ages.adsb > STALE_AFTER.adsb) {
    state = "STALE";
    note = "count is from a missed poll";
  } else {
    state = "WATCH";
    note = `${aircraft.length} in feed, ${airborne} airborne`;
  }
  return { name: "AIRCRAFT TRACKED", value: String(airborne), state, note, age: ages.adsb };
}

/**
 * GPS interference.
 *
 * `cells_evaluated` is a DENOMINATOR, not a reading. It used to sit in the
 * value column beside 22 aircraft / 24 vessels / 51 events, where a headline
 * "1" read as a magnitude of interference and actually meant "one cell was
 * evaluated" - and it read WATCH, which is exactly the dead sensor laundering
 * into a calm reading that DEGRADED exists to prevent. Coverage now lives in
 * the note, where it is legible as coverage.
 *
 * The value is the count of cells the backend itself emitted as over its
 * integrity threshold (`zones`); the backend applies MIN_CELL_AIRCRAFT and
 * MIN_RATIO, so a zone is its positive detection, not a threshold invented
 * here. Zero zones is a real reading; the worst sub-threshold ratio is not
 * reported by the API, so nothing here pretends to know it.
 */
function gpsRow(js: JammingStatus, zones: JammingZone[], ages: FeedAges): Row {
  const cells = `${js.cells_evaluated} cell${js.cells_evaluated === 1 ? "" : "s"}`;
  const coverage = `from ${cells} evaluated · ${js.aircraft_evaluable} aircraft evaluable`;
  const row = (state: RailState, value: string, note: string): Row => ({
    name: "GPS INTERFERENCE",
    value,
    state,
    note,
    age: ages.gnss,
  });

  if (ages.gnss == null) {
    return row("NOT-OBSERVED", "—", "integrity feed has not reported since load");
  }
  if (js.status === "no_integrity_data") {
    return row("DEGRADED", "—", "no NIC/NACp integrity fields in feed — nothing to judge");
  }
  if (js.status === "insufficient_coverage") {
    return row(
      "DEGRADED",
      "—",
      `coverage insufficient to evaluate — no cell held enough aircraft (${js.aircraft_evaluable} evaluable)`,
    );
  }
  if (js.cells_evaluated === 0) {
    return row("DEGRADED", "—", "no cell was evaluated — coverage insufficient to evaluate");
  }

  const n = zones.length;
  const zoneCount = `${n} zone${n === 1 ? "" : "s"} over the integrity threshold`;

  if (ages.gnss > STALE_AFTER.gnss) {
    return row("STALE", String(n), `${zoneCount}, from a missed poll · ${coverage}`);
  }
  if (n === 0) {
    return row("WATCH", "0", `no cell over the integrity threshold · ${coverage}`);
  }
  const worst = Math.round(Math.max(...zones.map((z) => z.ratio)) * 100);
  return row("TRIPPED", String(n), `${zoneCount} (${worst}% degraded at worst) · ${coverage}`);
}

function vesselRow(vessels: Vessel[], ages: FeedAges): Row {
  let state: RailState;
  let note: string;
  if (ages.ais == null) {
    state = "NOT-OBSERVED";
    note = "AIS has not reported since load";
  } else if (vessels.length === 0) {
    state = "DEGRADED";
    note = "feed returned no vessels — coverage unknown";
  } else if (ages.ais > STALE_AFTER.ais) {
    state = "STALE";
    note = "count is from a missed poll";
  } else {
    state = "WATCH";
    note = "AIS positions in view";
  }
  return { name: "VESSELS TRACKED", value: String(vessels.length), state, note, age: ages.ais };
}

function eventVolumeRow(
  events: ConflictEvent[],
  isConnected: boolean,
  ages: FeedAges,
): Row {
  let state: RailState;
  let note: string;
  if (!isConnected) {
    state = "DEGRADED";
    note = "stream disconnected — count is a floor, not a total";
  } else if (events.length === 0) {
    state = "NOT-OBSERVED";
    note = "no events in the active window";
  } else if (ages.stream != null && ages.stream > STALE_AFTER.stream) {
    state = "STALE";
    note = "nothing new for over 15m";
  } else {
    state = "WATCH";
    note = "in active window · volume is not escalation";
  }
  return { name: "EVENT VOLUME", value: String(events.length), state, note, age: ages.stream };
}

function IndicatorRow({ row, tall }: { row: Row; tall?: boolean }) {
  const status = STATUS[STATE_STATUS[row.state]];
  const Icon: LucideIcon = status.Icon;
  const nominal = row.state === NOMINAL;
  // Hue lands on the mark and the row's left edge only, and only when the row
  // is an exception. Never on text: text wears text tokens.
  const mark = nominal ? "var(--text-muted)" : status.color;
  return (
    <div
      className={`grid grid-cols-[14px_minmax(0,1fr)_auto] items-start gap-x-2 border-t border-[var(--border)] pr-3 pl-[10px] ${
        tall ? "py-2" : "py-[7px]"
      }`}
      style={{ borderLeft: `2px solid ${nominal ? "transparent" : status.color}` }}
    >
      <Icon
        size={13}
        strokeWidth={nominal ? 1.75 : 2.25}
        style={{ color: mark }}
        className="mt-[2px]"
        aria-hidden="true"
      />
      <div className="min-w-0">
        <div className="truncate text-[11px] font-semibold tracking-[0.09em] text-[var(--text-primary)]">
          {row.name}
        </div>
        <div className="mt-[2px] flex min-w-0 items-baseline gap-1.5">
          <span
            className={`shrink-0 text-[9px] font-bold tracking-[0.08em] ${
              nominal ? "text-[var(--text-muted)]" : "text-[var(--text-primary)]"
            }`}
          >
            {row.state}
          </span>
          <span className="min-w-0 text-[10px] leading-[1.35] text-[var(--text-muted)]">
            {row.note}
          </span>
        </div>
        {row.detail && (
          <div
            className="mt-[3px] truncate text-[9.5px] tabular-nums tracking-[0.02em] text-[var(--text-muted)]"
            style={{ fontFamily: "var(--font-mono)" }}
          >
            {row.detail}
          </div>
        )}
      </div>
      <div className="text-right">
        <div className="text-[15px] font-semibold leading-[1.2] tabular-nums text-[var(--text-primary)]">
          {row.value}
        </div>
        <div
          className="mt-[3px] text-[10px] tabular-nums text-[var(--text-secondary)]"
          style={{ fontFamily: "var(--font-mono)" }}
          title="Age of the data behind this row"
        >
          {formatAge(row.age)}
        </div>
      </div>
    </div>
  );
}

export function IndicatorRail({
  events,
  allEvents,
  aircraft,
  vessels,
  tleData,
  jammingStatus,
  jammingZones,
  isConnected,
  demoMode,
}: IndicatorRailProps) {
  const ages = useFeedAges({
    aircraft,
    vessels,
    tleData,
    jammingStatus,
    latestEventTs: allEvents[0]?.timestamp ?? null,
  });

  const rows: Row[] = [
    feedCurrencyRow(ages),
    aircraftRow(aircraft, ages),
    gpsRow(jammingStatus, jammingZones, ages),
    vesselRow(vessels, ages),
    eventVolumeRow(events, isConnected, ages),
  ];

  return (
    <div className="panel flex h-full flex-col">
      <div className="flex items-center justify-between px-3 py-[7px]">
        <span className="text-[11px] font-semibold tracking-[0.18em] text-[var(--text-primary)]">
          INDICATORS
        </span>
        <span className="text-[9px] tracking-[0.1em] text-[var(--text-muted)]">
          {demoMode ? "SYNTHETIC INPUTS" : "OBSERVABLES, NOT SEVERITY"}
        </span>
      </div>

      <div>
        {rows.map((row, i) => (
          <IndicatorRow key={row.name} row={row} tall={i === 0} />
        ))}
      </div>

      <div className="border-t border-[var(--border)] px-3 py-[6px] text-[9px] leading-[1.4] text-[var(--text-muted)]">
        DEGRADED = coverage insufficient to evaluate. It is not a calm reading.
        <br />
        Row 0 measures arrival, not content: a feed can answer on time with nothing in it.
        <br />
        Colour marks the exception only. An unlit row is nominal.
      </div>
    </div>
  );
}
