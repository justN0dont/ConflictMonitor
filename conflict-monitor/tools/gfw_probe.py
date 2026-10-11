"""Probe Global Fishing Watch's AIS vessel presence for one of the monitor's AIS regions.

Why: the vessel feed (AISStream, free tier) is shore receivers, and it sees nothing
in the Red Sea, the Caspian and, some days, the Gulf (FINDINGS.md, Measured facts,
"AIS by region"). Global Fishing Watch serves satellite-plus-terrestrial AIS (one
provider, Kpler, since 2026) as *presence*: one position per vessel per hour, summed
per 0.1 deg cell, published about 96 hours behind. Never a live layer; possibly the
denominator the hatch lacks: "GFW saw N vessels a day here last week; we see 0 now".

What it measures, for one region box of feeds.REGIONS (the box, not the sea cut;
GFW only has cells at sea anyway):
  1. vessels and vessel-hours per day over the last --days days, and the newest
     day with data (the real lag, not the documented one);
  2. the split by vessel type over the newest 7 days with data;
  3. how many 0.1 deg cells had any presence in those 7 days.

Run (host or backend container; stdlib only):
    GFW_API_TOKEN=... python tools/gfw_probe.py red_sea --days 14 [--out probe.json]

The token: a free Global Fishing Watch account, https://globalfishingwatch.org/our-apis/tokens
(issued instantly, never expires, 50,000 requests a day, one report at a time).
Licence CC BY-NC 4.0: non-commercial use only, and anything published says
"Powered by Global Fishing Watch". The register records what each run found.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))
from app.feeds import REGIONS  # noqa: E402

GATEWAY = "https://gateway.api.globalfishingwatch.org/v3/4wings/report"
DATASET = "public-global-presence:latest"
TOKENS_URL = "https://globalfishingwatch.org/our-apis/tokens"
# Cloudflare in front of the gateway answers 403 "browser signature banned" to
# urllib's default User-Agent; curl's passes. Say who is asking.
USER_AGENT = "conflict-monitor/gfw_probe (https://github.com/justN0dont/ConflictMonitor)"


def box_polygon(bbox) -> dict:
    lat_min, lat_max, lon_min, lon_max = bbox
    ring = [[lon_min, lat_min], [lon_max, lat_min], [lon_max, lat_max], [lon_min, lat_max], [lon_min, lat_min]]
    return {"type": "Polygon", "coordinates": [ring]}


def report(token: str, geometry: dict, start: dt.date, end: dt.date, **params: str) -> list[dict]:
    """One 4Wings report over the box; rows flattened whatever the envelope."""
    query = {
        "datasets[0]": DATASET,
        "date-range": f"{start.isoformat()},{end.isoformat()}",
        "format": "JSON",
        "spatial-resolution": "LOW",
        **params,
    }
    req = urllib.request.Request(
        GATEWAY + "?" + urllib.parse.urlencode(query),
        data=json.dumps({"geojson": geometry}).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json", "Accept": "application/json", "User-Agent": USER_AGENT},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            body = json.load(resp)
    except urllib.error.HTTPError as e:
        detail = e.read(400).decode("utf-8", "replace")
        raise SystemExit(f"HTTP {e.code} from {GATEWAY} ({query}): {detail}") from None
    rows: list[dict] = []
    if isinstance(body, dict) and "entries" in body:
        for entry in body["entries"]:
            for value in entry.values():
                if isinstance(value, list):
                    rows.extend(value)
    elif isinstance(body, list):
        rows = body
    else:
        raise SystemExit(f"unexpected response shape: {json.dumps(body)[:400]}")
    return rows


def num(row: dict, *keys: str) -> float:
    for k in keys:
        if row.get(k) is not None:
            return float(row[k])
    return 0.0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("region", choices=[s.key for s in REGIONS.values()])
    ap.add_argument("--days", type=int, default=14, help="how far back the daily series runs (default 14)")
    ap.add_argument("--out", help="write the raw rows of every call here, as JSON")
    args = ap.parse_args()

    token = os.environ.get("GFW_API_TOKEN", "").strip()
    if not token:
        print(f"GFW_API_TOKEN is not set. Create one (free, instant) at {TOKENS_URL}, put it in .env, re-run.")
        return 2

    spec = REGIONS[f"vessels:{args.region}"]
    geometry = box_polygon(spec.bbox)
    today = dt.datetime.now(dt.timezone.utc).date()
    start = today - dt.timedelta(days=args.days)
    print(f"{spec.name} ({args.region}) box {spec.bbox}; {DATASET}; {start} .. {today}; asked {today} UTC")

    raw: dict[str, list[dict]] = {}

    # 1. The daily series, summed over the box.
    daily = report(token, geometry, start, today, **{"temporal-resolution": "DAILY", "spatial-aggregation": "true"})
    raw["daily"] = daily
    if daily:
        print("row keys:", sorted(daily[0].keys()))
    by_day: dict[str, tuple[float, float]] = {}
    for r in daily:
        d = str(r.get("date"))
        v, h = by_day.get(d, (0.0, 0.0))
        by_day[d] = (v + num(r, "vesselIDs", "vessel_ids"), h + num(r, "hours"))
    print("\n| day (UTC) | vessels | vessel-hours |\n|---|---|---|")
    for d in sorted(by_day):
        v, h = by_day[d]
        print(f"| {d} | {v:.0f} | {h:.0f} |")
    with_data = [d for d, (v, h) in sorted(by_day.items()) if v > 0 or h > 0]
    if not with_data:
        print("\nno presence rows at all in the window")
        newest = None
    else:
        newest = dt.date.fromisoformat(with_data[-1][:10])
        print(f"\nnewest day with data: {newest}, {(today - newest).days} days before today (documented lag: about 96 h)")

    if newest is not None:
        week_start = newest - dt.timedelta(days=6)
        # 2. By vessel type over the newest week with data.
        try:
            by_type = report(
                token, geometry, week_start, newest + dt.timedelta(days=1),
                **{"temporal-resolution": "ENTIRE", "spatial-aggregation": "true", "group-by": "VESSEL_TYPE"},
            )
            raw["by_type"] = by_type
            print(f"\n{week_start} .. {newest}, by vessel type:\n\n| type | vessels | vessel-hours |\n|---|---|---|")
            for r in sorted(by_type, key=lambda r: -num(r, "vesselIDs", "vessel_ids")):
                t = r.get("vessel_type") or r.get("vesselType") or "?"
                print(f"| {t} | {num(r, 'vesselIDs', 'vessel_ids'):.0f} | {num(r, 'hours'):.0f} |")
        except SystemExit as e:
            print(f"\nby-type report failed, kept going: {e}")

        # 3. Cells with any presence in that week.
        cells = report(token, geometry, week_start, newest + dt.timedelta(days=1), **{"temporal-resolution": "ENTIRE"})
        raw["cells"] = cells
        lit = [r for r in cells if num(r, "hours") > 0 or num(r, "vesselIDs", "vessel_ids") > 0]
        lats = [float(r["lat"]) for r in lit if r.get("lat") is not None]
        lons = [float(r["lon"]) for r in lit if r.get("lon") is not None]
        print(f"\ncells (0.1 deg) with presence in that week: {len(lit)} of {len(cells)} returned")
        if lats and lons:
            print(f"their extent: lat {min(lats):.1f}..{max(lats):.1f}, lon {min(lons):.1f}..{max(lons):.1f}")

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump({"region": args.region, "bbox": list(spec.bbox), "dataset": DATASET, "asked": today.isoformat(), **raw}, f)
        print(f"\nraw rows written to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
