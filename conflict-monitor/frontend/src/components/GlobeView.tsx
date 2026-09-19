import { Suspense, useEffect, useMemo, useRef, useState } from "react";
import { Canvas, useFrame, useLoader } from "@react-three/fiber";
import { Line, OrbitControls, Text } from "@react-three/drei";
import * as THREE from "three";
import {
  degreesLat,
  degreesLong,
  eciToGeodetic,
  gstime,
  propagate,
  twoline2satrec,
} from "satellite.js";
import type { EciVec3, SatRec } from "satellite.js";
import type { ConflictEvent } from "../types/event";
import type { Aircraft, JammingZone, TLERecord, Vessel } from "../hooks/useTracking";
import { eventVisual } from "../lib/tokens";

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const EARTH_RADIUS = 1;

// ---------------------------------------------------------------------------
// Utilities
// ---------------------------------------------------------------------------

function latLonToVec3(
  lat: number,
  lon: number,
  R: number,
): [number, number, number] {
  const latRad = (lat * Math.PI) / 180;
  const lonRad = (lon * Math.PI) / 180;
  return [
    R * Math.cos(latRad) * Math.sin(lonRad),
    R * Math.sin(latRad),
    R * Math.cos(latRad) * Math.cos(lonRad),
  ];
}

function altToRadius(altKm: number): number {
  return EARTH_RADIUS + Math.min(altKm, 2000) / 2000 * 0.4 + 0.03;
}

// ---------------------------------------------------------------------------
// Starfield
// ---------------------------------------------------------------------------

function Starfield() {
  const geo = useMemo(() => {
    const positions = new Float32Array(5000 * 3);
    for (let i = 0; i < 5000; i++) {
      const r = 40 + Math.random() * 80;
      const theta = Math.random() * Math.PI * 2;
      const phi = Math.acos(2 * Math.random() - 1);
      positions[i * 3] = r * Math.sin(phi) * Math.cos(theta);
      positions[i * 3 + 1] = r * Math.sin(phi) * Math.sin(theta);
      positions[i * 3 + 2] = r * Math.cos(phi);
    }
    const g = new THREE.BufferGeometry();
    g.setAttribute("position", new THREE.Float32BufferAttribute(positions, 3));
    return g;
  }, []);

  return (
    <points geometry={geo}>
      <pointsMaterial
        color="#ffffff"
        size={0.12}
        sizeAttenuation
        transparent
        opacity={0.5}
      />
    </points>
  );
}

// ---------------------------------------------------------------------------
// Earth with NASA night-lights texture
// ---------------------------------------------------------------------------

function EarthTextured() {
  const texture = useLoader(THREE.TextureLoader, "/textures/earth-night.jpg");

  return (
    <group rotation={[0, -Math.PI * 0.5, 0]}>
      <mesh>
        <sphereGeometry args={[EARTH_RADIUS, 64, 64]} />
        <meshStandardMaterial
          map={texture}
          emissiveMap={texture}
          emissive="#ffffff"
          emissiveIntensity={1.6}
          roughness={1}
          metalness={0}
        />
      </mesh>
    </group>
  );
}

function EarthFallback() {
  return (
    <mesh>
      <sphereGeometry args={[EARTH_RADIUS, 64, 64]} />
      <meshBasicMaterial color="#0c1520" />
    </mesh>
  );
}

function Atmosphere() {
  return (
    <group>
      <mesh>
        <sphereGeometry args={[EARTH_RADIUS * 1.025, 64, 64]} />
        <meshBasicMaterial
          color="#3080d0"
          transparent
          opacity={0.07}
          side={THREE.BackSide}
        />
      </mesh>
      <mesh>
        <sphereGeometry args={[EARTH_RADIUS * 1.06, 64, 64]} />
        <meshBasicMaterial
          color="#2060b0"
          transparent
          opacity={0.035}
          side={THREE.BackSide}
        />
      </mesh>
    </group>
  );
}

// ---------------------------------------------------------------------------
// Coastlines + Country boundaries from GeoJSON
// ---------------------------------------------------------------------------

