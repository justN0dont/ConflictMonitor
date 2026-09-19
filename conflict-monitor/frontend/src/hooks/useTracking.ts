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

  return { aircraft, tleData, jammingZones, jammingStatus, vessels, aircraftTracks, vesselTracks };
}
