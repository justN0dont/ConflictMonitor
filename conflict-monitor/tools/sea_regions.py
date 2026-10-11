#!/usr/bin/env python3
"""Cut the vessel feed's region boxes to the sea, for the coverage layer (C88).

The map draws AIS coverage by region (C83, frontend/src/lib/coverage.ts). The
regions are boxes - the units feeds.region_of attributes a position by - and a
box drawn as a box hatches the land inside it, which reads as a claim about the
land. This writes frontend/src/data/sea_regions.json: each region's box minus
every box before it (first match wins, exactly as the backend attributes), cut
to Natural Earth's ocean polygon. The box stays the unit the monitor measures
by; only the drawing changes, and the frontend draws the box again for any
region whose registry entry no longer matches the file (coverage.ts,
seaGeometry), so a registry edit can never be drawn with yesterday's shape.

Provenance travels in the file: the ocean file's upstream URL at a pinned
commit, its sha256, the simplification tolerance, the registry it was cut
from. Natural Earth is public domain. Its ocean polygon draws the Caspian Sea
as ocean; other inland water inside a box (Lake Nasser, Lake Van) is not drawn.
The coastline is Natural Earth's cartographic choice, simplified to TOLERANCE.

Runs inside the backend container, where shapely and app.feeds are:

  curl -sSL -o ne_10m_ocean.geojson "$URL"          # URL below; sha256 is checked
  MSYS_NO_PATHCONV=1 docker cp ne_10m_ocean.geojson conflict-monitor-backend-1:/tmp/
  MSYS_NO_PATHCONV=1 docker cp tools/sea_regions.py conflict-monitor-backend-1:/tmp/
  MSYS_NO_PATHCONV=1 docker compose exec -T backend \
      python /tmp/sea_regions.py /tmp/ne_10m_ocean.geojson /tmp/sea_regions.json
  MSYS_NO_PATHCONV=1 docker cp conflict-monitor-backend-1:/tmp/sea_regions.json frontend/src/data/

Re-run it whenever feeds.REGIONS changes; frontend/src/lib/coverage.test.ts
pins the file's registry to the one it expects.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import sys

import shapely
from shapely.geometry import box, shape
from shapely.geometry.polygon import orient
from shapely.ops import unary_union

# nvkelso/natural-earth-vector, "final v5.0.0 geojson", 2021-12-08.
COMMIT = "ace5fed0eaf3c6c03c951e75b439ba8fffbc218e"
URL = f"https://raw.githubusercontent.com/nvkelso/natural-earth-vector/{COMMIT}/geojson/ne_10m_ocean.geojson"
SHA256 = "f9696a1337c746a0f6c8c13bc60d0f230d2ef8d105198d5657726c8f8e763fc2"
# Douglas-Peucker with topology kept, in degrees: GEV's choice for the same
# source (GODS-EYE-VIEW.md, DISPLAY-14). Applied to the water ONCE, before the
# boxes cut it, so two regions that share a box edge share it exactly.
TOLERANCE = 0.01
DECIMALS = 4  # ~11 m, under the tolerance
# Parts smaller than this (about a square kilometre) are slivers the box edges
# leave along a coast, invisible at any zoom and degenerate once rounded.
MIN_PART_DEG2 = 1e-4


def regions() -> list[tuple[str, str, tuple[float, float, float, float]]]:
    sys.path.insert(0, os.getcwd())
    from app.feeds import REGIONS

    return [(s.key, s.name, tuple(float(x) for x in s.bbox)) for s in REGIONS.values() if s.feed == "vessels"]


def rect(bbox):
    """The registry's (lat_min, lat_max, lon_min, lon_max) as a shapely box."""
    a, b, c, d = bbox
    return box(c, a, d, b)


def rings(polygons) -> list[list[list[list[float]]]]:
    """MultiPolygon coordinates: exterior counter-clockwise, holes clockwise, rounded."""

    def ring(coords):
        return [[round(x, DECIMALS), round(y, DECIMALS)] for x, y in coords]

    out = []
    for p in polygons:
        p = orient(p)
        out.append([ring(p.exterior.coords)] + [ring(i.coords) for i in p.interiors])
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("ocean", help="ne_10m_ocean.geojson, fetched from URL")
    ap.add_argument("out", help="where to write sea_regions.json")
    args = ap.parse_args()

    with open(args.ocean, "rb") as fh:
        raw = fh.read()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != SHA256:
        sys.exit(f"{args.ocean}: sha256 {digest} is not the pinned {SHA256}; fetch {URL}")
    ocean = unary_union([shape(f["geometry"]) for f in json.loads(raw)["features"]])

    regs = regions()
    envelope = unary_union([rect(b) for _, _, b in regs])
    water = ocean.intersection(envelope).simplify(TOLERANCE, preserve_topology=True)

    features = []
    earlier = []
    for key, name, bbox in regs:
        effective = rect(bbox).difference(unary_union(earlier)) if earlier else rect(bbox)
        earlier.append(rect(bbox))
        parts = [g for g in shapely.get_parts(effective.intersection(water)) if g.geom_type == "Polygon" and g.area >= MIN_PART_DEG2]
        coords = rings(parts)
        vertices = sum(len(r) for poly in coords for r in poly)
        features.append({
            "type": "Feature",
            "properties": {"key": key, "name": name, "bbox": list(bbox), "parts": len(coords), "vertices": vertices},
            "geometry": {"type": "MultiPolygon", "coordinates": coords},
        })
        print(f"{key:12s} {len(coords):3d} parts {vertices:6d} vertices  sea {sum(p.area for p in parts):7.2f} of box {rect(bbox).area:7.2f} deg^2")

    provenance = {
        "generator": "tools/sea_regions.py",
        "generated": datetime.date.today().isoformat(),
        "rule": (
            "each region is its box minus every box before it (feeds.region_of, first match wins), cut to the "
            "water; the box stays the unit the monitor measures by, and the drawing inherits Natural Earth's "
            "choice of coastline"
        ),
        "registry": [{"key": k, "bbox": list(b)} for k, _, b in regs],
        "water": {
            "source": (
                "Natural Earth 1:10m ocean (ne_10m_ocean.geojson), public domain; it draws the Caspian Sea as "
                "ocean, and other inland water inside a box is not drawn"
            ),
            "url": URL,
            "commit": COMMIT,
            "sha256": SHA256,
        },
        "simplify_tolerance_deg": TOLERANCE,
        "decimals": DECIMALS,
        "min_part_deg2": MIN_PART_DEG2,
    }
    with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
        fh.write('{"type":"FeatureCollection",\n"provenance":')
        fh.write(json.dumps(provenance, indent=1))
        fh.write(',\n"features":[\n')
        fh.write(",\n".join(json.dumps(f, separators=(",", ":")) for f in features))
        fh.write("\n]}\n")
    print(f"wrote {args.out}: {os.path.getsize(args.out):,} bytes")


if __name__ == "__main__":
    main()
