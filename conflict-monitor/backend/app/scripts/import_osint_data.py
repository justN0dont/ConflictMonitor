#!/usr/bin/env python3
"""
One-time importer: danielrosehill/Iran-Israel-War-2026-OSINT-Data → conflict_monitor DB

Fetches confirmed wave data (27 waves, Feb 28–Mar 7 2026) and inserts as Events.
Safe to re-run: uses source_url as dedup key (skips already-imported waves).

Run inside backend container:
    docker compose exec backend python /app/app/scripts/import_osint_data.py
"""

import asyncio
import json
import logging
import os
import urllib.request
from datetime import datetime, timezone

from geoalchemy2.shape import from_shape
from shapely.geometry import Point
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

# ── Dataset URL ──────────────────────────────────────────────────────────────
WAVES_URL = (
    "https://raw.githubusercontent.com/danielrosehill/"
    "Iran-Israel-War-2026-OSINT-Data/main/data/tp4-2026/waves.json"
)
DATASET_SOURCE = "danielrosehill-osint"
CHANNEL_NAME = "danielrosehill-osint"
SOURCE_RELIABILITY = 5  # Confirmed, geolocated, post-event OSINT compilation

# ── DB connection ─────────────────────────────────────────────────────────────
DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@db:5432/conflict_monitor",
)


def _compute_severity(wave: dict) -> int:
    """Map wave metadata to 1-10 severity scale."""
    base = 6  # All attacks are significant military operations
    escalation = wave.get("escalation") or {}
    impact = wave.get("impact") or {}

    bonus = 0
    if escalation.get("new_country_targeted"):
        bonus += 1
    if escalation.get("new_weapon_first_use"):
        bonus += 1

    fatalities = impact.get("fatalities") or 0
    if fatalities > 5:
        bonus += 1
    if fatalities > 20:
        bonus += 1

    return min(10, base + bonus)


def _build_summary(wave: dict) -> str:
    """Construct a concise summary from wave fields."""
    desc = wave.get("description") or ""
    weapons = (wave.get("weapons") or {}).get("payload") or ""
    targets_str = (wave.get("targets") or {}).get("targets") or ""
    impact = wave.get("impact") or {}
    fatalities = impact.get("fatalities") or 0
    injuries = impact.get("injuries") or 0

    parts = []
    if desc:
        parts.append(desc)
    if weapons:
        parts.append(f"Weapons: {weapons}.")
    if targets_str:
        parts.append(f"Targets: {targets_str}.")
    if fatalities or injuries:
        parts.append(f"Casualties: {fatalities} killed, {injuries} injured.")

    return " ".join(parts)[:2000]


def _build_location_name(wave: dict) -> str:
    """Best-effort location name from targets data."""
    targets = wave.get("targets") or {}
    targets_str = targets.get("targets") or ""
    # Take first location mentioned (before first comma)
    if targets_str:
        first = targets_str.split(",")[0].strip()
        return first[:100]
    return "Middle East"


def _build_source_url(wave: dict) -> str:
    wave_num = wave.get("wave_number") or 0
    return (
        f"https://github.com/danielrosehill/Iran-Israel-War-2026-OSINT-Data"
        f"/blob/main/data/tp4-2026/waves.json#wave-{wave_num}"
    )


