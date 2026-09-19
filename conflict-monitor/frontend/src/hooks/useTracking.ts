import { useEffect, useRef, useState } from "react";

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
  status: "ok" | "no_integrity_data" | "insufficient_coverage";
  source: string | null;
  as_of: number;
  cells_evaluated: number;
  aircraft_evaluable: number;
}

export type SensorKey = "bgp" | "ping-slash24" | "merit-nt" | "gtr";

/** One IODA sensor, scored against its OWN 24h baseline. */
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
  /** Fraction, e.g. -0.31. null whenever the sensor is unavailable. */
  deviation: number | null;
  depressed: boolean;
  points: number;
  /** 24h buckets, downsampled. null = not measured in that bucket, NOT zero. */
  spark: (number | null)[];
  /** Seconds between the last real measurement and the poll. IODA's ingest lag
   *  is part of how old the number is, so the row's age adds it in. */
  data_age: number | null;
}

export interface CountryConnectivity {
  code: string;
  name: string;
  sensors: Record<SensorKey, ConnectivitySensor>;
  sensors_available: number;
  sensors_depressed: number;
  /** The backend never averages the four; this is the corroboration verdict. */
  state: "nominal" | "partial" | "disruption" | "degraded";
  /** Most negative deviation among AVAILABLE sensors. null = nothing measured. */
  worst_deviation: number | null;
  as_of: number;
}

export interface ConnectivityStatus {
  status: "ok" | "no_data" | "unavailable";
  as_of: number;
  /** Backend poll cadence in seconds - what "stale" means for these rows. */
  poll_interval: number;
  cloudflare: { configured: boolean };
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

export function useTracking() {
  const [aircraft, setAircraft] = useState<Aircraft[]>([]);
  const [tleData, setTleData] = useState<TLERecord[]>([]);
  const [jammingZones, setJammingZones] = useState<JammingZone[]>([]);
  const [jammingStatus, setJammingStatus] = useState<JammingStatus>({
    status: "no_integrity_data",
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
    cloudflare: { configured: false },
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
        if (res.ok && mountedRef.current) setAircraft(await res.json());
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
            cloudflare: data.cloudflare ?? { configured: false },
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

  return { aircraft, tleData, jammingZones, jammingStatus, vessels, aircraftTracks, vesselTracks, connectivity, connectivityStatus };
}
