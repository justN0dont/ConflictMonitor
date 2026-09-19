import { useEffect, useRef, useState, useCallback } from "react";
import * as Cesium from "cesium";
import "cesium/Build/Cesium/Widgets/widgets.css";
import {
  degreesLat,
  degreesLong,
  eciToGeodetic,
  gstime,
  propagate,
  twoline2satrec,
} from "satellite.js";
import type { EciVec3 } from "satellite.js";
import type { ConflictEvent } from "../types/event";
import type { Aircraft, JammingZone, TLERecord, Vessel } from "../hooks/useTracking";
import { EVENT_TYPES, eventVisual, type EventKey } from "../lib/tokens";

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

/** Token colours as Cesium.Color, converted once - never in a render loop. */
const EVENT_CESIUM_COLORS = Object.fromEntries(
  EVENT_TYPES.map((e) => [e.key, Cesium.Color.fromCssColorString(e.color)]),
) as Record<EventKey, Cesium.Color>;

const AIRCRAFT_COLOR = Cesium.Color.fromCssColorString("#58d0ff");
const VESSEL_COLOR = Cesium.Color.fromCssColorString("#40e0d0");
const SATELLITE_COLOR = Cesium.Color.fromCssColorString("#00e5a0");
const JAMMING_COLOR = Cesium.Color.fromCssColorString("#ff2020");

type FilterMode = "normal" | "crt" | "nvg" | "flir";

// ---------------------------------------------------------------------------
// Shader source strings
// ---------------------------------------------------------------------------

const CRT_SHADER = `
  uniform sampler2D colorTexture;
  in vec2 v_textureCoordinates;
  void main() {
    vec2 uv = v_textureCoordinates;
    // Slight barrel distortion
    vec2 center = uv - 0.5;
    float dist = dot(center, center);
    uv = uv + center * dist * 0.05;
    vec4 color = texture(colorTexture, uv);
    // Scanlines
    float scanline = sin(uv.y * 800.0) * 0.04;
    color.rgb -= scanline;
    // Green tint
    color.rgb *= vec3(0.92, 1.08, 0.92);
    // Vignette
    float vignette = smoothstep(0.55, 0.2, dist);
    color.rgb *= vignette;
    out_FragColor = color;
  }
`;

const NVG_SHADER = `
  uniform sampler2D colorTexture;
  uniform float czm_frameNumber;
  in vec2 v_textureCoordinates;
  void main() {
    vec4 color = texture(colorTexture, v_textureCoordinates);
    float lum = dot(color.rgb, vec3(0.299, 0.587, 0.114));
    // Amplify
    lum = pow(lum, 0.7) * 1.5;
    // Noise grain
    float noise = fract(sin(dot(v_textureCoordinates * czm_frameNumber, vec2(12.9898, 78.233))) * 43758.5453);
    lum += (noise - 0.5) * 0.06;
    // Green monochrome
    vec3 nvg = vec3(lum * 0.2, lum * 1.0, lum * 0.15);
    // Slight bloom on bright areas
    nvg += max(lum - 0.8, 0.0) * vec3(0.1, 0.4, 0.1);
    out_FragColor = vec4(nvg, 1.0);
  }
`;

const FLIR_SHADER = `
  uniform sampler2D colorTexture;
  in vec2 v_textureCoordinates;
  // White-hot thermal palette
  vec3 thermalPalette(float t) {
    if (t < 0.25) return mix(vec3(0.0, 0.0, 0.1), vec3(0.1, 0.0, 0.5), t / 0.25);
    if (t < 0.5) return mix(vec3(0.1, 0.0, 0.5), vec3(0.8, 0.1, 0.1), (t - 0.25) / 0.25);
    if (t < 0.75) return mix(vec3(0.8, 0.1, 0.1), vec3(1.0, 0.8, 0.0), (t - 0.5) / 0.25);
    return mix(vec3(1.0, 0.8, 0.0), vec3(1.0, 1.0, 1.0), (t - 0.75) / 0.25);
  }
  void main() {
    // Simple box blur for slight softness
    vec2 px = vec2(1.0) / vec2(textureSize(colorTexture, 0));
    vec4 color = vec4(0.0);
    for (int x = -1; x <= 1; x++) {
      for (int y = -1; y <= 1; y++) {
        color += texture(colorTexture, v_textureCoordinates + vec2(float(x), float(y)) * px);
      }
    }
    color /= 9.0;
    float lum = dot(color.rgb, vec3(0.299, 0.587, 0.114));
    vec3 thermal = thermalPalette(clamp(lum, 0.0, 1.0));
    out_FragColor = vec4(thermal, 1.0);
  }
`;

