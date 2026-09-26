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
import type {
  Aircraft,
  JammingStatus,
  JammingZone,
  TLERecord,
  Vessel,
} from "../hooks/useTracking";
import { FEED_WORD, feedAge, type FeedEnvelope } from "../lib/feed";
import { formatAge } from "../lib/tokens";
import { STALE_AFTER, useFeedAges, useNow, type FeedAges } from "../lib/useFeedAges";
import type { ConflictEvent } from "../types/event";
import { IndicatorRow, type RailState, type Row } from "./IndicatorRow";

interface IndicatorRailProps {
  /** Events inside the active time window. */
  events: ConflictEvent[];
  /** The whole retained stream, for stream currency. */
  allEvents: ConflictEvent[];
  aircraft: Aircraft[];
  /** The aircraft feed's state, from the server; ADS-B currency comes from here. */
  aircraftFeed: FeedEnvelope<Aircraft>;
  aircraftReceivedAt: number | null;
  /** The vessel feed's state, from the server; AIS currency comes from here. */
  vesselsFeed: FeedEnvelope<Vessel>;
  vesselsReceivedAt: number | null;
  /** The satellites feed's state, from the server; TLE currency comes from here. */
  tleFeed: FeedEnvelope<TLERecord>;
  tleReceivedAt: number | null;
  vessels: Vessel[];
  tleData: TLERecord[];
  jammingStatus: JammingStatus;
  /** Cells the backend already judged to be over its integrity threshold. */
  jammingZones: JammingZone[];
  isConnected: boolean;
  demoMode: boolean;
}

/** A feed as FEED CURRENCY sees it, when the server reports its state. */
interface ServerFeed {
  age: number | null;
  live: boolean;
  reported: boolean;
  /** Not configured (e.g. no key): named, and left out of the count - it is not silent. */
  unconfigured: boolean;
}

/**
 * ADS-B's, AIS's and TLE's entries are the server's verdict: "fresh" only when
 * the feed is live. GNSS is still an arrival age, measured here - it is derived
 * from the aircraft feed, and its row already withholds the verdict when that
 * feed stops. An arrival cannot tell a dead upstream from a backend re-serving
 * its cache, which is how 4/4 WATCH sat over a dead fleet.
 */
function feedCurrencyRow(ages: FeedAges, adsb: ServerFeed, ais: ServerFeed, tle: ServerFeed): Row {
  const all = [
    { key: "ADS-B", age: adsb.reported ? adsb.age : null, fresh: adsb.live, off: adsb.unconfigured },
    { key: "AIS", age: ais.reported ? ais.age : null, fresh: ais.live, off: ais.unconfigured },
    { key: "GNSS", age: ages.gnss, fresh: ages.gnss != null && ages.gnss <= STALE_AFTER.gnss, off: false },
    { key: "TLE", age: tle.reported ? tle.age : null, fresh: tle.live, off: tle.unconfigured },
  ];
  const off = all.filter((f) => f.off);
  const feeds = all.filter((f) => !f.off);
  const offNote = off.length ? ` · ${off.map((f) => f.key).join(", ")} not configured` : "";
  const reporting = feeds.filter((f) => f.age != null);
  const fresh = reporting.filter((f) => f.fresh);
  const silent = feeds.length - reporting.length;

  let state: RailState;
  let note: string;
  if (reporting.length === 0) {
    state = "NOT-OBSERVED";
    note = `no feed has reported since load${offNote}`;
  } else if (silent > 0) {
    state = "DEGRADED";
    note = `${silent} of ${feeds.length} feeds silent since load${offNote}`;
  } else if (fresh.length < reporting.length) {
    state = "STALE";
    note = `${reporting.length - fresh.length} feed(s) not current${offNote}`;
  } else {
    state = "WATCH";
    note = `every configured feed is current${offNote}`;
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
    detail: all.map((f) => `${f.key} ${f.off ? "off" : formatAge(f.age)}`).join("  ·  "),
  };
}

/**
 * The server says what state the feed is in; this row only chooses words.
 * A count is printed only when the envelope carries one (live or stale), so a
 * dead feed reads "—" and a reason, never a number from its last good poll.
 */
