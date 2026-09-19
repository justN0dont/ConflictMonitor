"""CelesTrak satellite TLE fetching service.

Fetches Two-Line Element sets from CelesTrak for military satellites
and caches them. TLE data is refreshed every 6 hours.
"""

import asyncio
import json
import logging
import os
import time

import httpx

logger = logging.getLogger(__name__)

CELESTRAK_URL = "https://celestrak.org/NORAD/elements/gp.php"
TLE_GROUPS = ["military"]
REFRESH_INTERVAL = 6 * 3600  # 6 hours

# CelesTrak rate-limits by IP and states the block lifts "once the excessive downloads have
# ceased". Without an on-disk cache every process start refetches, so uvicorn --reload plus
# container restarts renew the block indefinitely. This host earned exactly that on 2026-09-18.
CACHE_PATH = "/app/.cache/tles.json"
BACKOFF_ON_BLOCK = 3600  # a 403 means back off, not retry on the normal cadence

_tle_cache: list[dict] = []


def _load_cache() -> float:
    """Load TLEs written by a previous process. Returns the cache age in seconds, or inf."""
    try:
        with open(CACHE_PATH, encoding="utf-8") as fh:
            blob = json.load(fh)
        tles = blob.get("tles") or []
        if not tles:
            return float("inf")
        _tle_cache.clear()
        _tle_cache.extend(tles)
        age = time.time() - float(blob.get("fetched_at") or 0)
        logger.info("TLE cache loaded from disk: %d satellites, %.1fh old", len(tles), age / 3600)
        return age
    except FileNotFoundError:
        return float("inf")
    except Exception as e:
        logger.warning("TLE cache unreadable (%s) — will refetch", e)
        return float("inf")


def _save_cache(tles: list[dict]) -> None:
    try:
        os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
        tmp = CACHE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"fetched_at": time.time(), "tles": tles}, fh)
        os.replace(tmp, CACHE_PATH)  # atomic: never leave a half-written cache
    except Exception as e:
        logger.warning("Could not persist TLE cache: %s", e)


async def start_tle_fetcher():
    """Background task that fetches TLE data from CelesTrak."""
    async with httpx.AsyncClient(
        timeout=60,
        headers={"User-Agent": "ConflictMonitor/1.0"},
    ) as client:
        # Start from disk. A fresh cache means this process does not touch CelesTrak at all.
        age = _load_cache()
        if age < REFRESH_INTERVAL:
            await asyncio.sleep(REFRESH_INTERVAL - age)

        while True:
            blocked = False
            try:
                all_tles: list[dict] = []
                for group in TLE_GROUPS:
                    resp = await client.get(
                        CELESTRAK_URL,
                        params={"GROUP": group, "FORMAT": "tle"},
                    )
                    if resp.status_code == 200:
                        lines = resp.text.strip().split("\n")
                        count = 0
                        for i in range(0, len(lines) - 2, 3):
                            name = lines[i].strip()
                            line1 = lines[i + 1].strip()
                            line2 = lines[i + 2].strip()
                            if line1.startswith("1 ") and line2.startswith("2 "):
                                all_tles.append(
                                    {"name": name, "line1": line1, "line2": line2}
                                )
                                count += 1
                        logger.info("CelesTrak [%s]: %d satellites", group, count)
                    else:
                        if resp.status_code in (403, 429):
                            blocked = True
                        logger.warning(
                            "CelesTrak %s returned %d", group, resp.status_code
                        )

                # Only replace the cache on success. A failed fetch used to clear it and extend
                # with an empty list, so one 403 turned a good cache into zero satellites.
                if all_tles:
                    _tle_cache.clear()
                    _tle_cache.extend(all_tles)
                    _save_cache(all_tles)
                    logger.info("Total TLEs cached: %d", len(_tle_cache))
                else:
                    logger.warning(
                        "CelesTrak returned nothing — keeping %d cached satellites",
                        len(_tle_cache),
                    )
            except Exception as e:
                logger.error("CelesTrak fetch error: %s", e)

            await asyncio.sleep(BACKOFF_ON_BLOCK if blocked else REFRESH_INTERVAL)


def get_tles() -> list[dict]:
    return _tle_cache
