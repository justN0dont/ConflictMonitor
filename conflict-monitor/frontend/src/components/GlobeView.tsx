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
import { isLocated } from "../lib/located";
import { eventVisual, geoPrecision } from "../lib/tokens";

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const EARTH_RADIUS = 1;

/**
 * Precision halo size: radians of arc on the globe per unit of the shared
 * GEO_PRECISION `spread`. The tiers and their order come from that one table in
 * lib/tokens.ts; this constant only says how much globe a unit of it is worth.
 *
 * IT ENCODES THE TIER, NOT THE METRES. It is categorical. Every country_centroid
 * draws the same cap whether its row says +/-121 km or +/-1,759 km. Do not read
 * a distance off it and do not let a later change quietly turn it into a scale.
 *
 * Denominating the cap in the row's real geo_uncertainty_m was the first choice
 * and the held data ruled it out: one country_centroid row carries
 * +/-20,320,227 m, which is further than the 20,015 km that is the greatest
 * distance any two points on Earth can be apart. Drawn true to size that row is
 * a translucent shell over the whole planet, and it would bury the other 95
 * marks. A real-distance mark needs an out-of-range treatment designed first,
 * which is a bigger job than this step carries - so the mark stays categorical
 * and says so here.
 *
 * What it does not do is repeat MapPanel's defect. MapPanel's disc is measured
 * in screen pixels, so its country-level disc covers about 160 km at the opening
 * view and about 220 m zoomed in while the row's uncertainty never moves. A cap
 * in radians is fixed to the sphere: it covers the same ground at every zoom.
 *
 * Chosen so the coarsest tier (spread 46) draws about 0.1 rad, roughly 640 km of
 * arc - inside the country_centroid range rather than flattering it.
 */
const PRECISION_ARC_PER_SPREAD = 0.0022;

// ---------------------------------------------------------------------------
// Utilities
// ---------------------------------------------------------------------------

/**
 * prefers-reduced-motion, read from JS.
 *
 * C49: this scene's motion is frame loops - autoRotate, the severity pulse, the
 * position lerps - and a CSS media query cannot reach a frame loop, so the rule
 * the rest of the app obeys in CSS never applied here at all. matchMedia reads
 * the same query from where the loops live. It is subscribed to rather than
 * sampled once at mount, because the setting can change while the page is open
 * and an answer that is only right at mount is the same kind of stale claim
 * this view is being cleaned of.
 */
