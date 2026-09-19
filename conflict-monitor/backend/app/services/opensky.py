"""Aircraft tracking service.

Uses adsb.lol as the primary free ADS-B data source (no auth required).
Falls back to OpenSky Network if configured.  Detects likely GPS-jamming
zones by clustering aircraft with degraded navigation accuracy.
"""

import asyncio
import logging
import math
import time
from collections import defaultdict

import httpx

from app.config import settings
from app.services.track_history import record_aircraft_position

logger = logging.getLogger(__name__)

# Centre of the Middle East bounding box
CENTRE_LAT = 29.5
CENTRE_LON = 45.5
RADIUS_NM = 650  # ~lat 23-40, lon 33-56 (Levant -> Hormuz); measured 139 aircraft / 84KB per poll

POLL_INTERVAL = 15  # seconds


def _nm_from_centre(lat: float, lon: float) -> float:
    """Great-circle distance from the AO centre, in nautical miles."""
    dlat = math.radians(lat - CENTRE_LAT)
    dlon = math.radians(lon - CENTRE_LON)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(CENTRE_LAT)) * math.cos(math.radians(lat)) * math.sin(dlon / 2) ** 2)
    return 3440.065 * 2 * math.asin(math.sqrt(min(1.0, a)))


def _opensky_bbox() -> dict:
    """The smallest lat/lon box containing the AO circle.

    adsb.lol takes a centre and a radius; OpenSky only takes a bounding box. Deriving
    the box from the same constants keeps the two poll paths pointed at one AO — they
    disagreed before, with the fallback still requesting the pre-2026-09-18 box. The
    box circumscribes the circle, so results are filtered back to RADIUS_NM by the caller.
    """
    dlat = RADIUS_NM / 60.0  # one nautical mile is 1/60 degree of latitude
    dlon = dlat / max(math.cos(math.radians(CENTRE_LAT)), 0.01)
    return {
        "lamin": round(max(CENTRE_LAT - dlat, -90.0), 4),
        "lamax": round(min(CENTRE_LAT + dlat, 90.0), 4),
        "lomin": round(max(CENTRE_LON - dlon, -180.0), 4),
        "lomax": round(min(CENTRE_LON + dlon, 180.0), 4),
    }

# Published gpsjam.org degradation threshold: nic < 7 OR nac_p < 8.
NIC_THRESHOLD = 7
NAC_P_THRESHOLD = 8

# A ratio is only meaningful with a real denominator: without a floor, a cell
# holding 2 aircraft that both happen to be degraded would read as 100%.
MIN_CELL_AIRCRAFT = 10
MIN_RATIO = 0.25

# adsb.lol rejects generic clients with 403 "User-Agent too generic; include
# valid contact info." Without a contact UA the live feed never returns.
ADSB_USER_AGENT = "conflict-monitor/1.0 (+https://github.com/troofevades-rgb/conflict-monitor)"

_cache: dict = {
    "states": [],
    "timestamp": 0,
    "source": None,
    "jamming": [],
    "jamming_status": "no_integrity_data",
    "cells_evaluated": 0,
    "aircraft_evaluable": 0,
}


def _detect_jamming(states: list[dict]) -> dict:
    """Measure degraded navigation integrity per grid cell, as a ratio.

    A position counts as degraded when it fails the published gpsjam.org
    threshold: nic < 7 or nac_p < 8.  An aircraft carrying neither field
    cannot be evaluated (OpenSky state vectors carry no integrity fields at
    all), so it is excluded from the numerator AND the denominator and the
    returned status says so — "I wasn't looking" must never render as
    "nothing happened".
    """
    grid_size = 0.8  # degrees (~90 km)
    grid: dict[tuple[float, float], list[bool]] = defaultdict(list)
    evaluable = 0

    for s in states:
        if s.get("on_ground"):
            continue
        if s.get("lat") is None or s.get("lon") is None:
            continue
        nic = s.get("nic")
        nac_p = s.get("nac_p")
        if nic is None and nac_p is None:
            continue  # unevaluable — no integrity fields to judge
        evaluable += 1
        degraded = (
            (nic is not None and nic < NIC_THRESHOLD)
            or (nac_p is not None and nac_p < NAC_P_THRESHOLD)
        )
        key = (
            round(s["lat"] / grid_size) * grid_size,
            round(s["lon"] / grid_size) * grid_size,
        )
        grid[key].append(degraded)

    zones = []
    cells_evaluated = 0
    for (lat, lon), flags in grid.items():
        total = len(flags)
        if total < MIN_CELL_AIRCRAFT:
            continue
        cells_evaluated += 1
        degraded_count = sum(flags)
        ratio = degraded_count / total
        if ratio < MIN_RATIO:
            continue
        zones.append({
            "lat": lat,
            "lon": lon,
            "radius_km": grid_size * 111 / 2,
            "degraded": degraded_count,
            "total": total,
            "ratio": ratio,
            "intensity": min(max(ratio, 0.0), 1.0),
        })

    if evaluable == 0:
        status = "no_integrity_data"
    elif cells_evaluated == 0:
        status = "insufficient_coverage"
    else:
        status = "ok"

    return {
        "status": status,
        "zones": zones,
        "cells_evaluated": cells_evaluated,
        "aircraft_evaluable": evaluable,
    }