function GeoLines({
  url,
  color,
  lineWidth,
  opacity,
}: {
  url: string;
  color: string;
  lineWidth: number;
  opacity: number;
}) {
  const [lines, setLines] = useState<[number, number, number][][]>([]);

  useEffect(() => {
    fetch(url)
      .then((r) => r.json())
      .then((geojson) => {
        const result: [number, number, number][][] = [];
        for (const feature of geojson.features) {
          const geom = feature.geometry;
          const lineStrings: number[][][] =
            geom.type === "MultiLineString"
              ? geom.coordinates
              : geom.type === "LineString"
                ? [geom.coordinates]
                : [];
          for (const coords of lineStrings) {
            if (coords.length < 2) continue;
            const points: [number, number, number][] = coords.map(
              ([lon, lat]: number[]) =>
                latLonToVec3(lat, lon, EARTH_RADIUS * 1.002),
            );
            result.push(points);
          }
        }
        setLines(result);
      })
      .catch(() => {});
  }, [url]);

  return (
    <group>
      {lines.map((pts, i) => (
        <Line
          key={i}
          points={pts}
          color={color}
          lineWidth={lineWidth}
          transparent
          opacity={opacity}
        />
      ))}
    </group>
  );
}

// ---------------------------------------------------------------------------
// Event markers (pulsing for high severity)
// ---------------------------------------------------------------------------

function EventMarker({ evt }: { evt: ConflictEvent }) {
  const meshRef = useRef<THREE.Mesh>(null);
  const pos = useMemo(
    () => latLonToVec3(evt.lat!, evt.lon!, EARTH_RADIUS * 1.005),
    [evt.lat, evt.lon],
  );
  const color = eventVisual(evt.event_type).color;
  // severity is null when it was never measured. JS coerces null to 0, so
  // `evt.severity * 0.0008` silently drew an unmeasured event at the smallest,
  // least alarming size on the scale - a measurement it never made. A size
  // channel cannot express "unmeasured", so these draw at the scale's base
  // size and are dimmed, which reads as "no reading" rather than "lowest".
  const unmeasured = evt.severity == null;
  const size = 0.003 + (evt.severity ?? 0) * 0.0008;

  useFrame(({ clock }) => {
    if (meshRef.current && evt.severity != null && evt.severity >= 7) {
      const s = 1 + Math.sin(clock.elapsedTime * 3) * 0.25;
      meshRef.current.scale.setScalar(s);
    }
  });

  return (
    <mesh ref={meshRef} position={pos}>
      <sphereGeometry args={[size, 10, 10]} />
      <meshBasicMaterial color={color} transparent opacity={unmeasured ? 0.45 : 0.95} />
    </mesh>
  );
}

function EventMarkers({ events }: { events: ConflictEvent[] }) {
  const geoEvents = useMemo(
    () => events.filter((e) => e.lat != null && e.lon != null),
    [events],
  );
  return (
    <group>
      {geoEvents.map((evt) => (
        <EventMarker key={evt.id} evt={evt} />
      ))}
    </group>
  );
}

// ---------------------------------------------------------------------------
// Aircraft layer with callsign labels and smooth interpolation
// ---------------------------------------------------------------------------

interface AircraftDot {
  current: THREE.Vector3;
  target: THREE.Vector3;
}

