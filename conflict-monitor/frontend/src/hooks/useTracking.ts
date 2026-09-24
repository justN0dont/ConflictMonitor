import { useEffect, useRef, useState } from "react";
import { emptyEnvelope, FEED_DRAWN, type FeedEnvelope, type FeedState } from "../lib/feed";

export interface Aircraft {
  icao24: string;
  callsign: string;
  origin_country: string;
  lat: number;
  lon: number;
  altitude: number | null;
  velocity: number | null;
  heading: number | null;
  on_ground: boolean;
  position_source: number;
}

export interface TLERecord {
  name: string;
  line1: string;
  line2: string;
}

export interface JammingZone {
  lat: number;
  lon: number;
  radius_km: number;
  degraded: number;
  total: number;
  ratio: number;
  intensity: number;
}

export interface JammingStatus {
  /**
   * "feed_not_live": the aircraft feed is not live or stale, so the backend
   * withholds its last verdict and serves no zones (C31). `feed_state` says why.
   */
  status: "ok" | "no_integrity_data" | "insufficient_coverage" | "feed_not_live";
  feed_state: FeedState | null;
  source: string | null;
  as_of: number;
  cells_evaluated: number;
  aircraft_evaluable: number;
}

export type SensorKey = "bgp" | "ping-slash24" | "merit-nt" | "gtr";

/** One IODA sensor, scored against its OWN baseline and its OWN normal swing. */
export interface ConnectivitySensor {
  /** False means this sensor could not be read OR could not be scored. */
  available: boolean;
  /**
   * Why it is unavailable, in the backend's own words: fetch_failed (IODA did
   * not answer) | no_series | all_null | bad_values | stale_series (it stopped
   * reporting) | insufficient_points | low_baseline (too small to carry a
   * ratio) | score_failed. "I could not reach it" and "it had nothing to say"
   * are different facts and stay different here.
   */
  reason: string | null;
  baseline: number | null;
  current: number | null;
  /**
   * Fraction, e.g. -0.31. The human-readable magnitude - and no longer what
   * decides anything, because the four sensors have wildly different natural
   * variance and -30% means catastrophe on bgp and a normal evening on gtr.
   * null whenever the sensor is unavailable.
   */
  deviation: number | null;
  /** This sensor's own normal swing, MAD/median. The denominator of robust_z. */
  typical: number | null;
  /** deviation / typical. THIS is what decides. null when z_basis is not "relative". */
  robust_z: number | null;
  /**
   * Which rule was applied: "relative" (judged by robust_z) | "flat-sensor-absolute"
   * (no swing to divide by, so z is null and a 2% move is the test) |
   * "unavailable". The UI never has to guess why a null z is null.
   */
  z_basis: "relative" | "flat-sensor-absolute" | "unavailable";
  /**
   * LONG baseline only: the week's slope as a fraction of the baseline per day.
   * A STEADY decline inflates its own denominator and is pinned at z = -2.33
   * whatever its slope, so the backend reports this and never votes with it.
   * null on the short baseline and on any unavailable sensor.
   */
  trend_per_day: number | null;
  depressed: boolean;
  points: number;
  /** 24h buckets, downsampled. null = not measured in that bucket, NOT zero. */
  spark: (number | null)[];
  /** Seconds between the last real measurement and the poll. IODA's ingest lag
   *  is part of how old the number is, so the row's age adds it in. */
  data_age: number | null;
}

/** One Cloudflare Radar outage annotation - a second organisation's reading. */
export interface RadarOutage {
  id: string | null;
  locations: string[];
  event_type: string | null;
  start: string | null;
  end: string | null;
  ongoing: boolean;
  /** POWER_OUTAGE | GOVERNMENT_DIRECTED | CYBERATTACK | NATURAL_DISASTER | ... */
  cause: string | null;
  scope: string | null;
  asns: number[];
}

export interface CountryConnectivity {
  code: string;
  name: string;
  /** SHORT baseline: the same clock hours yesterday. Catches a sudden cut. */
  sensors: Record<SensorKey, ConnectivitySensor>;
  sensors_available: number;
  sensors_depressed: number;
  /**
   * LONG baseline: the last 24h against the median of days 2-7. Catches a
   * sustained decline, which the short baseline cannot see at all - after a day
   * of outage the 24h baseline IS the outage. Never blended with the short one.
   */
  sensors_long: Record<SensorKey, ConnectivitySensor>;
  sensors_long_available: number;
  sensors_long_depressed: number;
  /** The backend never averages the four; this is the corroboration verdict. */
  state:
    | "nominal"
    | "partial"
    | "disruption_sudden"
    | "disruption_sustained"
    | "degraded";
  /** Which baseline the state came off. null when nothing fired. */
  basis: "short" | "long" | "both" | null;
  /** Most negative deviation among AVAILABLE sensors. null = nothing measured. */
  worst_deviation: number | null;
  worst_deviation_long: number | null;
  /**
   * Cloudflare Radar's independent view of the same country. A different
   * organisation, a different method - reported BESIDE the sensor count and
   * never folded into it.
   */
  corroboration: {
    source: string;
    /** False = Radar's OUTAGES call has told us nothing, so neither does this. */
    available: boolean;
    /** True = these are the last good annotations, not this poll's. */
    stale: boolean;
    /** When the annotations being served were actually fetched. */
    as_of: number;
    ongoing_outage: boolean;
    outages: RadarOutage[];
    independent: boolean;
  };
  as_of: number;
}