async def _poll_adsb_lol(client: httpx.AsyncClient) -> list[dict] | None:
    """Fetch aircraft from adsb.lol (free, no auth)."""
    try:
        resp = await client.get(
            f"https://api.adsb.lol/v2/lat/{CENTRE_LAT}/lon/{CENTRE_LON}/dist/{RADIUS_NM}",
        )
        if resp.status_code == 200:
            data = resp.json()
            ac_list = data.get("ac") or []
            states = []
            for a in ac_list:
                lat = a.get("lat")
                lon = a.get("lon")
                if lat is None or lon is None:
                    continue
                states.append({
                    "icao24": a.get("hex", ""),
                    "callsign": (a.get("flight") or "").strip(),
                    "origin_country": "",
                    "lon": lon,
                    "lat": lat,
                    "altitude": a.get("alt_geom") or a.get("alt_baro"),
                    "velocity": a.get("gs"),
                    "heading": a.get("track") or a.get("true_heading"),
                    "on_ground": a.get("alt_baro") == "ground",
                    "position_source": 2 if a.get("mlat") and "lat" in a.get("mlat", []) else 0,
                    "nac_p": a.get("nac_p"),
                    "nic": a.get("nic"),
                })
            return states
        else:
            logger.warning("adsb.lol returned %d", resp.status_code)
    except Exception as e:
        logger.error("adsb.lol poll error: %s", e)
    return None


async def _poll_opensky(client: httpx.AsyncClient, auth: tuple | None) -> list[dict] | None:
    """Fetch aircraft from OpenSky Network (fallback)."""
    bbox = _opensky_bbox()
    try:
        resp = await client.get(
            "https://opensky-network.org/api/states/all",
            params=bbox,
        )
        if resp.status_code == 200:
            data = resp.json()
            states_raw = data.get("states") or []
            states = [
                {
                    "icao24": s[0],
                    "callsign": (s[1] or "").strip(),
                    "origin_country": s[2],
                    "lon": s[5],
                    "lat": s[6],
                    "altitude": s[7] if s[7] is not None else s[13],
                    "velocity": s[9],
                    "heading": s[10],
                    "on_ground": s[8],
                    "position_source": s[16] if len(s) > 16 else 0,
                }
                for s in states_raw
                if s[5] is not None and s[6] is not None
                and _nm_from_centre(s[6], s[5]) <= RADIUS_NM
            ]
            return states
        elif resp.status_code in (429, 401):
            logger.warning("OpenSky %d — skipping", resp.status_code)
        else:
            logger.warning("OpenSky API returned %d", resp.status_code)
    except Exception as e:
        logger.error("OpenSky poll error: %s", e)
    return None


async def start_opensky_poller():
    """Background task that polls ADS-B sources every POLL_INTERVAL seconds."""
    opensky_auth = None
    if settings.opensky_username and settings.opensky_password:
        opensky_auth = (settings.opensky_username, settings.opensky_password)
        logger.info("OpenSky fallback: authenticated as %s", settings.opensky_username)

    logger.info("Aircraft tracking: using adsb.lol (primary), polling every %ds", POLL_INTERVAL)

    async with httpx.AsyncClient(timeout=30, auth=opensky_auth) as opensky_client, \
               httpx.AsyncClient(timeout=30, headers={"User-Agent": ADSB_USER_AGENT}) as adsb_client:
        while True:
            # Try adsb.lol first (free, no auth, no rate limits)
            states = await _poll_adsb_lol(adsb_client)
            source = "adsb.lol"

            # Fall back to OpenSky if adsb.lol failed
            if states is None:
                states = await _poll_opensky(opensky_client, opensky_auth)
                source = "opensky"

            if states is not None:
                interference = _detect_jamming(states)
                _cache["states"] = states
                _cache["timestamp"] = int(time.time())
                _cache["source"] = source
                _cache["jamming"] = interference["zones"]
                _cache["jamming_status"] = interference["status"]
                _cache["cells_evaluated"] = interference["cells_evaluated"]
                _cache["aircraft_evaluable"] = interference["aircraft_evaluable"]
                for ac in states:
                    if not ac.get("on_ground"):
                        record_aircraft_position(ac["icao24"], ac["lon"], ac["lat"])
                logger.info(
                    "Aircraft: %d tracked, %d interference zones, status=%s (source: %s)",
                    len(states),
                    len(interference["zones"]),
                    interference["status"],
                    source,
                )

            await asyncio.sleep(POLL_INTERVAL)


def get_aircraft() -> list[dict]:
    return _cache["states"]


def get_jamming_zones() -> dict:
    """Return the current interference measurement plus its provenance."""
    return {
        "status": _cache.get("jamming_status", "no_integrity_data"),
        "source": _cache.get("source"),
        "as_of": _cache.get("timestamp", 0),
        "cells_evaluated": _cache.get("cells_evaluated", 0),
        "aircraft_evaluable": _cache.get("aircraft_evaluable", 0),
        "zones": _cache.get("jamming", []),
    }