function AircraftLayer({ aircraft }: { aircraft: Aircraft[] }) {
  const dotsRef = useRef<Map<string, AircraftDot>>(new Map());
  const meshRefs = useRef<Map<string, THREE.Mesh>>(new Map());

  const airborne = useMemo(
    () => aircraft.filter((a) => !a.on_ground && a.lat != null && a.lon != null),
    [aircraft],
  );

  // Update target positions
  useEffect(() => {
    const dots = dotsRef.current;
    const seen = new Set<string>();
    for (const ac of airborne) {
      seen.add(ac.icao24);
      const t = new THREE.Vector3(...latLonToVec3(ac.lat, ac.lon, EARTH_RADIUS * 1.012));
      const existing = dots.get(ac.icao24);
      if (existing) {
        existing.target.copy(t);
      } else {
        dots.set(ac.icao24, { current: t.clone(), target: t.clone() });
      }
    }
    for (const key of dots.keys()) {
      if (!seen.has(key)) dots.delete(key);
    }
  }, [airborne]);

  // Interpolate each frame
  useFrame(() => {
    dotsRef.current.forEach((dot, key) => {
      dot.current.lerp(dot.target, 0.06);
      const mesh = meshRefs.current.get(key);
      if (mesh) mesh.position.copy(dot.current);
    });
  });

  return (
    <group>
      {airborne.map((ac) => (
        <group key={ac.icao24}>
          <mesh
            ref={(el) => {
              if (el) meshRefs.current.set(ac.icao24, el);
            }}
            position={latLonToVec3(ac.lat, ac.lon, EARTH_RADIUS * 1.012)}
          >
            <sphereGeometry args={[0.004, 6, 6]} />
            <meshBasicMaterial color="#58d0ff" transparent opacity={0.9} />
          </mesh>
          {/* Callsign label */}
          {ac.callsign && (
            <Text
              position={latLonToVec3(ac.lat, ac.lon, EARTH_RADIUS * 1.018)}
              fontSize={0.012}
              color="#58d0ff"
              anchorX="left"
              anchorY="middle"
              font={undefined}
            >
              {ac.callsign}
            </Text>
          )}
        </group>
      ))}
    </group>
  );
}

// ---------------------------------------------------------------------------
// Satellite layer with labels and ground-footprint lines
// ---------------------------------------------------------------------------

interface SatPos {
  name: string;
  lat: number;
  lon: number;
  alt: number;
  pos3d: [number, number, number];
  ground3d: [number, number, number];
}

function SatelliteLayer({ tleData }: { tleData: TLERecord[] }) {
  const [sats, setSats] = useState<SatPos[]>([]);

  const satrecs = useMemo(() => {
    const result: { name: string; satrec: SatRec }[] = [];
    for (const tle of tleData) {
      try {
        result.push({ name: tle.name, satrec: twoline2satrec(tle.line1, tle.line2) });
      } catch { /* skip */ }
    }
    return result;
  }, [tleData]);

  // Orbit paths (pre-computed, subset)
  const orbitPaths = useMemo(() => {
    const paths: [number, number, number][][] = [];
    const now = new Date();
    for (const { satrec } of satrecs.slice(0, 30)) {
      try {
        const period = (2 * Math.PI) / satrec.no;
        const pts: [number, number, number][] = [];
        for (let t = 0; t <= period; t += period / 50) {
          const d = new Date(now.getTime() + t * 60000);
          const pv = propagate(satrec, d);
          if (!pv || !pv.position || typeof pv.position === "boolean") continue;
          const geo = eciToGeodetic(pv.position as EciVec3<number>, gstime(d));
          pts.push(latLonToVec3(degreesLat(geo.latitude), degreesLong(geo.longitude), altToRadius(geo.height)));
        }
        if (pts.length > 2) paths.push(pts);
      } catch { /* skip */ }
    }
    return paths;
  }, [satrecs]);

  // Propagate positions every 2s
  useEffect(() => {
    const update = () => {
      const now = new Date();
      const gm = gstime(now);
      const result: SatPos[] = [];
      for (const { name, satrec } of satrecs) {
        try {
          const pv = propagate(satrec, now);
          if (!pv || !pv.position || typeof pv.position === "boolean") continue;
          const geo = eciToGeodetic(pv.position as EciVec3<number>, gm);
          const lat = degreesLat(geo.latitude);
          const lon = degreesLong(geo.longitude);
          result.push({
            name,
            lat,
            lon,
            alt: geo.height,
            pos3d: latLonToVec3(lat, lon, altToRadius(geo.height)),
            ground3d: latLonToVec3(lat, lon, EARTH_RADIUS * 1.001),
          });
        } catch { /* skip */ }
      }
      setSats(result);
    };
    update();
    const interval = setInterval(update, 2000);
    return () => clearInterval(interval);
  }, [satrecs]);

  return (
    <group>
      {/* Orbit traces */}
      {orbitPaths.map((pts, i) =>
        pts.length > 2 ? (
          <Line key={`orb-${i}`} points={pts} color="#00e5a0" lineWidth={0.4} transparent opacity={0.1} />
        ) : null,
      )}

      {/* Satellite dots + labels + ground lines */}
      {sats.map((sat, i) => (
        <group key={i}>
          {/* Dot at orbital altitude */}
          <mesh position={sat.pos3d}>
            <sphereGeometry args={[0.005, 6, 6]} />
            <meshBasicMaterial color="#00e5a0" transparent opacity={0.8} />
          </mesh>

          {/* Name label */}
          <Text
            position={sat.pos3d}
            fontSize={0.014}
            color="#00e5a0"
            anchorX="left"
            anchorY="bottom"
            font={undefined}
          >
            {`  ${sat.name}`}
          </Text>

          {/* Line from satellite to ground footprint */}
          <Line
            points={[sat.pos3d, sat.ground3d]}
            color="#00e5a0"
            lineWidth={0.4}
            transparent
            opacity={0.2}
          />

          {/* Ground footprint dot */}
          <mesh position={sat.ground3d}>
            <sphereGeometry args={[0.003, 6, 6]} />
            <meshBasicMaterial color="#00e5a0" transparent opacity={0.3} />
          </mesh>
        </group>
      ))}
    </group>
  );
}

