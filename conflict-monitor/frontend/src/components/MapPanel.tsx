import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Map, { Marker, Popup, Source, Layer } from "react-map-gl";
import type { ConflictEvent } from "../types/event";
import type { Aircraft, JammingZone, TLERecord, TrackHistory, Vessel } from "../hooks/useTracking";
import { GlobeView } from "./GlobeView";
import { CesiumView } from "./CesiumView";

const MAPBOX_TOKEN = import.meta.env.VITE_MAPBOX_TOKEN ?? "";

const EVENT_COLORS: Record<string, string> = {
  military: "#f85149",
  diplomatic: "#58a6ff",
  economic: "#d29922",
  cyber: "#bc8cff",
};

interface MapPanelProps {
  events: ConflictEvent[];
  aircraft: Aircraft[];
  vessels: Vessel[];
  tleData: TLERecord[];
  jammingZones: JammingZone[];
  aircraftTracks: TrackHistory;
  vesselTracks: TrackHistory;
}

/** Small diamond marker with type color and severity ring */
function PingMarker({
  evt,
  isNew,
  onClick,
}: {
  evt: ConflictEvent;
  isNew: boolean;
  onClick: () => void;
}) {
  const color = EVENT_COLORS[evt.event_type] ?? "#888";
  const isHighSeverity = evt.severity >= 8;
  // Core size: 6-10px based on severity
  const dotSize = 6 + evt.severity * 0.4;

  return (
    <div
      style={{
        position: "relative",
        width: 24,
        height: 24,
        cursor: "pointer",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
      }}
      onClick={onClick}
      title={evt.summary}
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

      {/* Outer severity ring (only for severity >= 5) */}
      {evt.severity >= 5 && (
        <div
          style={{
            position: "absolute",
            width: dotSize + 6,
            height: dotSize + 6,
            borderRadius: "50%",
            border: `1px solid ${color}`,
            opacity: 0.3 + (evt.severity / 10) * 0.4,
          }}
        />
      )}

      {/* Core diamond dot */}
      <div
        style={{
          width: dotSize,
          height: dotSize,
          borderRadius: isHighSeverity ? "2px" : "50%",
          transform: isHighSeverity ? "rotate(45deg)" : undefined,
          background: color,
          boxShadow: `0 0 ${3 + evt.severity}px ${color}88`,
        }}
      />
    </div>
  );
}

