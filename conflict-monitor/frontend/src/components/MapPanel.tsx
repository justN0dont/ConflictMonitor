import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { FEED_WORD, type FeedState, type RegionCurrency } from "../lib/feed";
import Map, { Marker, Popup, Source, Layer } from "react-map-gl";
import type { ConflictEvent } from "../types/event";
import type { Aircraft, JammingStatus, JammingZone, TLERecord, TrackHistory, Vessel } from "../hooks/useTracking";
import { GlobeView } from "./GlobeView";
import { CesiumView } from "./CesiumView";
import { isLocated } from "../lib/located";
import { type ArrivalLog, freshIds } from "../lib/events";
import { EVENT_TYPES, eventVisual, formatUncertainty, geoPrecision } from "../lib/tokens";
import { NOW, NOW_CHIP, NOW_TITLE, liveLook } from "../lib/replay";
import { HATCH_SIZE, coverageAreas, coverageFeatureCollection, coverageNote, hatchCanvas } from "../lib/coverage";

const MAPBOX_TOKEN = import.meta.env.VITE_MAPBOX_TOKEN ?? "";

const NO_TRAILS = { type: "FeatureCollection" as const, features: [] as never[] };

/**
 * What each view needs before it can show anything, or null. A view that cannot
 * render is still offered - focusable, aria-disabled - and says which variable
 * is missing, rather than opening a blank panel. TERRAIN is Cesium: with neither
 * an ion token nor a Google key it has no terrain and no imagery of its own
 * (FINDINGS C52 and the untriaged "terrain view renders no basemap").
 */
const VIEW_NEEDS: Record<"2d" | "globe" | "terrain", string | null> = {
  "2d": MAPBOX_TOKEN ? null : "2D map: needs VITE_MAPBOX_TOKEN",
  globe: null,
  terrain:
    import.meta.env.VITE_CESIUM_ION_TOKEN || import.meta.env.VITE_GOOGLE_MAPS_KEY
      ? null
      : "TERRAIN: needs VITE_CESIUM_ION_TOKEN or VITE_GOOGLE_MAPS_KEY",
};

interface MapPanelProps {
  events: ConflictEvent[];
  aircraft: Aircraft[];
  vessels: Vessel[];
  tleData: TLERecord[];
  jammingZones: JammingZone[];
  jammingStatus: JammingStatus;
  aircraftTracks: TrackHistory;
  vesselTracks: TrackHistory;
  /** The aircraft feed's state. Legend counts print "—" unless it can vouch for a number. */
  aircraftFeedState: FeedState;
  /** The vessel feed's state; a missing AIS key is not an empty sea. */
  vesselsFeedState: FeedState;
  tleFeedState: FeedState;
  /** The vessel feed's per-region currency, drawn as coverage (C83, lib/coverage.ts). */
  regions: RegionCurrency[];
  /** When each event arrived over the live stream; only these ping (C50). */
  arrivals: ArrivalLog;
  /** No window is scrubbed. When false the live layers are dimmed and counted as NOW (C82, lib/replay.ts). */
  isLive: boolean;
}

/** A legend count: the number drawn, or "—" plus the state when the feed cannot vouch for it. */
function legendCount(n: number, state: FeedState): string {
  return state === "live" || state === "stale" ? String(n) : `— ${FEED_WORD[state].toLowerCase()}`;
}

/**
 * Event mark. Geometry carries the geocoder's precision, size carries severity.
 *
 * A facility fix and a country centroid used to draw the same 8px dot, which
 * read every coordinate as a street address. Now only the tiers that really are
 * points keep a crisp edge; admin1 / region_named / country_centroid draw as a
 * blurred disc whose diameter grows with how vague the claim is. The blur is a
 * static CSS filter, not motion, so prefers-reduced-motion has nothing to undo.
 *
 * A null geo_precision means the tier was never measured, so the mark falls back
 * to the neutral dot and claims nothing about extent. A null severity takes the
 * smallest core rather than the size a severity of 5 would have drawn.
 */