// ---------------------------------------------------------------------------
// Maritime vessel layer
// ---------------------------------------------------------------------------

function VesselLayer({ vessels }: { vessels: Vessel[] }) {
  const dotsRef = useRef<Map<string, { current: THREE.Vector3; target: THREE.Vector3 }>>(new Map());
  const meshRefs = useRef<Map<string, THREE.Mesh>>(new Map());

  useEffect(() => {
    const dots = dotsRef.current;
    const seen = new Set<string>();
    for (const v of vessels) {
      seen.add(v.mmsi);
      const t = new THREE.Vector3(...latLonToVec3(v.lat, v.lon, EARTH_RADIUS * 1.002));
      const existing = dots.get(v.mmsi);
      if (existing) {
        existing.target.copy(t);
      } else {
        dots.set(v.mmsi, { current: t.clone(), target: t.clone() });
      }
    }
    for (const key of dots.keys()) {
      if (!seen.has(key)) dots.delete(key);
    }
  }, [vessels]);

  useFrame(() => {
    dotsRef.current.forEach((dot, key) => {
      dot.current.lerp(dot.target, 0.06);
      const mesh = meshRefs.current.get(key);
      if (mesh) mesh.position.copy(dot.current);
    });
  });

  return (
    <group>
      {vessels.map((v) => (
        <group key={v.mmsi}>
          <mesh
            ref={(el) => {
              if (el) meshRefs.current.set(v.mmsi, el);
            }}
            position={latLonToVec3(v.lat, v.lon, EARTH_RADIUS * 1.002)}
          >
            <coneGeometry args={[0.004, 0.008, 3]} />
            <meshBasicMaterial color="#40e0d0" transparent opacity={0.85} />
          </mesh>
          {v.name && (
            <Text
              position={latLonToVec3(v.lat, v.lon, EARTH_RADIUS * 1.008)}
              fontSize={0.01}
              color="#40e0d0"
              anchorX="left"
              anchorY="middle"
              font={undefined}
            >
              {v.name}
            </Text>
          )}
        </group>
      ))}
    </group>
  );
}

// ---------------------------------------------------------------------------
// GPS Jamming zones (red hexagonal prisms on the surface)
// ---------------------------------------------------------------------------

