"""CelesTrak satellite TLE fetching service.

Fetches Two-Line Element sets from CelesTrak for military satellites
and caches them. TLE data is refreshed every 6 hours.

Feed health (docs/FINDINGS.md, Roadmap > Phase 2 > feed_health decisions):
every fetch attempt reaches the "satellites" tracker. Its upstream clock is
the newest element-set EPOCH, parsed from line 1 and checksum-verified, so a
fresh fetch of old orbits reads `stale`, not live, and a cache loaded from
disk carries the time it was actually fetched, not the time it was loaded.
Closes the untriaged "TLE epoch age is never checked" item.
"""

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone

import httpx

from app import feeds
from app.feeds import ErrorKind

logger = logging.getLogger(__name__)

CELESTRAK_URL = "https://celestrak.org/NORAD/elements/gp.php"
# ONE group. The success test below ("any record parsed") is only right for one:
# before a second group is added, outcomes must become per-group, or a 403 on
# one group will be reported as a success because another answered.
TLE_GROUPS = ["military"]
REFRESH_INTERVAL = 6 * 3600  # 6 hours

# CelesTrak rate-limits by IP and states the block lifts "once the excessive downloads have
# ceased". Without an on-disk cache every process start refetches, so uvicorn --reload plus
# container restarts renew the block indefinitely. This host earned exactly that on 2026-09-18.
CACHE_PATH = "/app/.cache/tles.json"
BACKOFF_ON_BLOCK = 3600  # a 403 means back off, not retry on the normal cadence

_tle_cache: list[dict] = []


# ── Element-set parsing ──────────────────────────────────────────────────────

def _checksum_ok(line: str) -> bool:
    """TLE mod-10 checksum: digits count their value, '-' counts 1, the rest 0."""
    if len(line) < 69 or not line[68].isdigit():
        return False
    total = sum(int(c) if c.isdigit() else (1 if c == "-" else 0) for c in line[:68])
    return total % 10 == int(line[68])


def epoch_utc(line1: str) -> float | None:
    """The element-set epoch from line 1, columns 19-32 (YYDDD.DDDDDDDD), as epoch
    seconds, or None. Two-digit years below 57 are 20YY (the NORAD convention)."""
    try:
        field = line1[18:32]
        yy = int(field[:2])
        day = float(field[2:])
    except (ValueError, IndexError):
        return None
    if not 1 <= day < 367:
        return None
    year = 2000 + yy if yy < 57 else 1900 + yy
    start = datetime(year, 1, 1, tzinfo=timezone.utc)
    return (start + timedelta(days=day - 1)).timestamp()


def parse_tle_text(text: str) -> tuple[list[dict], int]:
    """Three-line records from CelesTrak's FORMAT=tle. Returns (records, rejected).

    A record is kept only when both lines carry the right leading digit and a
    valid checksum and its epoch parses; anything else is counted, never dated
    by guess. Each kept record carries `epoch_utc`.
    """
    lines = [l.rstrip("\r") for l in text.strip().split("\n")]
    records, rejected = [], 0
    for i in range(0, len(lines) - 2, 3):
        name, line1, line2 = lines[i].strip(), lines[i + 1].strip(), lines[i + 2].strip()
        if not (line1.startswith("1 ") and line2.startswith("2 ")):
            rejected += 1
            continue
        epoch = epoch_utc(line1)
        if epoch is None or not _checksum_ok(line1) or not _checksum_ok(line2):
            rejected += 1
            continue
        records.append({"name": name, "line1": line1, "line2": line2, "epoch_utc": epoch})
    return records, rejected


def _newest_epoch(records: list[dict]) -> float | None:
    epochs = [r["epoch_utc"] for r in records if r.get("epoch_utc") is not None]
    return max(epochs) if epochs else None


# ── Disk cache ───────────────────────────────────────────────────────────────