// ---------------------------------------------------------------------------
// Canvas billboard generators
// ---------------------------------------------------------------------------

function makeCircleBillboard(
  color: string,
  size: number,
  pulse: boolean = false,
): HTMLCanvasElement {
  const dim = size * 4;
  const canvas = document.createElement("canvas");
  canvas.width = dim;
  canvas.height = dim;
  const ctx = canvas.getContext("2d")!;
  const cx = dim / 2;

  if (pulse) {
    // Outer glow ring
    ctx.beginPath();
    ctx.arc(cx, cx, cx * 0.9, 0, Math.PI * 2);
    ctx.strokeStyle = color;
    ctx.lineWidth = 2;
    ctx.globalAlpha = 0.3;
    ctx.stroke();
    ctx.globalAlpha = 1;
  }

  // Main circle
  ctx.beginPath();
  ctx.arc(cx, cx, cx * 0.5, 0, Math.PI * 2);
  ctx.fillStyle = color;
  ctx.fill();

  // Glow
  ctx.shadowColor = color;
  ctx.shadowBlur = dim * 0.3;
  ctx.beginPath();
  ctx.arc(cx, cx, cx * 0.35, 0, Math.PI * 2);
  ctx.fillStyle = color;
  ctx.fill();

  return canvas;
}

function makeTriangleBillboard(color: string, size: number): HTMLCanvasElement {
  const dim = size * 4;
  const canvas = document.createElement("canvas");
  canvas.width = dim;
  canvas.height = dim;
  const ctx = canvas.getContext("2d")!;
  const cx = dim / 2;
  const r = cx * 0.6;

  ctx.beginPath();
  ctx.moveTo(cx, cx - r);
  ctx.lineTo(cx - r * 0.7, cx + r * 0.6);
  ctx.lineTo(cx + r * 0.7, cx + r * 0.6);
  ctx.closePath();
  ctx.fillStyle = color;
  ctx.shadowColor = color;
  ctx.shadowBlur = dim * 0.2;
  ctx.fill();

  return canvas;
}