function aircraftRow(feed: FeedEnvelope<Aircraft>, age: number | null): Row {
  const airborne = feed.items.filter((a) => !a.on_ground).length;
  const via = feed.fallback_from ? ` · via ${feed.source} (fallback from ${feed.fallback_from})` : "";
  const why = feed.reason ? ` — ${feed.reason}` : "";
  let state: RailState;
  let value = "—";
  let note: string;
  switch (feed.state) {
    case "live":
      state = feed.count === 0 ? "DEGRADED" : "WATCH";
      value = String(airborne);
      note =
        feed.count === 0
          ? `feed is live and returned no aircraft — coverage unknown${via}`
          : `${feed.count} in feed, ${airborne} airborne${via}`;
      break;
    case "stale":
      state = "STALE";
      value = String(airborne);
      note = `count is real but old${why}${via}`;
      break;
    case "pending":
      state = "NOT-OBSERVED";
      note = `${FEED_WORD.pending.toLowerCase()}${why}`;
      break;
    default:
      // retrying, unavailable, auth_failed, dead, unconfigured
      state = "DEGRADED";
      note = `${FEED_WORD[feed.state].toLowerCase()}${why}`;
  }
  return { name: "AIRCRAFT TRACKED", value, state, note, age };
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
  if (js.status === "feed_not_live") {
    const why = js.feed_state ? FEED_WORD[js.feed_state].toLowerCase() : "not live";
    return row("DEGRADED", "—", `aircraft feed ${why} — last verdict withheld, nothing measured now`);
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

/**
 * Same rules as the aircraft row. "Live" is judged by accepted position
 * reports, not by an open socket, and it is subscription-wide: per-region
 * coverage is not measured yet, and the Gulf has no free-tier receivers.
 */
function vesselRow(feed: FeedEnvelope<Vessel>, age: number | null): Row {
  const why = feed.reason ? ` — ${feed.reason}` : "";
  let state: RailState;
  let value = "—";
  let note: string;
  switch (feed.state) {
    case "live":
      state = feed.count === 0 ? "DEGRADED" : "WATCH";
      value = String(feed.count ?? 0);
      note =
        feed.count === 0
          ? "feed is live and returned no vessels — coverage unknown"
          : "AIS feed live · per-region coverage not measured";
      break;
    case "stale":
      state = "STALE";
      value = String(feed.count ?? 0);
      note = `count is real but old${why}`;
      break;
    case "pending":
      state = "NOT-OBSERVED";
      note = `${FEED_WORD.pending.toLowerCase()}${why}`;
      break;
    default:
      // retrying, unavailable, auth_failed, dead, unconfigured
      state = "DEGRADED";
      note = `${FEED_WORD[feed.state].toLowerCase()}${why}`;
  }
  return { name: "VESSELS TRACKED", value, state, note, age };
}

/**
 * Satellites. Two ages matter and they are different: how long since the last
 * successful fetch (the row's age), and how old the newest ELEMENT SET is (the
 * note). A fresh fetch can carry old orbits; past 14 days the feed reads stale.
 * The epoch age is computed from the server's clock, never the browser's.
 */
function satelliteRow(feed: FeedEnvelope<TLERecord>, age: number | null): Row {
  const why = feed.reason ? ` — ${feed.reason}` : "";
  const epochAge =
    feed.source_epoch != null ? formatAge(Math.max(0, feed.server_now - feed.source_epoch)) : null;
  const epochNote = epochAge ? `newest element set ${epochAge} old` : "no element-set epoch";
  let state: RailState;
  let value = "—";
  let note: string;
  switch (feed.state) {
    case "live":
      state = "WATCH";
      value = String(feed.count ?? 0);
      note = `${epochNote} · ${feed.source ?? "CelesTrak"}`;
      break;
    case "stale":
      state = "STALE";
      value = String(feed.count ?? 0);
      note = `${epochNote}${why}`;
      break;
    case "pending":
      state = "NOT-OBSERVED";
      note = `${FEED_WORD.pending.toLowerCase()}${why}`;
      break;
    default:
      state = "DEGRADED";
      note = `${FEED_WORD[feed.state].toLowerCase()}${why}`;
  }
  return { name: "SATELLITES", value, state, note, age };
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

export function IndicatorRail({
  events,
  allEvents,
  aircraft,
  aircraftFeed,
  aircraftReceivedAt,
  vesselsFeed,
  vesselsReceivedAt,
  tleFeed,
  tleReceivedAt,
  vessels,
  tleData,
  jammingStatus,
  jammingZones,
  isConnected,
  demoMode,
}: IndicatorRailProps) {
  const now = useNow(1000);
  const adsbAge = feedAge(aircraftFeed, aircraftReceivedAt, now);
  const aisAge = feedAge(vesselsFeed, vesselsReceivedAt, now);
  const tleAge = feedAge(tleFeed, tleReceivedAt, now);
  const ages = useFeedAges({
    aircraft,
    vessels,
    tleData,
    jammingStatus,
    latestEventTs: allEvents[0]?.timestamp ?? null,
  });

  const rows: Row[] = [
    feedCurrencyRow(ages, {
      age: adsbAge,
      live: aircraftFeed.state === "live",
      reported: aircraftReceivedAt != null && aircraftFeed.state !== "pending",
      unconfigured: aircraftFeed.state === "unconfigured",
    }, {
      age: aisAge,
      live: vesselsFeed.state === "live",
      reported: vesselsReceivedAt != null && vesselsFeed.state !== "pending",
      unconfigured: vesselsFeed.state === "unconfigured",
    }, {
      age: tleAge,
      live: tleFeed.state === "live",
      reported: tleReceivedAt != null && tleFeed.state !== "pending",
      unconfigured: tleFeed.state === "unconfigured",
    }),
    aircraftRow(aircraftFeed, adsbAge),
    gpsRow(jammingStatus, jammingZones, ages),
    vesselRow(vesselsFeed, aisAge),
    satelliteRow(tleFeed, tleAge),
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
        Row 0: ADS-B, AIS and TLE are judged by the server from the upstream's own
        clock; GNSS still measures arrival, and withholds its verdict when ADS-B stops.
        <br />
        Colour marks the exception only. An unlit row is nominal.
      </div>
    </div>
  );
}