function PingMarker({
  evt,
  isNew,
  onClick,
}: {
  evt: ConflictEvent;
  isNew: boolean;
  onClick: () => void;
}) {
  const color = eventVisual(evt.event_type).color;
  const sev = evt.severity;
  const isHighSeverity = sev != null && sev >= 8;
  // Core size: 6-10px based on severity; the floor when it was never measured.
  const dotSize = sev == null ? 6 : 6 + sev * 0.4;

  const spec = geoPrecision(evt.geo_precision);
  const diffuse = spec != null && spec.blur > 0;
  const discSize = dotSize + (spec?.spread ?? 0);
  // The box has to hold the widest thing drawn in it, or the disc gets clipped.
  const box = Math.max(24, discSize + 12);

  return (
    <div
      style={{
        position: "relative",
        width: box,
        height: box,
        cursor: "pointer",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
      }}
      onClick={onClick}
      title={
        spec
          ? `${evt.summary} — ${spec.label}`
          : `${evt.summary} — precision not recorded`
      }
    >
      {/* Radar ping on new events */}
      {isNew && (
        <div
          style={{
            position: "absolute",
            width: 12,
            height: 12,
            borderRadius: "50%",
            border: `1.5px solid ${isHighSeverity ? "#f85149" : color}`,
            animation: `${isHighSeverity ? "radarPingRed" : "radarPing"} 1.5s ease-out forwards`,
            pointerEvents: "none",
          }}
        />
      )}

      {/* Diffuse disc: for a coarse tier the mark IS the uncertainty, so it has
          no hard edge and no crisp core to mistake for a position. */}
      {spec != null && spec.blur > 0 && (
        <div
          style={{
            position: "absolute",
            width: discSize,
            height: discSize,
            borderRadius: "50%",
            background: `radial-gradient(circle, ${color}66 0%, ${color}22 55%, transparent 75%)`,
            filter: `blur(${spec.blur}px)`,
            pointerEvents: "none",
          }}
        />
      )}

      {/* A named region keeps a dashed edge, so it stays distinct from the
          equally diffuse admin1 disc. */}
      {spec?.ring === "dashed" && (
        <div
          style={{
            position: "absolute",
            width: discSize,
            height: discSize,
            borderRadius: "50%",
            border: `1px dashed ${color}`,
            opacity: 0.45,
            pointerEvents: "none",
          }}
        />
      )}

      {/* Soft ring: city tier always, and the legacy severity ring on rows whose
          precision was never measured. */}
      {(spec?.ring === "soft" || (spec == null && sev != null && sev >= 5)) && (
        <div
          style={{
            position: "absolute",
            width: dotSize + 6,
            height: dotSize + 6,
            borderRadius: "50%",
            border: `1px solid ${color}`,
            opacity: sev == null ? 0.3 : 0.3 + (sev / 10) * 0.4,
          }}
        />
      )}

      {/* Core diamond dot - only where the evidence really is a point.
          A row with no geo_precision at all is NOT that case: nothing
          recorded how precise its coordinate is, and a filled hard-edged dot
          claims a point regardless of what the data says. Those draw hollow -
          the position is shown, the extent is left unstated - which is
          distinct from every measured tier and from the dashed region rim. */}
      {!diffuse && (
        <div
          style={{
            width: dotSize,
            height: dotSize,
            borderRadius: isHighSeverity ? "2px" : "50%",
            transform: isHighSeverity ? "rotate(45deg)" : undefined,
            background: spec == null ? "transparent" : color,
            border: spec == null ? `1px solid ${color}` : undefined,
            boxShadow: spec == null ? undefined : `0 0 ${3 + (sev ?? 0)}px ${color}88`,
          }}
        />
      )}
    </div>
  );
}