export interface ConnectivityStatus {
  status: "ok" | "no_data" | "unavailable";
  as_of: number;
  /** Backend poll cadence in seconds - what "stale" means for these rows. */
  poll_interval: number;
  /** The 7-day baseline runs on its own slower clock and is cached between. */
  long_poll_interval: number;
  long_as_of: number;
  long_status: "ok" | "no_data" | "unavailable";
  cloudflare: {
    configured: boolean;
    status: "ok" | "no_data" | "unconfigured" | "error" | "degraded";
    error: string | null;
    as_of: number;
    poll_interval: number;
    /**
     * Radar is TWO calls that fail independently, so each carries its own
     * status: "stale" means the last good answer is being served because this
     * poll's call failed. One combined word could not tell "stale outages,
     * fresh attacks" from its mirror image.
     */
    outages_status: "ok" | "stale" | "error" | "unconfigured" | "no_data";
    outages_as_of: number;
    attacks_status: "ok" | "stale" | "error" | "unconfigured" | "no_data";
    attacks_as_of: number;
    /** Outages in countries NOT on the watchlist - the discovery channel. */
    discovery: RadarOutage[];
    attacks: {
      date_range: string;
      units: unknown;
      normalization: string | null;
      last_updated: string | null;
      targets: {
        code: string | null;
        name: string | null;
        /** Percentage share of OBSERVED layer-7 attack traffic. Has a denominator. */
        share: number | null;
        rank: number | null;
      }[];
    } | null;
  };
}

export interface Vessel {
  mmsi: string;
  name: string;
  lat: number;
  lon: number;
  speed: number;
  heading: number;
  course: number;
  nav_status: number;
  ship_type?: number;
  ship_type_name?: string;
  destination?: string;
  length?: number;
}

// [lon, lat, timestamp]
export type TrackPoint = [number, number, number];
export type TrackHistory = Record<string, TrackPoint[]>;

const API_BASE = (import.meta as any).env?.VITE_API_URL || "http://localhost:8000";
const AIRCRAFT_POLL_MS = 15_000;
const JAMMING_POLL_MS = 15_000;
const VESSEL_POLL_MS = 10_000;
const TLE_POLL_MS = 6 * 3600 * 1000;
const TRACK_POLL_MS = 15_000;
const CONNECTIVITY_POLL_MS = 60_000;
const NO_AIRCRAFT: Aircraft[] = [];