def _load_cache(now: float) -> float:
    """Load TLEs written by a previous process and tell the tracker WHEN THEY
    WERE FETCHED. Returns the cache age in seconds, or inf."""
    try:
        with open(CACHE_PATH, encoding="utf-8") as fh:
            blob = json.load(fh)
        tles = blob.get("tles") or []
        fetched_at = float(blob.get("fetched_at") or 0)
        if not tles or fetched_at <= 0:
            return float("inf")
        # Records written before this commit carry no epoch; re-derive it.
        for r in tles:
            if r.get("epoch_utc") is None:
                r["epoch_utc"] = epoch_utc(r.get("line1", ""))
        _tle_cache.clear()
        _tle_cache.extend(tles)
        feeds.tracker("satellites").succeeded(
            fetched_at, count=len(tles), source="celestrak (disk cache)",
            source_epoch=_newest_epoch(tles),
        )
        age = now - fetched_at
        logger.info("TLE cache loaded from disk: %d satellites, %.1fh old", len(tles), age / 3600)
        return age
    except FileNotFoundError:
        return float("inf")
    except Exception as e:
        logger.warning("TLE cache unreadable (%s) — will refetch", e)
        return float("inf")


def _save_cache(tles: list[dict], fetched_at: float) -> None:
    try:
        os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
        tmp = CACHE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"fetched_at": fetched_at, "tles": tles}, fh)
        os.replace(tmp, CACHE_PATH)  # atomic: never leave a half-written cache
    except Exception as e:
        logger.warning("Could not persist TLE cache: %s", e)


# ── One fetch ────────────────────────────────────────────────────────────────

async def fetch_once(client, now: float) -> ErrorKind:
    """Fetch every group once, update the cache on success, and report the
    outcome to the tracker. Returns the outcome."""
    tracker = feeds.tracker("satellites")
    all_tles: list[dict] = []
    rejected = 0
    failure: tuple[ErrorKind, str] | None = None
    try:
        for group in TLE_GROUPS:
            resp = await client.get(CELESTRAK_URL, params={"GROUP": group, "FORMAT": "tle"})
            if resp.status_code == 200:
                records, bad = parse_tle_text(resp.text)
                all_tles.extend(records)
                rejected += bad
                logger.info("CelesTrak [%s]: %d satellites, %d rejected", group, len(records), bad)
            else:
                kind = ErrorKind.RATE_LIMITED if resp.status_code in (403, 429) else ErrorKind.HTTP_ERROR
                failure = (kind, f"CelesTrak {group} HTTP {resp.status_code}")
                logger.warning("CelesTrak %s returned %d", group, resp.status_code)
    except httpx.TimeoutException as e:
        failure = (ErrorKind.TIMEOUT, f"{type(e).__name__}: {e}")
    except Exception as e:
        failure = (ErrorKind.FETCH_ERROR, f"{type(e).__name__}: {e}")

    # Only replace the cache on success. A failed fetch used to clear it and extend
    # with an empty list, so one 403 turned a good cache into zero satellites.
    if all_tles:
        _tle_cache.clear()
        _tle_cache.extend(all_tles)
        _save_cache(all_tles, now)
        tracker.succeeded(now, count=len(all_tles), source="celestrak",
                          source_epoch=_newest_epoch(all_tles))
        logger.info("Total TLEs cached: %d", len(_tle_cache))
        return ErrorKind.OK
    if failure is None:
        # A 200 with no usable record is a malformed answer, not an empty sky.
        failure = (ErrorKind.PARSE_ERROR, f"no valid element set in the reply ({rejected} rejected)")
    tracker.failed(now, *failure)
    logger.warning("CelesTrak fetch failed (%s) — keeping %d cached satellites",
                   failure[0].value, len(_tle_cache))
    return failure[0]


async def start_tle_fetcher(sleep=asyncio.sleep, clock=time.time):
    """Background task that fetches TLE data from CelesTrak."""
    async with httpx.AsyncClient(
        timeout=60,
        headers={"User-Agent": "ConflictMonitor/1.0"},
    ) as client:
        # Start from disk. A fresh cache means this process does not touch CelesTrak at all.
        age = _load_cache(clock())
        if age < REFRESH_INTERVAL:
            await sleep(REFRESH_INTERVAL - age)

        while True:
            outcome = await fetch_once(client, clock())
            await sleep(BACKOFF_ON_BLOCK if outcome is ErrorKind.RATE_LIMITED else REFRESH_INTERVAL)


def get_tles() -> list[dict]:
    return _tle_cache


def get_tles_envelope() -> dict:
    """/tracking/tle: the element sets with the feed's state beside it. Each
    record carries `epoch_utc`; the envelope's `source_epoch` is the newest."""
    return feeds.envelope("satellites", _tle_cache, time.time())