// Pre-generate billboards
const billboardCache: Record<string, HTMLCanvasElement> = {};
function getBillboard(key: string, generator: () => HTMLCanvasElement): HTMLCanvasElement {
  if (!billboardCache[key]) billboardCache[key] = generator();
  return billboardCache[key];
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

interface CesiumViewProps {
  events: ConflictEvent[];
  aircraft: Aircraft[];
  vessels: Vessel[];
  tleData: TLERecord[];
  jammingZones: JammingZone[];
}

export function CesiumView({
  events,
  aircraft,
  vessels,
  tleData,
  jammingZones,
}: CesiumViewProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const viewerRef = useRef<Cesium.Viewer | null>(null);
  const [filterMode, setFilterMode] = useState<FilterMode>("normal");
  const postProcessRef = useRef<Cesium.PostProcessStage | null>(null);

  // Data source refs for efficient updates
  const eventSourceRef = useRef<Cesium.CustomDataSource | null>(null);
  const aircraftSourceRef = useRef<Cesium.CustomDataSource | null>(null);
  const vesselSourceRef = useRef<Cesium.CustomDataSource | null>(null);
  const satelliteSourceRef = useRef<Cesium.CustomDataSource | null>(null);
  const jammingSourceRef = useRef<Cesium.CustomDataSource | null>(null);
  const satIntervalRef = useRef<number | null>(null);

  // -----------------------------------------------------------------------
  // Initialize viewer
  // -----------------------------------------------------------------------
  useEffect(() => {
    if (!containerRef.current || viewerRef.current) return;

    // Ion token (optional)
    const ionToken = (import.meta as any).env?.VITE_CESIUM_ION_TOKEN;
    if (ionToken) {
      Cesium.Ion.defaultAccessToken = ionToken;
    }

    const viewer = new Cesium.Viewer(containerRef.current, {
      timeline: false,
      animation: false,
      baseLayerPicker: false,
      geocoder: false,
      homeButton: false,
      navigationHelpButton: false,
      sceneModePicker: false,
      fullscreenButton: false,
      vrButton: false,
      selectionIndicator: true,
      infoBox: true,
      creditContainer: document.createElement("div"), // Hide credits
    });

    viewerRef.current = viewer;

    // Dark space background
    viewer.scene.skyBox = undefined as any;
    viewer.scene.backgroundColor = Cesium.Color.fromCssColorString("#030508");
    if (viewer.scene.sun) viewer.scene.sun.show = false;
    if (viewer.scene.moon) viewer.scene.moon.show = false;
    viewer.scene.globe.enableLighting = false;
    viewer.scene.globe.baseColor = Cesium.Color.fromCssColorString("#0c1520");

    // Try Google 3D Tiles
    const googleKey = (import.meta as any).env?.VITE_GOOGLE_MAPS_KEY;
    if (googleKey) {
      Cesium.Cesium3DTileset.fromUrl(
        `https://tile.googleapis.com/v1/3dtiles/root.json?key=${googleKey}`,
      )
        .then((tileset) => {
          viewer.scene.primitives.add(tileset);
        })
        .catch(() => {
          console.warn("Google 3D Tiles unavailable, using default imagery");
        });
    }

    // Terrain
    if (ionToken) {
      viewer.scene.setTerrain(new Cesium.Terrain(Cesium.createWorldTerrainAsync()));
    }

    // Initial camera: Middle East
    viewer.camera.flyTo({
      destination: Cesium.Cartesian3.fromDegrees(47, 32, 3_000_000),
      duration: 0,
    });

    // Create data sources
    const eventSource = new Cesium.CustomDataSource("events");
    const aircraftSource = new Cesium.CustomDataSource("aircraft");
    const vesselSource = new Cesium.CustomDataSource("vessels");
    const satelliteSource = new Cesium.CustomDataSource("satellites");
    const jammingSource = new Cesium.CustomDataSource("jamming");

    viewer.dataSources.add(eventSource);
    viewer.dataSources.add(aircraftSource);
    viewer.dataSources.add(vesselSource);
    viewer.dataSources.add(satelliteSource);
    viewer.dataSources.add(jammingSource);

    eventSourceRef.current = eventSource;
    aircraftSourceRef.current = aircraftSource;
    vesselSourceRef.current = vesselSource;
    satelliteSourceRef.current = satelliteSource;
    jammingSourceRef.current = jammingSource;

    // Style the info box
    const infoBoxFrame = viewer.infoBox?.frame;
    if (infoBoxFrame) {
      infoBoxFrame.addEventListener("load", () => {
        const style = infoBoxFrame.contentDocument?.createElement("style");
        if (style) {
          style.textContent = `
            body { background: #111822 !important; color: #c8d6e5 !important; font-family: 'JetBrains Mono', monospace !important; font-size: 12px !important; }
            .cesium-infoBox-title { background: #0d1219 !important; color: #58a6ff !important; }
          `;
          infoBoxFrame.contentDocument?.head?.appendChild(style);
        }
      });
    }

    return () => {
      if (satIntervalRef.current) clearInterval(satIntervalRef.current);
      viewer.destroy();
      viewerRef.current = null;
    };
  }, []);

  // -----------------------------------------------------------------------
  // Post-processing filters
  // -----------------------------------------------------------------------
  useEffect(() => {
    const viewer = viewerRef.current;
    if (!viewer) return;

    // Remove existing
    if (postProcessRef.current) {
      viewer.scene.postProcessStages.remove(postProcessRef.current);
      postProcessRef.current = null;
    }

    if (filterMode === "normal") return;

    const shaderMap: Record<string, string> = {
      crt: CRT_SHADER,
      nvg: NVG_SHADER,
      flir: FLIR_SHADER,
    };

    const shader = shaderMap[filterMode];
    if (!shader) return;

    const stage = new Cesium.PostProcessStage({
      fragmentShader: shader,
    });
    viewer.scene.postProcessStages.add(stage);
    postProcessRef.current = stage;
  }, [filterMode]);

  // -----------------------------------------------------------------------
  // Update conflict events
  // -----------------------------------------------------------------------
  useEffect(() => {
    const source = eventSourceRef.current;
    if (!source) return;

    const geoEvents = events.filter((e) => e.lat != null && e.lon != null);
    const existingIds = new Set<string>();

    // Update or add
    for (const evt of geoEvents) {
      const id = `evt-${evt.id}`;
      existingIds.add(id);
      let entity = source.entities.getById(id);
      const color = EVENT_CESIUM_COLORS[eventVisual(evt.event_type).key];
      const isHigh = evt.severity >= 8;
      const size = 12 + evt.severity * 3;

      if (!entity) {
        const billboard = getBillboard(
          `${evt.event_type}-${isHigh ? "high" : "low"}`,
          () =>
            makeCircleBillboard(
              color.toCssColorString(),
              size,
              isHigh,
            ),
        );

        entity = source.entities.add({
          id,
          position: Cesium.Cartesian3.fromDegrees(evt.lon!, evt.lat!, 100),
          billboard: {
            image: billboard,
            width: size,
            height: size,
            verticalOrigin: Cesium.VerticalOrigin.CENTER,
            disableDepthTestDistance: Number.POSITIVE_INFINITY,
          },
          label: {
            text: evt.summary.length > 60 ? evt.summary.slice(0, 57) + "..." : evt.summary,
            font: "11px JetBrains Mono, monospace",
            fillColor: Cesium.Color.fromCssColorString("#c8d6e5"),
            outlineColor: Cesium.Color.BLACK,
            outlineWidth: 2,
            style: Cesium.LabelStyle.FILL_AND_OUTLINE,
            verticalOrigin: Cesium.VerticalOrigin.TOP,
            pixelOffset: new Cesium.Cartesian2(0, 14),
            distanceDisplayCondition: new Cesium.DistanceDisplayCondition(0, 500_000),
            disableDepthTestDistance: Number.POSITIVE_INFINITY,
          },
          description: `
            <div style="font-family: JetBrains Mono, monospace; font-size: 12px; color: #c8d6e5; line-height: 1.6;">
              <div style="font-weight: 600; margin-bottom: 8px; font-size: 13px;">${evt.summary}</div>
              <div style="color: #5a6a7e;">
                <span style="color: ${color.toCssColorString()}; font-weight: 600;">${evt.event_type.toUpperCase()}</span>
                &nbsp;|&nbsp; SEVERITY ${evt.severity}/10
              </div>
              <div style="color: #5a6a7e; margin-top: 4px;">
                ${evt.channel_name} &middot; ${new Date(evt.timestamp).toUTCString()}
              </div>
              <div style="color: #5a6a7e; margin-top: 4px;">
                ${evt.lat!.toFixed(4)}°N, ${evt.lon!.toFixed(4)}°E
              </div>
            </div>
          `,
        });
      }
    }

    // Remove stale
    const toRemove: Cesium.Entity[] = [];
    for (let i = 0; i < source.entities.values.length; i++) {
      const e = source.entities.values[i];
      if (!existingIds.has(e.id)) toRemove.push(e);
    }
    toRemove.forEach((e) => source.entities.remove(e));
  }, [events]);

  // -----------------------------------------------------------------------
  // Update aircraft
  // -----------------------------------------------------------------------
  useEffect(() => {
    const source = aircraftSourceRef.current;
    if (!source) return;

    const airborne = aircraft.filter((a) => !a.on_ground && a.lat != null && a.lon != null);
    const existingIds = new Set<string>();

    for (const ac of airborne) {
      const id = `ac-${ac.icao24}`;
      existingIds.add(id);
      let entity = source.entities.getById(id);
      const alt = ac.altitude || 10000;

      if (entity) {
        // Update position
        (entity.position as any) = new Cesium.ConstantPositionProperty(
          Cesium.Cartesian3.fromDegrees(ac.lon, ac.lat, alt),
        );
      } else {
        entity = source.entities.add({
          id,
          position: Cesium.Cartesian3.fromDegrees(ac.lon, ac.lat, alt),
          point: {
            pixelSize: 5,
            color: AIRCRAFT_COLOR,
            outlineColor: Cesium.Color.WHITE.withAlpha(0.4),
            outlineWidth: 1,
            disableDepthTestDistance: Number.POSITIVE_INFINITY,
          },
          label: {
            text: ac.callsign || ac.icao24,
            font: "10px JetBrains Mono, monospace",
            fillColor: AIRCRAFT_COLOR,
            outlineColor: Cesium.Color.BLACK,
            outlineWidth: 2,
            style: Cesium.LabelStyle.FILL_AND_OUTLINE,
            verticalOrigin: Cesium.VerticalOrigin.BOTTOM,
            pixelOffset: new Cesium.Cartesian2(8, -4),
            distanceDisplayCondition: new Cesium.DistanceDisplayCondition(0, 1_000_000),
            disableDepthTestDistance: Number.POSITIVE_INFINITY,
          },
          description: `
            <div style="font-family: JetBrains Mono, monospace; font-size: 12px; color: #c8d6e5;">
              <div style="font-weight: 600; color: #58d0ff;">${ac.callsign || ac.icao24}</div>
              <div style="color: #5a6a7e; margin-top: 4px;">ICAO: ${ac.icao24}</div>
              ${ac.origin_country ? `<div style="color: #5a6a7e;">Origin: ${ac.origin_country}</div>` : ""}
              <div style="color: #5a6a7e;">Altitude: ${alt ? Math.round(alt).toLocaleString() + "m" : "N/A"}</div>
              <div style="color: #5a6a7e;">Velocity: ${ac.velocity ? Math.round(ac.velocity) + " m/s" : "N/A"}</div>
              <div style="color: #5a6a7e;">Heading: ${ac.heading ? Math.round(ac.heading) + "°" : "N/A"}</div>
            </div>
          `,
        });
      }
    }

    const toRemove: Cesium.Entity[] = [];
    for (let i = 0; i < source.entities.values.length; i++) {
      const e = source.entities.values[i];
      if (!existingIds.has(e.id)) toRemove.push(e);
    }
    toRemove.forEach((e) => source.entities.remove(e));
  }, [aircraft]);

  // -----------------------------------------------------------------------
  // Update vessels
  // -----------------------------------------------------------------------
  useEffect(() => {
    const source = vesselSourceRef.current;
    if (!source) return;

    const active = vessels.filter((v) => v.lat != null && v.lon != null);
    const existingIds = new Set<string>();

    for (const v of active) {
      const id = `ves-${v.mmsi}`;
      existingIds.add(id);
      let entity = source.entities.getById(id);

      if (entity) {
        (entity.position as any) = new Cesium.ConstantPositionProperty(
          Cesium.Cartesian3.fromDegrees(v.lon, v.lat, 0),
        );
      } else {
        const billboard = getBillboard("vessel-tri", () =>
          makeTriangleBillboard("#40e0d0", 10),
        );

        entity = source.entities.add({
          id,
          position: Cesium.Cartesian3.fromDegrees(v.lon, v.lat, 0),
          billboard: {
            image: billboard,
            width: 10,
            height: 10,
            rotation: Cesium.Math.toRadians(-(v.heading || 0)),
            verticalOrigin: Cesium.VerticalOrigin.CENTER,
            disableDepthTestDistance: Number.POSITIVE_INFINITY,
          },
          label: {
            text: v.name || "",
            font: "9px JetBrains Mono, monospace",
            fillColor: VESSEL_COLOR,
            outlineColor: Cesium.Color.BLACK,
            outlineWidth: 2,
            style: Cesium.LabelStyle.FILL_AND_OUTLINE,
            verticalOrigin: Cesium.VerticalOrigin.TOP,
            pixelOffset: new Cesium.Cartesian2(0, 10),
            distanceDisplayCondition: new Cesium.DistanceDisplayCondition(0, 500_000),
            disableDepthTestDistance: Number.POSITIVE_INFINITY,
            show: !!v.name,
          },
          description: `
            <div style="font-family: JetBrains Mono, monospace; font-size: 12px; color: #c8d6e5;">
              <div style="font-weight: 600; color: #40e0d0;">${v.name || v.mmsi}</div>
              <div style="color: #5a6a7e; margin-top: 4px;">MMSI: ${v.mmsi}</div>
              ${v.ship_type_name ? `<div style="color: #5a6a7e;">Type: ${v.ship_type_name}</div>` : ""}
              <div style="color: #5a6a7e;">Speed: ${v.speed ? v.speed + " kn" : "N/A"}</div>
              ${v.destination ? `<div style="color: #5a6a7e;">Dest: ${v.destination}</div>` : ""}
            </div>
          `,
        });
      }
    }

    const toRemove: Cesium.Entity[] = [];
    for (let i = 0; i < source.entities.values.length; i++) {
      const e = source.entities.values[i];
      if (!existingIds.has(e.id)) toRemove.push(e);
    }
    toRemove.forEach((e) => source.entities.remove(e));
  }, [vessels]);

  // -----------------------------------------------------------------------
  // Update GPS jamming zones
  // -----------------------------------------------------------------------
  useEffect(() => {
    const source = jammingSourceRef.current;
    if (!source) return;

    source.entities.removeAll();

    for (let i = 0; i < jammingZones.length; i++) {
      const z = jammingZones[i];
      source.entities.add({
        id: `jam-${i}`,
        position: Cesium.Cartesian3.fromDegrees(z.lon, z.lat, 0),
        ellipse: {
          semiMajorAxis: z.radius_km * 1000,
          semiMinorAxis: z.radius_km * 1000,
          material: JAMMING_COLOR.withAlpha(0.1 + z.intensity * 0.25),
          outline: true,
          outlineColor: JAMMING_COLOR.withAlpha(0.5),
          outlineWidth: 2,
          height: 0,
        },
        label: {
          text: `GPS INTERFERENCE\n${z.degraded}/${z.total} degraded`,
          font: "10px JetBrains Mono, monospace",
          fillColor: JAMMING_COLOR,
          outlineColor: Cesium.Color.BLACK,
          outlineWidth: 2,
          style: Cesium.LabelStyle.FILL_AND_OUTLINE,
          verticalOrigin: Cesium.VerticalOrigin.CENTER,
          distanceDisplayCondition: new Cesium.DistanceDisplayCondition(0, 2_000_000),
          disableDepthTestDistance: Number.POSITIVE_INFINITY,
        },
      });
    }
  }, [jammingZones]);

  // -----------------------------------------------------------------------
  // Satellite layer with TLE propagation
  // -----------------------------------------------------------------------
  useEffect(() => {
    const source = satelliteSourceRef.current;
    if (!source) return;

    // Clear old interval
    if (satIntervalRef.current) {
      clearInterval(satIntervalRef.current);
      satIntervalRef.current = null;
    }

    // Parse TLEs (limit to 50 for perf)
    const satrecs = tleData.slice(0, 50).map((tle) => {
      try {
        return { name: tle.name, satrec: twoline2satrec(tle.line1, tle.line2) };
      } catch {
        return null;
      }
    }).filter(Boolean) as { name: string; satrec: any }[];

    if (satrecs.length === 0) {
      source.entities.removeAll();
      return;
    }

    const updateSats = () => {
      const now = new Date();
      const gm = gstime(now);
      source.entities.removeAll();

      for (const { name, satrec } of satrecs) {
        try {
          const pv = propagate(satrec, now);
          if (!pv || !pv.position || typeof pv.position === "boolean") continue;
          const geo = eciToGeodetic(pv.position as EciVec3<number>, gm);
          const lat = degreesLat(geo.latitude);
          const lon = degreesLong(geo.longitude);
          const altKm = geo.height;
          // Scale altitude for visibility (real orbit heights are too high to see dots)
          const displayAlt = Math.min(altKm, 2000) * 500 + 50_000;

          // Satellite point
          source.entities.add({
            id: `sat-${name}`,
            position: Cesium.Cartesian3.fromDegrees(lon, lat, displayAlt),
            point: {
              pixelSize: 4,
              color: SATELLITE_COLOR.withAlpha(0.8),
              disableDepthTestDistance: Number.POSITIVE_INFINITY,
            },
            label: {
              text: name,
              font: "9px JetBrains Mono, monospace",
              fillColor: SATELLITE_COLOR,
              outlineColor: Cesium.Color.BLACK,
              outlineWidth: 2,
              style: Cesium.LabelStyle.FILL_AND_OUTLINE,
              pixelOffset: new Cesium.Cartesian2(8, 0),
              distanceDisplayCondition: new Cesium.DistanceDisplayCondition(0, 5_000_000),
              disableDepthTestDistance: Number.POSITIVE_INFINITY,
            },
            // Ground track line
            polyline: {
              positions: [
                Cesium.Cartesian3.fromDegrees(lon, lat, displayAlt),
                Cesium.Cartesian3.fromDegrees(lon, lat, 0),
              ],
              width: 0.5,
              material: SATELLITE_COLOR.withAlpha(0.15),
            },
            description: `
              <div style="font-family: JetBrains Mono, monospace; font-size: 12px; color: #c8d6e5;">
                <div style="font-weight: 600; color: #00e5a0;">${name}</div>
                <div style="color: #5a6a7e; margin-top: 4px;">Alt: ${Math.round(altKm)} km</div>
                <div style="color: #5a6a7e;">Lat: ${lat.toFixed(2)}° Lon: ${lon.toFixed(2)}°</div>
              </div>
            `,
          });
        } catch { /* skip bad propagation */ }
      }
    };

    updateSats();
    satIntervalRef.current = window.setInterval(updateSats, 3000);

    return () => {
      if (satIntervalRef.current) clearInterval(satIntervalRef.current);
    };
  }, [tleData]);

  // -----------------------------------------------------------------------
  // Render
  // -----------------------------------------------------------------------

  const filterButtons: { mode: FilterMode; label: string }[] = [
    { mode: "normal", label: "NORMAL" },
    { mode: "crt", label: "CRT" },
    { mode: "nvg", label: "NVG" },
    { mode: "flir", label: "FLIR" },
  ];

  return (
    <div style={{ width: "100%", height: "100%", position: "relative" }}>
      <div
        ref={containerRef}
        style={{ width: "100%", height: "100%" }}
      />

      {/* Filter mode buttons */}
      <div
        style={{
          position: "absolute",
          top: 12,
          left: 12,
          zIndex: 10,
          display: "flex",
          gap: 1,
          fontFamily: "var(--font-mono)",
          fontSize: 9,
          letterSpacing: 1,
        }}
      >
        {filterButtons.map(({ mode, label }) => (
          <button
            key={mode}
            onClick={() => setFilterMode(mode)}
            style={{
              padding: "4px 8px",
              background:
                filterMode === mode
                  ? mode === "nvg"
                    ? "rgba(0,229,160,0.15)"
                    : mode === "flir"
                      ? "rgba(248,81,73,0.15)"
                      : mode === "crt"
                        ? "rgba(88,166,255,0.15)"
                        : "rgba(88,166,255,0.15)"
                  : "rgba(10,14,20,0.85)",
              border: `1px solid ${
                filterMode === mode
                  ? mode === "nvg"
                    ? "#00e5a0"
                    : mode === "flir"
                      ? "#f85149"
                      : "#58a6ff"
                  : "var(--border)"
              }`,
              color:
                filterMode === mode
                  ? mode === "nvg"
                    ? "#00e5a0"
                    : mode === "flir"
                      ? "#f85149"
                      : "#58a6ff"
                  : "var(--text-secondary)",
              cursor: "pointer",
              fontFamily: "inherit",
              fontSize: "inherit",
              letterSpacing: "inherit",
              fontWeight: 600,
              borderRadius:
                mode === "normal"
                  ? "3px 0 0 3px"
                  : mode === "flir"
                    ? "0 3px 3px 0"
                    : "0",
            }}
          >
            {label}
          </button>
        ))}
      </div>
    </div>
  );
}