export function MapPanel({ events, aircraft, vessels, tleData, jammingZones, jammingStatus, aircraftTracks, vesselTracks, aircraftFeedState, vesselsFeedState, tleFeedState, arrivals, isLive, regions }: MapPanelProps) {
  const [selected, setSelected] = useState<ConflictEvent | null>(null);
  const [selectedAircraft, setSelectedAircraft] = useState<Aircraft | null>(null);
  const [selectedVessel, setSelectedVessel] = useState<Vessel | null>(null);
  const [newEventIds, setNewEventIds] = useState<Set<number>>(new Set());
  // 2D is Mapbox-only: with no token mapbox-gl throws and react-map-gl swallows
  // it, leaving a black panel. The globe needs no key, so keyless installs open
  // on it instead.
  const [viewMode, setViewMode] = useState<"2d" | "globe" | "terrain">(
    MAPBOX_TOKEN ? "2d" : "globe",
  );

  // What does not follow the timeline: aircraft, vessels, satellites, GPS
  // interference and their trails. In replay they are dimmed and counted as
  // NOW, never hidden - an empty sky at T would be a claim (C82).
  const look = liveLook(isLive);
  const nowSuffix = isLive ? "" : ` · ${NOW}`;

  // Coverage (C83): where AIS can vouch for a count (tint) and where it cannot
  // (the lane's hatch), each box minus the boxes before it, like the backend.
  const coverage = useMemo(() => coverageAreas(regions), [regions]);
  const coverageGeoJSON = useMemo(() => coverageFeatureCollection(coverage), [coverage]);
  const coverageDrawn = coverage.some((a) => a.fill !== "off");
  // Mapbox needs the hatch registered as an image before a layer can use it.
  const [hatchReady, setHatchReady] = useState(false);

  // One definition of "located", now held in lib/located.ts so the globe reads
  // the same one rather than its own. The feed states it as its negation
  // (LiveFeed.geoMissing).
  const geoEvents = useMemo(() => events.filter(isLocated), [events]);

  // Events in the current window that have no resolved coordinates. They are
  // deliberately absent from the map - there is no placeholder position - but
  // they must not vanish without trace, so the legend counts them.
  const unlocatedCount = events.length - geoEvents.length;

  const selectedPrecision = selected ? geoPrecision(selected.geo_precision) : null;

  const airborneAircraft = useMemo(
    () => aircraft.filter((a) => !a.on_ground),
    [aircraft],
  );

  // Ping the events that ARRIVED over the stream in the last 2 s (C50) - never
  // ones a scrub brought into the window, and never the REST backfill, which
  // pinged the whole map on first load when this diffed ids against an empty set.
  useEffect(() => {
    const fresh = freshIds(arrivals, geoEvents.map((e) => e.id), Date.now(), 2000);
    setNewEventIds(fresh);
    if (fresh.size === 0) return;
    const timer = setTimeout(() => setNewEventIds(new Set()), 2000);
    return () => clearTimeout(timer);
  }, [arrivals, geoEvents]);

  const handleMarkerClick = useCallback((e: ConflictEvent) => {
    setSelected(e);
    setSelectedAircraft(null);
    setSelectedVessel(null);
  }, []);

  const handleAircraftClick = useCallback((ac: Aircraft) => {
    setSelectedAircraft(ac);
    setSelected(null);
    setSelectedVessel(null);
  }, []);

  const handleVesselClick = useCallback((v: Vessel) => {
    setSelectedVessel(v);
    setSelected(null);
    setSelectedAircraft(null);
  }, []);

  // Build GeoJSON for aircraft trail lines
  const aircraftTrailGeoJSON = useMemo(() => ({
    type: "FeatureCollection" as const,
    features: Object.entries(aircraftTracks).map(([id, points]) => ({
      type: "Feature" as const,
      properties: { id },
      geometry: {
        type: "LineString" as const,
        coordinates: points.map(([lon, lat]) => [lon, lat]),
      },
    })).filter(f => f.geometry.coordinates.length >= 2),
  }), [aircraftTracks]);

  // Build GeoJSON for vessel trail lines
  const vesselTrailGeoJSON = useMemo(() => ({
    type: "FeatureCollection" as const,
    features: Object.entries(vesselTracks).map(([id, points]) => ({
      type: "Feature" as const,
      properties: { id },
      geometry: {
        type: "LineString" as const,
        coordinates: points.map(([lon, lat]) => [lon, lat]),
      },
    })).filter(f => f.geometry.coordinates.length >= 2),
  }), [vesselTracks]);

  // Highlight track for selected aircraft/vessel
  const selectedTrailGeoJSON = useMemo(() => {
    let points: [number, number, number][] | undefined;
    if (selectedAircraft) {
      points = aircraftTracks[selectedAircraft.icao24];
    } else if (selectedVessel) {
      points = vesselTracks[selectedVessel.mmsi];
    }
    if (!points || points.length < 2) return null;
    return {
      type: "FeatureCollection" as const,
      features: [{
        type: "Feature" as const,
        properties: {},
        geometry: {
          type: "LineString" as const,
          coordinates: points.map(([lon, lat]) => [lon, lat]),
        },
      }],
    };
  }, [selectedAircraft, selectedVessel, aircraftTracks, vesselTracks]);

  return (
    <div className="panel" style={{ gridArea: "map", position: "relative" }}>
      {/* View mode toggle */}
      <div
        style={{
          position: "absolute",
          top: 12,
          right: 12,
          zIndex: 10,
          display: "flex",
          gap: 1,
          fontFamily: "var(--font-mono)",
          fontSize: 10,
          letterSpacing: 1,
        }}
      >
        {!isLive && (
          <span
            title={NOW_TITLE}
            style={{
              marginRight: 8,
              padding: "4px 8px",
              borderRadius: 3,
              background: "rgba(10,14,20,0.9)",
              border: "1px solid var(--border)",
              color: "var(--text-secondary)",
              fontWeight: 600,
              whiteSpace: "nowrap",
            }}
          >
            {NOW_CHIP}
          </span>
        )}
        {(["2d", "globe", "terrain"] as const).map((mode, idx, arr) => (
          <button
            key={mode}
            onClick={() => {
              if (!VIEW_NEEDS[mode]) setViewMode(mode);
            }}
            aria-disabled={VIEW_NEEDS[mode] != null}
            title={VIEW_NEEDS[mode] ?? undefined}
            style={{
              padding: "4px 10px",
              background:
                viewMode === mode
                  ? "rgba(88,166,255,0.15)"
                  : "rgba(10,14,20,0.8)",
              border: `1px solid ${viewMode === mode ? "var(--accent-blue)" : "var(--border)"}`,
              color:
                viewMode === mode
                  ? "var(--accent-blue)"
                  : VIEW_NEEDS[mode]
                    ? "var(--text-muted)"
                    : "var(--text-secondary)",
              cursor: VIEW_NEEDS[mode] ? "not-allowed" : "pointer",
              fontFamily: "inherit",
              fontSize: "inherit",
              letterSpacing: "inherit",
              fontWeight: 600,
              borderRadius:
                idx === 0
                  ? "3px 0 0 3px"
                  : idx === arr.length - 1
                    ? "0 3px 3px 0"
                    : "0",
            }}
          >
            {mode.toUpperCase()}
          </button>
        ))}
      </div>

      {viewMode === "2d" ? (
        <>
          <Map
            initialViewState={{
              longitude: 47,
              latitude: 32,
              zoom: 4.5,
            }}
            style={{ width: "100%", height: "100%" }}
            mapStyle="mapbox://styles/mapbox/dark-v11"
            mapboxAccessToken={MAPBOX_TOKEN}
            onLoad={(e) => {
              const ctx = hatchCanvas().getContext("2d")!;
              e.target.addImage("hatch-unknown", ctx.getImageData(0, 0, HATCH_SIZE, HATCH_SIZE), { pixelRatio: 1 });
              setHatchReady(true);
            }}
          >
            {/* Coverage (C83), under everything else: tint where a region can
                vouch for a count, hatch where it cannot. Each region is its box
                cut to the sea (C88), or the box itself when the sea file no
                longer matches the registry, and then the label says so. */}
            <Source id="coverage" type="geojson" data={coverageGeoJSON}>
              <Layer
                id="coverage-observed"
                type="fill"
                filter={["all", ["==", "$type", "Polygon"], ["==", "fill", "observed"]]}
                paint={{ "fill-color": "#40e0d0", "fill-opacity": 0.07 * look.opacity }}
              />
              {hatchReady && (
                <Layer
                  id="coverage-hole"
                  type="fill"
                  filter={["all", ["==", "$type", "Polygon"], ["==", "fill", "hole"]]}
                  paint={{ "fill-pattern": "hatch-unknown", "fill-opacity": look.opacity }}
                />
              )}
              <Layer
                id="coverage-labels"
                type="symbol"
                filter={["==", "$type", "Point"]}
                layout={{
                  "text-field": ["get", "label"],
                  "text-size": 9,
                  "text-font": ["DIN Pro Medium", "Arial Unicode MS Regular"],
                  "text-letter-spacing": 0.08,
                  "text-anchor": "top-left",
                  "text-offset": [0.6, 0.6],
                  "text-justify": "left",
                  "text-max-width": 28,
                  "text-line-height": 1.3,
                }}
                paint={{
                  "text-color": ["match", ["get", "fill"], "observed", "#40e0d0", "#8ea3bb"],
                  "text-opacity": 0.85 * look.opacity,
                  "text-halo-color": "#0a0e14",
                  "text-halo-width": 1,
                }}
              />
            </Source>

            {/* Trail lines: a short live tail, not history, so empty in replay (C82) */}
            <Source id="aircraft-trails" type="geojson" data={look.tracks ? aircraftTrailGeoJSON : NO_TRAILS}>
              <Layer
                id="aircraft-trail-lines"
                type="line"
                paint={{
                  "line-color": "#58d0ff",
                  "line-width": 1,
                  "line-opacity": 0.25,
                }}
              />
            </Source>

            {/* Vessel trail lines */}
            <Source id="vessel-trails" type="geojson" data={look.tracks ? vesselTrailGeoJSON : NO_TRAILS}>
              <Layer
                id="vessel-trail-lines"
                type="line"
                paint={{
                  "line-color": "#40e0d0",
                  "line-width": 1,
                  "line-opacity": 0.25,
                }}
              />
            </Source>

            {/* Highlighted trail for selected entity */}
            {look.tracks && selectedTrailGeoJSON && (
              <Source id="selected-trail" type="geojson" data={selectedTrailGeoJSON}>
                <Layer
                  id="selected-trail-line"
                  type="line"
                  paint={{
                    "line-color": selectedAircraft ? "#58d0ff" : "#40e0d0",
                    "line-width": 2.5,
                    "line-opacity": 0.8,
                  }}
                />
              </Source>
            )}

            {/* Conflict event markers */}
            {geoEvents.map((evt) => (
              <Marker
                key={evt.id}
                longitude={evt.lon!}
                latitude={evt.lat!}
                anchor="center"
                onClick={(e) => {
                  e.originalEvent.stopPropagation();
                  handleMarkerClick(evt);
                }}
              >
                <PingMarker
                  evt={evt}
                  isNew={newEventIds.has(evt.id)}
                  onClick={() => handleMarkerClick(evt)}
                />
              </Marker>
            ))}

            {/* Jamming zones */}
            {jammingZones.map((z, i) => (
              <Marker
                key={`jam-${i}`}
                longitude={z.lon}
                latitude={z.lat}
                anchor="center"
              >
                <div
                  title={`GPS INTERFERENCE | ${z.degraded}/${z.total} aircraft degraded (${Math.round(z.ratio * 100)}%)`}
                  style={{
                    width: Math.max(30, z.radius_km / 2),
                    height: Math.max(30, z.radius_km / 2),
                    borderRadius: "4px",
                    background: `rgba(255, 32, 32, ${0.15 + z.intensity * 0.2})`,
                    border: "1px solid rgba(255, 64, 64, 0.5)",
                    transform: "rotate(45deg)",
                    pointerEvents: "none",
                    opacity: look.opacity,
                  }}
                />
              </Marker>
            ))}

            {/* Aircraft markers with plane icons */}
            {airborneAircraft.map((ac, i) => (
              <Marker
                key={`ac-${ac.icao24 || i}`}
                longitude={ac.lon}
                latitude={ac.lat}
                anchor="center"
              >
                <div
                  style={{ display: "flex", alignItems: "center", gap: 3, cursor: "pointer", opacity: look.opacity }}
                  onClick={(e) => { e.stopPropagation(); handleAircraftClick(ac); }}
                  title={`${ac.callsign || ac.icao24} | ${ac.origin_country}${ac.altitude ? ` | ${Math.round(ac.altitude)}m` : ""}`}
                >
                  {/* Plane icon rotated by heading */}
                  <svg
                    width="16"
                    height="16"
                    viewBox="0 0 24 24"
                    fill={selectedAircraft?.icao24 === ac.icao24 ? "#fff" : "#58d0ff"}
                    style={{
                      transform: `rotate(${(ac.heading || 0)}deg)`,
                      filter: "drop-shadow(0 0 3px rgba(88,208,255,0.6))",
                      opacity: 0.9,
                    }}
                  >
                    <path d="M12 2 L14 8 L21 10 L14 11 L14 18 L17 20 L17 21 L12 19 L7 21 L7 20 L10 18 L10 11 L3 10 L10 8 Z" />
                  </svg>
                  {/* Callsign label */}
                  {ac.callsign && (
                    <span
                      style={{
                        fontSize: 8,
                        color: "#58d0ff",
                        fontFamily: "var(--font-mono)",
                        fontWeight: 600,
                        letterSpacing: 0.5,
                        textShadow: "0 0 4px rgba(0,0,0,0.8)",
                        whiteSpace: "nowrap",
                      }}
                    >
                      {ac.callsign}
                    </span>
                  )}
                </div>
              </Marker>
            ))}

            {/* Vessel markers with ship icons */}
            {vessels.map((v) => (
              <Marker
                key={`v-${v.mmsi}`}
                longitude={v.lon}
                latitude={v.lat}
                anchor="center"
              >
                <div
                  style={{ display: "flex", alignItems: "center", gap: 3, cursor: "pointer", opacity: look.opacity }}
                  onClick={(e) => { e.stopPropagation(); handleVesselClick(v); }}
                  title={`${v.name || v.mmsi} | ${v.ship_type_name || "Vessel"}${v.speed ? ` | ${v.speed}kn` : ""}${v.destination ? ` → ${v.destination}` : ""}`}
                >
                  {/* Ship icon rotated by heading */}
                  <svg
                    width="14"
                    height="14"
                    viewBox="0 0 24 24"
                    fill={selectedVessel?.mmsi === v.mmsi ? "#fff" : "#40e0d0"}
                    style={{
                      transform: `rotate(${(v.heading >= 0 && v.heading < 360) ? v.heading : (v.course || 0)}deg)`,
                      filter: "drop-shadow(0 0 3px rgba(64,224,208,0.5))",
                      opacity: 0.9,
                    }}
                  >
                    <path d="M12 2 L15 9 L15 16 L19 20 L12 22 L5 20 L9 16 L9 9 Z" />
                  </svg>
                  {/* Name label */}
                  {v.name && (
                    <span
                      style={{
                        fontSize: 7,
                        color: "#40e0d0",
                        fontFamily: "var(--font-mono)",
                        fontWeight: 600,
                        letterSpacing: 0.5,
                        textShadow: "0 0 4px rgba(0,0,0,0.8)",
                        whiteSpace: "nowrap",
                      }}
                    >
                      {v.name}
                    </span>
                  )}
                </div>
              </Marker>
            ))}

            {/* Event info popup */}
            {selected && selected.lat && selected.lon && (
              <Popup
                longitude={selected.lon}
                latitude={selected.lat}
                anchor="bottom"
                onClose={() => setSelected(null)}
                closeButton
                closeOnClick={false}
                style={{ maxWidth: 280 }}
              >
                <div
                  style={{
                    color: "#c8d6e5",
                    fontSize: 12,
                    lineHeight: 1.5,
                    fontFamily: "var(--font-sans)",
                  }}
                >
                  <div style={{ fontWeight: 600, marginBottom: 4 }}>
                    {selected.summary}
                  </div>
                  <div
                    style={{
                      fontSize: 10,
                      color: "#5a6a7e",
                      fontFamily: "var(--font-mono)",
                    }}
                  >
                    {selected.event_type.toUpperCase()} |{" "}
                    {selected.severity == null
                      ? "SEV —"
                      : `SEV ${selected.severity}/10`}
                  </div>
                  {selectedPrecision && (
                    <div
                      style={{
                        fontSize: 10,
                        color: "#5a6a7e",
                        marginTop: 2,
                        fontFamily: "var(--font-mono)",
                      }}
                      title="How precise this coordinate is, from the geocoder."
                    >
                      {selectedPrecision.label.toUpperCase()}
                      {formatUncertainty(selected.geo_uncertainty_m) &&
                        ` ${formatUncertainty(selected.geo_uncertainty_m)}`}
                    </div>
                  )}
                  <div
                    style={{
                      fontSize: 10,
                      color: "#5a6a7e",
                      marginTop: 2,
                      fontFamily: "var(--font-mono)",
                    }}
                  >
                    {selected.channel_name} &middot;{" "}
                    {new Date(selected.timestamp).toLocaleString()}
                  </div>
                </div>
              </Popup>
            )}

            {/* Aircraft info popup */}
            {selectedAircraft && (
              <Popup
                longitude={selectedAircraft.lon}
                latitude={selectedAircraft.lat}
                anchor="bottom"
                onClose={() => setSelectedAircraft(null)}
                closeButton
                closeOnClick={false}
                style={{ maxWidth: 280 }}
              >
                <div style={{ color: "#c8d6e5", fontSize: 11, fontFamily: "var(--font-mono)", lineHeight: 1.6 }}>
                  <div style={{ fontWeight: 700, fontSize: 13, color: "#58d0ff", marginBottom: 4 }}>
                    {selectedAircraft.callsign || selectedAircraft.icao24}
                  </div>
                  <div>ICAO24: {selectedAircraft.icao24}</div>
                  {selectedAircraft.callsign && <div>CALLSIGN: {selectedAircraft.callsign}</div>}
                  {selectedAircraft.origin_country && <div>ORIGIN: {selectedAircraft.origin_country}</div>}
                  {selectedAircraft.altitude != null && <div>ALT: {Math.round(selectedAircraft.altitude).toLocaleString()} ft</div>}
                  {selectedAircraft.velocity != null && <div>SPD: {Math.round(selectedAircraft.velocity)} kts</div>}
                  {selectedAircraft.heading != null && <div>HDG: {Math.round(selectedAircraft.heading)}&deg;</div>}
                  <div style={{ fontSize: 9, color: "#5a6a7e", marginTop: 4 }}>
                    {selectedAircraft.lat.toFixed(4)}, {selectedAircraft.lon.toFixed(4)}
                    {selectedAircraft.position_source === 2 && " | MLAT"}
                  </div>
                  {aircraftTracks[selectedAircraft.icao24] && (
                    <div style={{ fontSize: 9, color: "#5a6a7e" }}>
                      TRACK: {aircraftTracks[selectedAircraft.icao24].length} points
                    </div>
                  )}
                </div>
              </Popup>
            )}

            {/* Vessel info popup */}
            {selectedVessel && (
              <Popup
                longitude={selectedVessel.lon}
                latitude={selectedVessel.lat}
                anchor="bottom"
                onClose={() => setSelectedVessel(null)}
                closeButton
                closeOnClick={false}
                style={{ maxWidth: 280 }}
              >
                <div style={{ color: "#c8d6e5", fontSize: 11, fontFamily: "var(--font-mono)", lineHeight: 1.6 }}>
                  <div style={{ fontWeight: 700, fontSize: 13, color: "#40e0d0", marginBottom: 4 }}>
                    {selectedVessel.name || selectedVessel.mmsi}
                  </div>
                  <div>MMSI: {selectedVessel.mmsi}</div>
                  {selectedVessel.ship_type_name && <div>TYPE: {selectedVessel.ship_type_name}</div>}
                  {selectedVessel.speed > 0 && <div>SPD: {selectedVessel.speed} kts</div>}
                  {selectedVessel.heading >= 0 && selectedVessel.heading < 360 && <div>HDG: {selectedVessel.heading}&deg;</div>}
                  {selectedVessel.course > 0 && <div>COG: {selectedVessel.course.toFixed(1)}&deg;</div>}
                  {selectedVessel.destination && <div>DEST: {selectedVessel.destination}</div>}
                  {selectedVessel.length != null && selectedVessel.length > 0 && <div>LEN: {selectedVessel.length}m</div>}
                  <div style={{ fontSize: 9, color: "#5a6a7e", marginTop: 4 }}>
                    {selectedVessel.lat.toFixed(4)}, {selectedVessel.lon.toFixed(4)}
                  </div>
                  {vesselTracks[selectedVessel.mmsi] && (
                    <div style={{ fontSize: 9, color: "#5a6a7e" }}>
                      TRACK: {vesselTracks[selectedVessel.mmsi].length} points
                    </div>
                  )}
                </div>
              </Popup>
            )}
          </Map>

          {/* Legend */}
          <div
            style={{
              position: "absolute",
              bottom: 16,
              left: 16,
              background: "rgba(10, 14, 20, 0.9)",
              padding: "8px 12px",
              borderRadius: 4,
              fontSize: 10,
              display: "flex",
              flexWrap: "wrap",
              maxWidth: "calc(100% - 32px)",
              gap: 12,
              border: "1px solid var(--border)",
              fontFamily: "var(--font-mono)",
              letterSpacing: 0.5,
            }}
          >
            {EVENT_TYPES.map(({ key, label, color, Icon }) => (
              <div
                key={key}
                style={{ display: "flex", alignItems: "center", gap: 4 }}
              >
                <div
                  style={{
                    width: 8,
                    height: 8,
                    borderRadius: "50%",
                    background: color,
                    boxShadow: `0 0 4px ${color}66`,
                  }}
                />
                <Icon size={11} strokeWidth={2.25} style={{ color }} aria-hidden="true" />
                <span
                  style={{
                    color: "var(--text-secondary)",
                    textTransform: "uppercase",
                  }}
                >
                  {label}
                </span>
              </div>
            ))}
            {coverageDrawn && (
              <div
                style={{ display: "flex", alignItems: "center", gap: 4 }}
                title={coverageNote(coverage)}
              >
                <div style={{ width: 8, height: 8, background: "rgba(64,224,208,0.35)" }} />
                <span style={{ color: "var(--text-secondary)" }}>AIS OBSERVED</span>
                <div
                  style={{
                    width: 8,
                    height: 8,
                    marginLeft: 4,
                    background: "repeating-linear-gradient(45deg, transparent 0 2px, rgba(200,214,229,0.5) 2px 3px)",
                  }}
                />
                <span style={{ color: "var(--text-secondary)" }}>AIS NOT OBSERVED{nowSuffix}</span>
              </div>
            )}
            {unlocatedCount > 0 && (
              <div
                style={{ display: "flex", alignItems: "center", gap: 4 }}
                title="Events in this window that were never placed - no coordinate, or a legacy placeholder coordinate the row itself marks as not geolocated. They are not drawn, and no position is invented for them."
              >
                <div
                  style={{
                    width: 8,
                    height: 8,
                    borderRadius: "50%",
                    background: "transparent",
                    border: "1px dashed var(--text-muted)",
                  }}
                />
                <span style={{ color: "var(--text-muted)" }}>
                  UNLOCATED {unlocatedCount}
                </span>
              </div>
            )}
            {airborneAircraft.length > 0 && (
              <div style={{ display: "flex", alignItems: "center", gap: 4 }}>
                <svg width="10" height="10" viewBox="0 0 24 24" fill="#58d0ff">
                  <path d="M12 2 L14 8 L21 10 L14 11 L14 18 L17 20 L17 21 L12 19 L7 21 L7 20 L10 18 L10 11 L3 10 L10 8 Z" />
                </svg>
                <span style={{ color: "var(--text-secondary)" }}>
                  AIRCRAFT ({legendCount(airborneAircraft.length, aircraftFeedState)}){nowSuffix}
                </span>
              </div>
            )}
            {(vessels.length > 0 || vesselsFeedState !== "live") && (
              <div style={{ display: "flex", alignItems: "center", gap: 4 }}>
                <svg width="10" height="10" viewBox="0 0 24 24" fill="#40e0d0">
                  <path d="M12 2 L15 9 L15 16 L19 20 L12 22 L5 20 L9 16 L9 9 Z" />
                </svg>
                <span style={{ color: "var(--text-secondary)" }}>
                  VESSELS ({legendCount(vessels.length, vesselsFeedState)}){nowSuffix}
                </span>
              </div>
            )}
            {jammingStatus.status === "ok" && (
              <div style={{ display: "flex", alignItems: "center", gap: 4 }}>
                <div
                  style={{
                    width: 8,
                    height: 8,
                    borderRadius: 2,
                    background: "rgba(255,32,32,0.6)",
                    border: "1px solid #ff4040",
                  }}
                />
                <span style={{ color: "var(--text-secondary)" }}>
                  GPS INTERFERENCE ({jammingZones.length}){nowSuffix}
                </span>
              </div>
            )}
            {jammingStatus.status !== "ok" && (
              <div style={{ display: "flex", alignItems: "center", gap: 4 }}>
                <div
                  style={{
                    width: 8,
                    height: 8,
                    borderRadius: 2,
                    background: "transparent",
                    border: "1px dashed #d29922",
                  }}
                />
                <span style={{ color: "#d29922" }}>
                  {jammingStatus.status === "no_integrity_data"
                    ? "GPS: NO INTEGRITY DATA"
                    : jammingStatus.status === "feed_not_live"
                      ? "GPS: AIRCRAFT FEED NOT LIVE"
                      : "GPS: INSUFFICIENT COVERAGE"}{nowSuffix}
                </span>
              </div>
            )}
          </div>
        </>
      ) : viewMode === "globe" ? (
        <>
          <GlobeView events={events} aircraft={aircraft} vessels={vessels} tleData={tleData} jammingZones={jammingZones} regions={regions} isLive={isLive} />

          {/* 3D overlay legend */}
          <div
            style={{
              position: "absolute",
              bottom: 16,
              left: 16,
              background: "rgba(4, 6, 8, 0.85)",
              padding: "8px 12px",
              borderRadius: 4,
              fontSize: 10,
              display: "flex",
              flexDirection: "column",
              maxWidth: "calc(100% - 32px)",
              gap: 6,
              border: "1px solid var(--border)",
              fontFamily: "var(--font-mono)",
              letterSpacing: 0.5,
              zIndex: 10,
            }}
          >
            <div style={{ display: "flex", flexWrap: "wrap", gap: 12 }}>
              {EVENT_TYPES.map(({ key, label, color, Icon }) => (
                <div
                  key={key}
                  style={{ display: "flex", alignItems: "center", gap: 4 }}
                >
                  <div
                    style={{
                      width: 6,
                      height: 6,
                      borderRadius: "50%",
                      background: color,
                    }}
                  />
                  <Icon size={11} strokeWidth={2.25} style={{ color }} aria-hidden="true" />
                  <span style={{ color: "var(--text-secondary)", textTransform: "uppercase" }}>
                    {label}
                  </span>
                </div>
              ))}
            </div>
            <div style={{ display: "flex", flexWrap: "wrap", gap: 12 }}>
              <div style={{ display: "flex", alignItems: "center", gap: 4 }}>
                <div
                  style={{
                    width: 6,
                    height: 6,
                    borderRadius: "50%",
                    background: "#f0c040",
                  }}
                />
                <span style={{ color: "var(--text-secondary)" }}>
                  AIRCRAFT ({legendCount(aircraft.filter((a) => !a.on_ground).length, aircraftFeedState)}){nowSuffix}
                </span>
              </div>
              <div style={{ display: "flex", alignItems: "center", gap: 4 }}>
                <div
                  style={{
                    width: 0,
                    height: 0,
                    borderLeft: "3px solid transparent",
                    borderRight: "3px solid transparent",
                    borderBottom: "6px solid #40e0d0",
                  }}
                />
                <span style={{ color: "var(--text-secondary)" }}>
                  VESSELS ({legendCount(vessels.length, vesselsFeedState)}){nowSuffix}
                </span>
              </div>
              <div style={{ display: "flex", alignItems: "center", gap: 4 }}>
                <div
                  style={{
                    width: 6,
                    height: 6,
                    borderRadius: "50%",
                    background: "#00e5a0",
                  }}
                />
                <span style={{ color: "var(--text-secondary)" }}>
                  SATELLITES ({legendCount(tleData.length, tleFeedState)}){nowSuffix}
                </span>
              </div>
              {coverageDrawn && (
                <div
                  style={{ display: "flex", alignItems: "center", gap: 4 }}
                  title={coverageNote(coverage)}
                >
                  <div style={{ width: 8, height: 8, background: "rgba(64,224,208,0.35)" }} />
                  <span style={{ color: "var(--text-secondary)" }}>AIS OBSERVED</span>
                  <div
                    style={{
                      width: 8,
                      height: 8,
                      marginLeft: 4,
                      background: "repeating-linear-gradient(45deg, transparent 0 2px, rgba(200,214,229,0.5) 2px 3px)",
                    }}
                  />
                  <span style={{ color: "var(--text-secondary)" }}>AIS NOT OBSERVED{nowSuffix}</span>
                </div>
              )}
            </div>
          </div>
        </>
      ) : (
        <CesiumView
          events={events}
          aircraft={aircraft}
          vessels={vessels}
          tleData={tleData}
          jammingZones={jammingZones}
          regions={regions}
          isLive={isLive}
        />
      )}
    </div>
  );
}