async def import_waves(session: AsyncSession, waves: list[dict]) -> tuple[int, int]:
    """Insert waves as Events. Returns (imported, skipped)."""
    imported = 0
    skipped = 0

    for wave in waves:
        wave_num = wave.get("wave_number") or 0
        source_url = _build_source_url(wave)

        # Dedup: skip if this wave URL already exists
        existing = await session.execute(
            text("SELECT id FROM events WHERE source_url = :url LIMIT 1"),
            {"url": source_url},
        )
        if existing.scalar_one_or_none() is not None:
            log.info("Wave %d already imported — skipping", wave_num)
            skipped += 1
            continue

        # ── Coordinates ───────────────────────────────────────────────────────
        target_coords = (wave.get("targets") or {}).get("target_coordinates") or {}
        lat = target_coords.get("lat")
        lon = target_coords.get("lon")

        if lat is None or lon is None:
            log.warning("Wave %d has no target coordinates — skipping", wave_num)
            skipped += 1
            continue

        # ── Timestamp ─────────────────────────────────────────────────────────
        timing = wave.get("timing") or {}
        ts_str = timing.get("announced_utc") or timing.get("probable_launch_time")
        if not ts_str:
            log.warning("Wave %d has no timestamp — skipping", wave_num)
            skipped += 1
            continue

        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        except ValueError:
            log.warning("Wave %d bad timestamp '%s' — skipping", wave_num, ts_str)
            skipped += 1
            continue

        # ── Build fields ──────────────────────────────────────────────────────
        summary = _build_summary(wave)
        raw_text = json.dumps(wave, ensure_ascii=False)[:5000]
        severity = _compute_severity(wave)
        location_name = _build_location_name(wave)
        geometry = from_shape(Point(lon, lat), srid=4326)

        weapons_info = (wave.get("weapons") or {}).get("payload") or ""
        wave_name_en = wave.get("wave_codename_english") or f"Wave {wave_num}"
        wave_name_fa = wave.get("wave_codename_farsi") or ""

        # Insert via raw SQL to avoid needing the full ORM model import path
        await session.execute(
            text("""
                INSERT INTO events (
                    source, channel_name, raw_text, summary, event_type,
                    severity, lat, lon, geometry, timestamp, created_at,
                    report_count, reporting_channels, source_reliability,
                    location_name, source_url
                ) VALUES (
                    :source, :channel_name, :raw_text, :summary, :event_type,
                    :severity, :lat, :lon, ST_GeomFromEWKB(:geometry), :timestamp,
                    NOW(), 1, :reporting_channels, :source_reliability,
                    :location_name, :source_url
                )
            """),
            {
                "source": DATASET_SOURCE,
                "channel_name": CHANNEL_NAME,
                "raw_text": raw_text,
                "summary": summary,
                "event_type": "military",
                "severity": severity,
                "lat": lat,
                "lon": lon,
                "geometry": geometry.desc,
                "timestamp": ts,
                "reporting_channels": CHANNEL_NAME,
                "source_reliability": SOURCE_RELIABILITY,
                "location_name": location_name,
                "source_url": source_url,
            },
        )
        await session.commit()
        imported += 1
        log.info(
            "Wave %02d imported | %s | sev=%d | lat=%.2f lon=%.2f | %s",
            wave_num, ts.strftime("%Y-%m-%d %H:%M"), severity, lat, lon,
            wave_name_en or wave_name_fa,
        )

    return imported, skipped


async def main():
    log.info("Fetching dataset from GitHub...")
    try:
        with urllib.request.urlopen(WAVES_URL, timeout=30) as resp:
            raw = resp.read()
        data = json.loads(raw)
    except Exception as e:
        log.error("Failed to fetch dataset: %s", e)
        return

    # Dataset may be wrapped in metadata or be a raw list
    if isinstance(data, list):
        waves = data
    elif isinstance(data, dict):
        # Find the list of waves — could be under 'waves', 'data', or be the values
        waves = data.get("waves") or data.get("data") or []
        if not waves:
            # Try finding any list value with wave_number fields
            for v in data.values():
                if isinstance(v, list) and v and isinstance(v[0], dict) and "wave_number" in v[0]:
                    waves = v
                    break
    else:
        log.error("Unexpected data format: %s", type(data))
        return

    if not waves:
        log.error("No waves found in dataset. Keys: %s", list(data.keys()) if isinstance(data, dict) else "N/A")
        return

    log.info("Found %d waves to process", len(waves))

    engine = create_async_engine(DATABASE_URL, echo=False)
    async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with async_session() as session:
        imported, skipped = await import_waves(session, waves)

    await engine.dispose()
    log.info("=" * 50)
    log.info("IMPORT COMPLETE: %d imported, %d skipped", imported, skipped)
    log.info("=" * 50)


if __name__ == "__main__":
    asyncio.run(main())