export function MapPanel({ events, aircraft, vessels, tleData, jammingZones, aircraftTracks, vesselTracks }: MapPanelProps) {
  const [selected, setSelected] = useState<ConflictEvent | null>(null);
  const [selectedAircraft, setSelectedAircraft] = useState<Aircraft | null>(null);
  const [selectedVessel, setSelectedVessel] = useState<Vessel | null>(null);
  const [newEventIds, setNewEventIds] = useState<Set<number>>(new Set());
  const prevIdsRef = useRef<Set<number>>(new Set());
  const [viewMode, setViewMode] = useState<"2d" | "globe" | "terrain">("2d");

  const geoEvents = useMemo(
    () => events.filter((e) => e.lat != null && e.lon != null),
    [events],
  );

  const airborneAircraft = useMemo(
    () => aircraft.filter((a) => !a.on_ground),
    [aircraft],
  );

  // Track which events are new for ping animation
  useEffect(() => {
    const currentIds = new Set(geoEvents.map((e) => e.id));
    const fresh = new Set<number>();
    for (const id of currentIds) {
      if (!prevIdsRef.current.has(id)) fresh.add(id);
    }
    if (fresh.size > 0) {
      setNewEventIds(fresh);
      const timer = setTimeout(() => setNewEventIds(new Set()), 2000);
      prevIdsRef.current = currentIds;
      return () => clearTimeout(timer);
    }
    prevIdsRef.current = currentIds;
  }, [geoEvents]);

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
        {(["2d", "globe", "terrain"] as const).map((mode, idx, arr) => (
          <button
            key={mode}
            onClick={() => setViewMode(mode)}
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
                  : "var(--text-secondary)",
              cursor: "pointer",
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
          >
            {/* Aircraft trail lines */}
            <Source id="aircraft-trails" type="geojson" data={aircraftTrailGeoJSON}>
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
            <Source id="vessel-trails" type="geojson" data={vesselTrailGeoJSON}>
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
            {selectedTrailGeoJSON && (
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
                  title={`GPS JAMMING | ${z.aircraft_count} MLAT aircraft`}
                  style={{
                    width: Math.max(30, z.radius_km / 2),
                    height: Math.max(30, z.radius_km / 2),
                    borderRadius: "4px",
                    background: `rgba(255, 32, 32, ${0.15 + z.intensity * 0.2})`,
                    border: "1px solid rgba(255, 64, 64, 0.5)",
                    transform: "rotate(45deg)",
                    pointerEvents: "none",
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
                style={{ transition: "transform 2s linear" }}
              >
                <div
                  style={{ display: "flex", alignItems: "center", gap: 3, cursor: "pointer" }}
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
                style={{ transition: "transform 2s linear" }}
              >
                <div
                  style={{ display: "flex", alignItems: "center", gap: 3, cursor: "pointer" }}
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
                    {selected.event_type.toUpperCase()} | SEV{" "}
                    {selected.severity}/10
                  </div>
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
              gap: 12,
              border: "1px solid var(--border)",
              fontFamily: "var(--font-mono)",
              letterSpacing: 0.5,
            }}
          >
            {Object.entries(EVENT_COLORS).map(([type, color]) => (
              <div
                key={type}
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
                <span
                  style={{
                    color: "var(--text-secondary)",
                    textTransform: "uppercase",
                  }}
                >
                  {type}
                </span>
              </div>
            ))}
            {airborneAircraft.length > 0 && (
              <div style={{ display: "flex", alignItems: "center", gap: 4 }}>
                <svg width="10" height="10" viewBox="0 0 24 24" fill="#58d0ff">
                  <path d="M12 2 L14 8 L21 10 L14 11 L14 18 L17 20 L17 21 L12 19 L7 21 L7 20 L10 18 L10 11 L3 10 L10 8 Z" />
                </svg>
                <span style={{ color: "var(--text-secondary)" }}>
                  AIRCRAFT ({airborneAircraft.length})
                </span>
              </div>
            )}
            {vessels.length > 0 && (
              <div style={{ display: "flex", alignItems: "center", gap: 4 }}>
                <svg width="10" height="10" viewBox="0 0 24 24" fill="#40e0d0">
                  <path d="M12 2 L15 9 L15 16 L19 20 L12 22 L5 20 L9 16 L9 9 Z" />
                </svg>
                <span style={{ color: "var(--text-secondary)" }}>
                  VESSELS ({vessels.length})
                </span>
              </div>
            )}
            {jammingZones.length > 0 && (
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
                  GPS JAMMING ({jammingZones.length})
                </span>
              </div>
            )}
          </div>
        </>
      ) : viewMode === "globe" ? (
        <>
          <GlobeView events={events} aircraft={aircraft} vessels={vessels} tleData={tleData} jammingZones={jammingZones} />

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
              gap: 6,
              border: "1px solid var(--border)",
              fontFamily: "var(--font-mono)",
              letterSpacing: 0.5,
              zIndex: 10,
            }}
          >
            <div style={{ display: "flex", gap: 12 }}>
              {Object.entries(EVENT_COLORS).map(([type, color]) => (
                <div
                  key={type}
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
                  <span style={{ color: "var(--text-secondary)", textTransform: "uppercase" }}>
                    {type}
                  </span>
                </div>
              ))}
            </div>
            <div style={{ display: "flex", gap: 12 }}>
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
                  AIRCRAFT ({aircraft.filter((a) => !a.on_ground).length})
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
                  VESSELS ({vessels.length})
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
                  SATELLITES ({tleData.length})
                </span>
              </div>
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
        />
      )}
    </div>
  );
}