function usePrefersReducedMotion(): boolean {
  const query = "(prefers-reduced-motion: reduce)";
  const [reduced, setReduced] = useState(() => window.matchMedia(query).matches);

  useEffect(() => {
    const mq = window.matchMedia(query);
    const onChange = () => setReduced(mq.matches);
    setReduced(mq.matches);
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, []);

  return reduced;
}

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

function EventMarker({
  evt,
  reducedMotion,
}: {
  evt: ConflictEvent;
  reducedMotion: boolean;
}) {
  const meshRef = useRef<THREE.Mesh>(null);
  const pos = useMemo(
    () => latLonToVec3(evt.lat!, evt.lon!, EARTH_RADIUS * 1.005),
    [evt.lat, evt.lon],
  );
  const color = eventVisual(evt.event_type).color;

  // How big the coordinate's claim is, from the same GEO_PRECISION table the 2D
  // map reads. A facility fix and a country centroid used to draw the identical
  // dot here: severity was the only channel this view had, so the globe said
  // "here" about a point it only knew to a country. Every tier with a non-zero
  // `spread` now carries a cap of globe under it; only facility (spread 0)
  // draws none. That is keyed off `spread` rather than the table's `coarse`
  // flag, so `city` gets a small cap too even though the table calls it not
  // coarse - said plainly here because the two are easy to confuse.
  //
  // A null tier means the precision was NEVER MEASURED, and that is not the
  // same claim as facility. Drawing no cap is right - there is no extent to
  // state - but drawing the same solid point as a +/-500 m facility fix would
  // say the globe knows the position exactly, which is the state-2-as-state-1
  // error this whole change exists to remove, reintroduced in a new channel.
  //
  // MapPanel draws these HOLLOW (MapPanel.tsx:136-154: transparent background,
  // 1px border, no glow - "the position is shown, the extent is left
  // unstated"). The globe says the same thing in its own medium: a wireframe
  // sphere. Position shown, extent unstated, and visibly not a facility.
  const spec = geoPrecision(evt.geo_precision);
  const capArc = (spec?.spread ?? 0) * PRECISION_ARC_PER_SPREAD;
  // SphereGeometry's cap opens around +Y, so turn +Y to face the event.
  const capQuat = useMemo(
    () =>
      new THREE.Quaternion().setFromUnitVectors(
        new THREE.Vector3(0, 1, 0),
        new THREE.Vector3(...pos).normalize(),
      ),
    [pos],
  );
  // severity is null when it was never measured. JS coerces null to 0, so
  // `evt.severity * 0.0008` silently drew an unmeasured event at the smallest,
  // least alarming size on the scale - a measurement it never made. A size
  // channel cannot express "unmeasured", so these draw at the scale's base
  // size and are dimmed, which reads as "no reading" rather than "lowest".
  const unmeasured = evt.severity == null;
  const size = 0.003 + (evt.severity ?? 0) * 0.0008;

  useFrame(({ clock }) => {
    const mesh = meshRef.current;
    if (!mesh) return;
    if (reducedMotion) {
      // Also undoes a pulse caught mid-stride when the setting changes.
      mesh.scale.setScalar(1);
      return;
    }
    if (evt.severity != null && evt.severity >= 7) {
      const s = 1 + Math.sin(clock.elapsedTime * 3) * 0.25;
      mesh.scale.setScalar(s);
    }
  });

  return (
    <group>
      {capArc > 0 && (
        <mesh quaternion={capQuat}>
          <sphereGeometry
            args={[EARTH_RADIUS * 1.004, 24, 12, 0, Math.PI * 2, 0, capArc]}
          />
          <meshBasicMaterial
            color={color}
            transparent
            opacity={0.13}
            side={THREE.DoubleSide}
            depthWrite={false}
          />
        </mesh>
      )}
      <mesh ref={meshRef} position={pos}>
        <sphereGeometry args={[size, 10, 10]} />
        <meshBasicMaterial
          color={color}
          transparent
          opacity={unmeasured ? 0.45 : 0.95}
          wireframe={spec == null}
        />
      </mesh>
    </group>
  );
}

/** `located` is already filtered by lib/located.ts - this draws what it is given. */
function EventMarkers({
  located,
  reducedMotion,
}: {
  located: ConflictEvent[];
  reducedMotion: boolean;
}) {
  return (
    <group>
      {located.map((evt) => (
        <EventMarker key={evt.id} evt={evt} reducedMotion={reducedMotion} />
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

function AircraftLayer({
  aircraft,
  reducedMotion,
}: {
  aircraft: Aircraft[];
  reducedMotion: boolean;
}) {
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

  // Interpolate each frame. Under reduced motion the factor is 1, which lands on
  // the new position immediately: the aircraft still moves when the data moves,
  // it just does not glide there.
  useFrame(() => {
    dotsRef.current.forEach((dot, key) => {
      dot.current.lerp(dot.target, reducedMotion ? 1 : 0.06);
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

function VesselLayer({
  vessels,
  reducedMotion,
}: {
  vessels: Vessel[];
  reducedMotion: boolean;
}) {
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
      dot.current.lerp(dot.target, reducedMotion ? 1 : 0.06);
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
  /** Already filtered by lib/located.ts. */
  located: ConflictEvent[];
  aircraft: Aircraft[];
  vessels: Vessel[];
  tleData: TLERecord[];
  jammingZones: JammingZone[];
  reducedMotion: boolean;
}

function Scene({
  located,
  aircraft,
  vessels,
  tleData,
  jammingZones,
  reducedMotion,
}: SceneProps) {
  return (
    <>
      <ambientLight intensity={0.3} />
      <directionalLight position={[5, 3, 5]} intensity={0.2} />
      <Starfield />
      <OrbitControls
        makeDefault
        target={[0, 0, 0]}
        autoRotate={!reducedMotion}
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
      <EventMarkers located={located} reducedMotion={reducedMotion} />
      <AircraftLayer aircraft={aircraft} reducedMotion={reducedMotion} />
      <VesselLayer vessels={vessels} reducedMotion={reducedMotion} />
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
  const reducedMotion = usePrefersReducedMotion();

  // The same predicate the 2D map uses, from the same module, so the two views
  // can no longer disagree about which events exist.
  const located = useMemo(() => events.filter(isLocated), [events]);
  const unlocatedCount = events.length - located.length;

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
          located={located}
          aircraft={aircraft}
          vessels={vessels}
          tleData={tleData}
          jammingZones={jammingZones}
          reducedMotion={reducedMotion}
        />
      </Canvas>

      {/*
        What the globe is not drawing.

        Switching to GLOBE used to drop more than half the window - 104 of 200
        events at the time of writing - with nothing on screen to say so, which
        reads as "nothing happened there" when the truth is "these were never
        placed". The 2D legend has carried this count for a while; the globe is
        not a place the disclosure gets to lapse.

        It is not the 2D legend pasted over the scene: no panel, no border, no
        swatch - unboxed mono text over the starfield, opposite the legend so
        neither crowds the other. And it states the disclaimer on screen rather
        than hiding it in a hover title, because there is no cursor affordance
        over a rotating canvas to suggest hovering. pointerEvents is off so it
        never swallows a drag meant for the globe.
      */}
      {unlocatedCount > 0 && (
        <div
          style={{
            position: "absolute",
            bottom: 16,
            right: 16,
            zIndex: 10,
            textAlign: "right",
            fontFamily: "var(--font-mono)",
            fontSize: 10,
            letterSpacing: 0.5,
            lineHeight: 1.6,
            textTransform: "uppercase",
            color: "var(--text-muted)",
            textShadow: "0 0 6px rgba(3, 5, 8, 0.95)",
            pointerEvents: "none",
          }}
        >
          {/* Name the noun. The globe is simultaneously drawing aircraft and
              vessels in the hundreds or thousands, so a bare "104 of 200"
              beside those counts invites the reader to attach it to the wrong
              feed. It counts EVENTS. */}
          <div style={{ color: "var(--text-secondary)" }}>
            {unlocatedCount} of {events.length} events not drawn
          </div>
          <div>never placed — no position invented</div>
        </div>
      )}
    </div>
  );
}