function JammingHex({ zone }: { zone: JammingZone }) {
  const groupRef = useRef<THREE.Group>(null);
  const pos = useMemo(
    () => new THREE.Vector3(...latLonToVec3(zone.lat, zone.lon, EARTH_RADIUS * 1.001)),
    [zone.lat, zone.lon],
  );

  // Orient the hexagon so it sits flush on the globe surface
  useEffect(() => {
    if (groupRef.current) {
      const normal = pos.clone().normalize();
      groupRef.current.quaternion.setFromUnitVectors(
        new THREE.Vector3(0, 1, 0),
        normal,
      );
    }
  }, [pos]);

  const angularSize = (zone.radius_km / 6371) * EARTH_RADIUS;
  const height = 0.01 + zone.intensity * 0.03;

  return (
    <group ref={groupRef} position={pos}>
      {/* Extruded hexagon */}
      <mesh>
        <cylinderGeometry args={[angularSize, angularSize * 0.85, height, 6]} />
        <meshBasicMaterial
          color="#ff2020"
          transparent
          opacity={0.2 + zone.intensity * 0.3}
          side={THREE.DoubleSide}
        />
      </mesh>
      {/* Wireframe outline */}
      <mesh>
        <cylinderGeometry args={[angularSize, angularSize * 0.85, height, 6]} />
        <meshBasicMaterial
          color="#ff4040"
          transparent
          opacity={0.4 + zone.intensity * 0.3}
          wireframe
        />
      </mesh>
    </group>
  );
}

function JammingLayer({ zones }: { zones: JammingZone[] }) {
  return (
    <group>
      {zones.map((z, i) => (
        <JammingHex key={`j${i}`} zone={z} />
      ))}
    </group>
  );
}

// ---------------------------------------------------------------------------
// Scene composition
// ---------------------------------------------------------------------------

interface SceneProps {
  events: ConflictEvent[];
  aircraft: Aircraft[];
  vessels: Vessel[];
  tleData: TLERecord[];
  jammingZones: JammingZone[];
}

function Scene({ events, aircraft, vessels, tleData, jammingZones }: SceneProps) {
  return (
    <>
      <ambientLight intensity={0.3} />
      <directionalLight position={[5, 3, 5]} intensity={0.2} />
      <Starfield />
      <OrbitControls
        makeDefault
        target={[0, 0, 0]}
        autoRotate
        autoRotateSpeed={0.08}
        enablePan={false}
        enableDamping
        dampingFactor={0.12}
        minDistance={1.15}
        maxDistance={8}
        rotateSpeed={0.5}
        zoomSpeed={0.8}
      />

      {/* Earth */}
      <Suspense fallback={<EarthFallback />}>
        <EarthTextured />
      </Suspense>
      <Atmosphere />

      {/* Geographic reference lines */}
      <GeoLines url="/textures/coastlines.json" color="#2a6090" lineWidth={1} opacity={0.5} />
      <GeoLines url="/textures/countries.json" color="#1e4060" lineWidth={0.6} opacity={0.3} />

      {/* Data layers */}
      <EventMarkers events={events} />
      <AircraftLayer aircraft={aircraft} />
      <VesselLayer vessels={vessels} />
      <SatelliteLayer tleData={tleData} />
      <JammingLayer zones={jammingZones} />
    </>
  );
}

// ---------------------------------------------------------------------------
// Exported component
// ---------------------------------------------------------------------------

interface GlobeViewProps {
  events: ConflictEvent[];
  aircraft: Aircraft[];
  vessels: Vessel[];
  tleData: TLERecord[];
  jammingZones: JammingZone[];
}

export function GlobeView({ events, aircraft, vessels, tleData, jammingZones }: GlobeViewProps) {
  return (
    <div style={{ width: "100%", height: "100%", position: "relative" }}>
      <Canvas
        camera={{
          position: [1.2, 1.6, 2.8],
          fov: 45,
          near: 0.01,
          far: 300,
        }}
        style={{ width: "100%", height: "100%", background: "#030508" }}
        gl={{ antialias: true, alpha: false }}
      >
        <Scene
          events={events}
          aircraft={aircraft}
          vessels={vessels}
          tleData={tleData}
          jammingZones={jammingZones}
        />
      </Canvas>
    </div>
  );
}
