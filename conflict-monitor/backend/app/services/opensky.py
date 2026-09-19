"""Aircraft tracking service.

Uses adsb.lol as the primary free ADS-B data source (no auth required).
Falls back to OpenSky Network if configured.  Detects likely GPS-jamming
zones by clustering aircraft with degraded navigation accuracy.
"""

import asyncio
import logging
from collections import defaultdict

import httpx

from app.config import settings
from app.services.track_history import record_aircraft_position

logger = logging.getLogger(__name__)

# Centre of the Middle East bounding box
CENTRE_LAT = 30
CENTRE_LON = 45
RADIUS_NM = 2500  # ~covers lat 15-45, lon 25-65

POLL_INTERVAL = 15  # seconds

_cache: dict = {"states": [], "timestamp": 0, "jamming": []}


def _detect_jamming(states: list[dict]) -> list[dict]:
    """Infer GPS jamming zones from aircraft with degraded navigation accuracy.

    Uses two signals:
    - position_source == 2 (MLAT fallback, from OpenSky)
    - nac_p == 0 and nic == 0 (degraded GPS accuracy, from adsb.lol)
    """
    jammed = [
        s for s in states
        if not s.get("on_ground") and (
            s.get("position_source") == 2
            or (s.get("nac_p") == 0 and s.get("nic") == 0)
        )
        and s.get("lat") is not None and s.get("lon") is not None
    ]
    if len(jammed) < 2:
        return []

    grid_size = 0.8  # degrees (~90 km)
    grid: dict[tuple[float, float], list[dict]] = defaultdict(list)
    for ac in jammed:
        key = (
            round(ac["lat"] / grid_size) * grid_size,
            round(ac["lon"] / grid_size) * grid_size,
        )
        grid[key].append(ac)

    zones = []
    for (lat, lon), aircraft in grid.items():
        if len(aircraft) >= 2:
            zones.append({
                "lat": lat,
                "lon": lon,
                "radius_km": grid_size * 111 / 2,
                "aircraft_count": len(aircraft),
                "intensity": min(len(aircraft) / 8, 1.0),
            })
    return zones


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
    bbox = {"lamin": 15, "lamax": 45, "lomin": 25, "lomax": 65}
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
               httpx.AsyncClient(timeout=30) as adsb_client:
        while True:
            # Try adsb.lol first (free, no auth, no rate limits)
            states = await _poll_adsb_lol(adsb_client)

            # Fall back to OpenSky if adsb.lol failed
            if states is None:
                states = await _poll_opensky(opensky_client, opensky_auth)

            if states is not None:
                _cache["states"] = states
                _cache["timestamp"] = int(asyncio.get_event_loop().time())
                _cache["jamming"] = _detect_jamming(states)
                for ac in states:
                    if not ac.get("on_ground"):
                        record_aircraft_position(ac["icao24"], ac["lon"], ac["lat"])
                logger.info(
                    "Aircraft: %d tracked, %d jamming zones (source: %s)",
                    len(states),
                    len(_cache["jamming"]),
                    "adsb.lol" if states else "opensky",
                )

            await asyncio.sleep(POLL_INTERVAL)


def get_aircraft() -> list[dict]:
    return _cache["states"]


def get_jamming_zones() -> list[dict]:
    return _cache["jamming"]