export function useTracking() {
  const [aircraftFeed, setAircraftFeed] = useState<FeedEnvelope<Aircraft>>(() => emptyEnvelope("aircraft"));
  const [aircraftReceivedAt, setAircraftReceivedAt] = useState<number | null>(null);
  const [tleData, setTleData] = useState<TLERecord[]>([]);
  const [jammingZones, setJammingZones] = useState<JammingZone[]>([]);
  const [jammingStatus, setJammingStatus] = useState<JammingStatus>({
    status: "no_integrity_data",
    feed_state: null,
    source: null,
    as_of: 0,
    cells_evaluated: 0,
    aircraft_evaluable: 0,
  });
  const [vessels, setVessels] = useState<Vessel[]>([]);
  const [connectivity, setConnectivity] = useState<CountryConnectivity[]>([]);
  const [connectivityStatus, setConnectivityStatus] = useState<ConnectivityStatus>({
    status: "no_data",
    as_of: 0,
    poll_interval: 300,
    long_poll_interval: 3600,
    long_as_of: 0,
    long_status: "no_data",
    cloudflare: {
      configured: false,
      status: "no_data",
      error: null,
      as_of: 0,
      poll_interval: 900,
      outages_status: "no_data",
      outages_as_of: 0,
      attacks_status: "no_data",
      attacks_as_of: 0,
      discovery: [],
      attacks: null,
    },
  });
  const [aircraftTracks, setAircraftTracks] = useState<TrackHistory>({});
  const [vesselTracks, setVesselTracks] = useState<TrackHistory>({});
  const mountedRef = useRef(true);

  // Poll aircraft positions
  useEffect(() => {
    mountedRef.current = true;
    const fetchAircraft = async () => {
      try {
        const res = await fetch(`${API_BASE}/tracking/aircraft`);
        if (res.ok && mountedRef.current) {
          setAircraftFeed(await res.json());
          setAircraftReceivedAt(Date.now());
        }
      } catch { /* backend unavailable */ }
    };
    fetchAircraft();
    const interval = setInterval(fetchAircraft, AIRCRAFT_POLL_MS);
    return () => { mountedRef.current = false; clearInterval(interval); };
  }, []);

  // Poll jamming zones
  useEffect(() => {
    const fetchJamming = async () => {
      try {
        const res = await fetch(`${API_BASE}/tracking/jamming`);
        if (res.ok) {
          const data = await res.json();
          setJammingZones(data.zones ?? []);
          setJammingStatus({
            status: data.status ?? "no_integrity_data",
            feed_state: data.feed_state ?? null,
            source: data.source ?? null,
            as_of: data.as_of ?? 0,
            cells_evaluated: data.cells_evaluated ?? 0,
            aircraft_evaluable: data.aircraft_evaluable ?? 0,
          });
        }
      } catch { /* backend unavailable */ }
    };
    fetchJamming();
    const interval = setInterval(fetchJamming, JAMMING_POLL_MS);
    return () => clearInterval(interval);
  }, []);

  // Poll measured internet disruption (IODA). Same shape as the jamming poll:
  // a status object beside the data, because an empty list of countries and a
  // feed that has never answered are different facts.
  useEffect(() => {
    const fetchConnectivity = async () => {
      try {
        const res = await fetch(`${API_BASE}/tracking/connectivity`);
        if (res.ok) {
          const data = await res.json();
          setConnectivity(data.countries ?? []);
          setConnectivityStatus({
            status: data.status ?? "no_data",
            as_of: data.as_of ?? 0,
            poll_interval: data.poll_interval ?? 300,
            long_poll_interval: data.long_poll_interval ?? 3600,
            long_as_of: data.long_as_of ?? 0,
            long_status: data.long_status ?? "no_data",
            cloudflare: data.cloudflare ?? {
              configured: false,
              status: "no_data",
              error: null,
              as_of: 0,
              poll_interval: 900,
              outages_status: "no_data",
              outages_as_of: 0,
              attacks_status: "no_data",
              attacks_as_of: 0,
              discovery: [],
              attacks: null,
            },
          });
        }
      } catch { /* backend unavailable */ }
    };
    fetchConnectivity();
    const interval = setInterval(fetchConnectivity, CONNECTIVITY_POLL_MS);
    return () => clearInterval(interval);
  }, []);

  // Poll vessel positions
  useEffect(() => {
    const fetchVessels = async () => {
      try {
        const res = await fetch(`${API_BASE}/tracking/vessels`);
        if (res.ok) setVessels(await res.json());
      } catch { /* backend unavailable */ }
    };
    fetchVessels();
    const interval = setInterval(fetchVessels, VESSEL_POLL_MS);
    return () => clearInterval(interval);
  }, []);

  // Fetch TLE data
  useEffect(() => {
    const fetchTLE = async () => {
      try {
        const res = await fetch(`${API_BASE}/tracking/tle`);
        if (res.ok) setTleData(await res.json());
      } catch { /* backend unavailable */ }
    };
    fetchTLE();
    const interval = setInterval(fetchTLE, TLE_POLL_MS);
    return () => clearInterval(interval);
  }, []);

  // Poll track histories
  useEffect(() => {
    const fetchTracks = async () => {
      try {
        const [acRes, vRes] = await Promise.all([
          fetch(`${API_BASE}/tracking/aircraft/tracks`),
          fetch(`${API_BASE}/tracking/vessels/tracks`),
        ]);
        if (acRes.ok) setAircraftTracks(await acRes.json());
        if (vRes.ok) setVesselTracks(await vRes.json());
      } catch { /* backend unavailable */ }
    };
    fetchTracks();
    const interval = setInterval(fetchTracks, TRACK_POLL_MS);
    return () => clearInterval(interval);
  }, []);

  // The renderers draw last-good items only while the state allows it; the
  // envelope itself goes to the header and rail, which say what it is worth.
  const aircraft = FEED_DRAWN[aircraftFeed.state] ? aircraftFeed.items : NO_AIRCRAFT;

  return { aircraft, aircraftFeed, aircraftReceivedAt, tleData, jammingZones, jammingStatus, vessels, aircraftTracks, vesselTracks, connectivity, connectivityStatus };
}
