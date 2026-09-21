import asyncio
import json
import logging
import urllib.request
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from geoalchemy2.shape import from_shape
from shapely.geometry import Point
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import async_session as make_session, get_session
from app.models import Event, EventReport
from app.schemas import EventRead
from app.services.classifier import classify_message
from app.services.geocoder import geocode

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/events", tags=["events"])

# The unknown-coordinate sentinel constants are gone. "Unlocated" is now a
# single fact in one place: geometry IS NULL. Float-equality against the old
# sentinel latitude also miscounted any genuine event sitting at that
# latitude, so this is a correctness fix as well as a cleanup.


@router.get("", response_model=list[EventRead])
async def list_events(
    limit: int = Query(50, ge=1, le=500),
    event_type: str | None = Query(None),
    after: datetime | None = Query(None),
    before: datetime | None = Query(None),
    min_reliability: int | None = Query(None, ge=1, le=5),
    session: AsyncSession = Depends(get_session),
):
    stmt = select(Event).order_by(Event.timestamp.desc()).limit(limit)
    if event_type:
        stmt = stmt.where(Event.event_type == event_type)
    if after:
        stmt = stmt.where(Event.timestamp >= after)
    if before:
        stmt = stmt.where(Event.timestamp <= before)
    if min_reliability:
        stmt = stmt.where(Event.source_reliability >= min_reliability)
    result = await session.execute(stmt)
    return result.scalars().all()


@router.get("/time-range")
async def event_time_range(session: AsyncSession = Depends(get_session)):
    result = await session.execute(
        select(func.min(Event.timestamp), func.max(Event.timestamp))
    )
    row = result.one_or_none()
    if row and row[0] and row[1]:
        return {"earliest": row[0].isoformat(), "latest": row[1].isoformat()}
    return {"earliest": None, "latest": None}


@router.get("/{event_id}", response_model=EventRead)
async def get_event(event_id: int, session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(Event).where(Event.id == event_id))
    event = result.scalar_one_or_none()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    return event


# ──────────────────────────────────────────────────────────────────────────────
# ADMIN: Retroactively fix events that have null or Indian Ocean coordinates
# ──────────────────────────────────────────────────────────────────────────────

async def _fix_null_coords_task():
    """Background: re-geocode every event that has no location."""
    from app.db import async_session as make_session

    async with make_session() as session:
        stmt = select(Event).where(Event.geometry.is_(None)).order_by(Event.id)
        result = await session.execute(stmt)
        events = result.scalars().all()

    logger.info("fix-null-coords: %d events to retry", len(events))
    fixed = 0

    for ev in events:
        location_name = (getattr(ev, "location_name", "") or "").strip()
        new_severity = ev.severity
        new_killed = ev.killed_reported
        new_summary = ev.summary
        reclassified_model = None

        # If location_name is blank/unknown, try re-classifying from raw text
        if not location_name or location_name.lower() in ("unknown", "n/a", ""):
            if ev.raw_text and ev.raw_text.strip():
                try:
                    classified = await classify_message(ev.raw_text)
                    if classified.get("extraction_status") != "ok":
                        # A fallback is a regex scrape of the raw text, not a
                        # classification. Writing its summary onto the row while
                        # the row keeps its old status files output no model
                        # produced as though it had been extracted.
                        logger.debug(
                            "Re-classify for #%d returned %s — leaving the row alone",
                            ev.id, classified.get("extraction_status"),
                        )
                        continue
                    location_name = (classified.get("location_name") or "").strip()
                    # A re-classification that produced no severity must not
                    # erase the one already on the row.
                    reclassified_severity = classified.get("severity")
                    if reclassified_severity is not None:
                        new_severity = reclassified_severity
                    # Written unconditionally, including None: a successful
                    # re-classification saying this text states no death toll
                    # IS the finding, so it may replace an older number.
                    # (Severity differs — None there means the reply carried
                    # no severity at all, which is a parse failure, not a
                    # measurement of zero.)
                    new_killed = classified.get("killed_reported")
                    new_summary = classified.get("summary", ev.summary)
                    reclassified_model = classified.get("extraction_model")
                except Exception as e:
                    logger.warning("Re-classify failed for event #%d: %s", ev.id, e)
                    continue

        if not location_name or location_name.lower() in ("unknown", "n/a", ""):
            continue

        geo = await geocode(location_name)
        if not geo:
            logger.debug("Still no coords for event #%d (loc='%s')", ev.id, location_name)
            # Nominatim rate-limits — still sleep before next request
            await asyncio.sleep(1.2)
            continue

        lat, lon = geo.lat, geo.lon
        async with make_session() as session:
            result = await session.execute(select(Event).where(Event.id == ev.id))
            db_ev = result.scalar_one_or_none()
            if db_ev:
                db_ev.lat = lat
                db_ev.lon = lon
                db_ev.geometry = from_shape(Point(lon, lat), srid=4326)
                db_ev.location_name = location_name
                db_ev.severity = new_severity
                db_ev.killed_reported = new_killed
                db_ev.summary = new_summary
                if reclassified_model:
                    # The row now carries this model's output, so it carries its
                    # provenance too — otherwise a healed row still reads as the
                    # failure it used to be.
                    db_ev.extraction_status = "ok"
                    db_ev.extraction_model = reclassified_model
                db_ev.is_geolocated = True
                db_ev.geo_precision = geo.precision
                db_ev.geo_uncertainty_m = geo.uncertainty_m
                db_ev.geo_method = geo.method
                await session.commit()
                fixed += 1
                logger.info(
                    "Fixed event #%d: loc='%s' -> (%.2f, %.2f) %s ±%dm",
                    ev.id, location_name, lat, lon, geo.precision, geo.uncertainty_m,
                )

        await asyncio.sleep(1.2)  # Nominatim: max 1 req/sec

    logger.info("fix-null-coords complete: fixed %d / %d events", fixed, len(events))


@router.post("/admin/fix-null-coords")
async def fix_null_coords(background_tasks: BackgroundTasks):
    """Re-geocode all events with null or unknown coordinates (runs in background)."""
    background_tasks.add_task(_fix_null_coords_task)
    return {
        "status": "started",
        "message": "Retroactive geocoding running in background. Check logs for progress.",
    }


@router.post("/admin/reclassify-locations")
async def reclassify_locations(background_tasks: BackgroundTasks):
    """Re-run classifier on all events whose location_name is a country or vague term.
    Extracts sub-city precision (facility/district level) using the improved prompt.
    Runs in background — check logs for 'reclassify-locations' progress."""
    background_tasks.add_task(_reclassify_vague_locations_task)
    return {
        "status": "started",
        "message": "Reclassifying vague location_names (country-level → facility/district). Check logs.",
    }


# Country names and other vague location terms that should be reclassified
_VAGUE_LOCATIONS = {
    "iran", "israel", "lebanon", "syria", "iraq", "yemen", "saudi arabia",
    "jordan", "kuwait", "qatar", "uae", "bahrain", "oman", "egypt", "turkey",
    "united states", "russia", "china", "unknown", "n/a", "", "middle east",
    "region", "area", "various",
}

async def _reclassify_vague_locations_task():
    """Re-classify events where location_name is only a country name or vague."""
    from app.db import async_session as make_session

    async with make_session() as session:
        result = await session.execute(select(Event).order_by(Event.id))
        all_events = result.scalars().all()

    # Filter to events with only country-level or vague location names
    to_fix = [
        ev for ev in all_events
        if (getattr(ev, "location_name", "") or "").strip().lower() in _VAGUE_LOCATIONS
        and ev.raw_text and ev.raw_text.strip()
    ]

    logger.info("reclassify-locations: %d events with vague locations", len(to_fix))
    improved = 0

    for ev in to_fix:
        try:
            classified = await classify_message(ev.raw_text)
            if classified.get("extraction_status") != "ok":
                # Same rule as fix-null-coords: only a real classification may
                # overwrite a row, and only while stamping its own provenance.
                continue
            new_loc = (classified.get("location_name") or "").strip()

            # Only update if we got something more specific
            if not new_loc or new_loc.lower() in _VAGUE_LOCATIONS:
                continue

            geo = await geocode(new_loc)
            if not geo:
                await asyncio.sleep(1.2)
                continue

            lat, lon = geo.lat, geo.lon
            async with make_session() as session:
                db_result = await session.execute(select(Event).where(Event.id == ev.id))
                db_ev = db_result.scalar_one_or_none()
                if db_ev:
                    db_ev.location_name = new_loc
                    db_ev.lat = lat
                    db_ev.lon = lon
                    db_ev.geometry = from_shape(Point(lon, lat), srid=4326)
                    db_ev.is_geolocated = True
                    db_ev.geo_precision = geo.precision
                    db_ev.geo_uncertainty_m = geo.uncertainty_m
                    db_ev.geo_method = geo.method
                    if classified.get("severity"):
                        db_ev.severity = classified["severity"]
                    db_ev.killed_reported = classified.get("killed_reported")
                    if classified.get("summary"):
                        db_ev.summary = classified["summary"]
                    db_ev.extraction_status = "ok"
                    db_ev.extraction_model = classified.get("extraction_model")
                    await session.commit()
                    improved += 1
                    logger.info(
                        "Reclassified event #%d: '%s' → '%s' (%.4f, %.4f)",
                        ev.id, ev.location_name, new_loc, lat, lon,
                    )
        except Exception as e:
            logger.error("Reclassify error for event #%d: %s", ev.id, e)

        await asyncio.sleep(0.5)  # small pause between classifier calls

    logger.info("reclassify-locations complete: improved %d / %d events", improved, len(to_fix))


@router.post("/admin/backfill")
async def trigger_backfill(background_tasks: BackgroundTasks):
    """Trigger a full historical backfill from conflict start (2026-02-28) for all
    monitored Telegram channels. Runs in background — check logs for progress.
    Safe to call repeatedly; checkpoint logic prevents double-processing messages.
    """
    from app.services.telegram import trigger_backfill as _backfill
    background_tasks.add_task(_backfill)
    return {
        "status": "started",
        "message": (
            "Historical backfill running in background. "
            "Fetches all Telegram messages since 2026-02-28 for every monitored channel. "
            "Check backend logs for per-channel progress."
        ),
    }


@router.delete("/admin/purge-old")
async def purge_old_events(
    before: datetime | None = Query(None, description="ISO datetime cutoff — defaults to 2026-02-28"),
    session: AsyncSession = Depends(get_session),
):
    """Delete all events with timestamp before `before` (default: 2026-02-28T00:00:00Z).
    Useful for clearing out stale backfill data before the conflict started.
    """
    cutoff = before or datetime(2026, 2, 28, tzinfo=timezone.utc)
    result = await session.execute(
        delete(Event).where(Event.timestamp < cutoff)
    )
    await session.commit()
    return {
        "deleted": result.rowcount,
        "cutoff": cutoff.isoformat(),
        "message": f"Deleted {result.rowcount} events before {cutoff.date()}",
    }


@router.delete("/admin/dedup")
async def dedup_events(session: AsyncSession = Depends(get_session)):
    """Remove exact duplicate events — keeps the lowest ID per (source, raw_text) pair.
    Duplicates occur when the same Telegram/RSS message is ingested twice."""
    # Find duplicates: same source + raw_text, keep min(id)
    dedup_sql = text("""
        DELETE FROM events
        WHERE id NOT IN (
            SELECT MIN(id)
            FROM events
            GROUP BY source, raw_text
        )
    """)
    result = await session.execute(dedup_sql)
    await session.commit()
    return {
        "deleted": result.rowcount,
        "message": f"Removed {result.rowcount} duplicate events",
    }


@router.get("/admin/geo-stats")
async def geo_stats(session: AsyncSession = Depends(get_session)):
    """Show counts of geolocated vs unknown-location events.

    One definition of located, used by both halves: geometry IS NOT NULL.
    """
    total = (await session.execute(select(func.count(Event.id)))).scalar_one()
    geolocated = (await session.execute(
        select(func.count(Event.id)).where(Event.geometry.isnot(None))
    )).scalar_one()
    unknown = (await session.execute(
        select(func.count(Event.id)).where(Event.geometry.is_(None))
    )).scalar_one()
    return {
        "total": total,
        "geolocated": geolocated,
        "unknown_location": unknown,
        "geo_rate": f"{(geolocated / total * 100):.1f}%" if total else "0%",
    }


@router.get("/stats/extraction")
async def extraction_stats(session: AsyncSession = Depends(get_session)):
    """Counts by extraction_status and is_geolocated. NULL = row written before
    these columns existed, so it is distinguishable from a tagged row."""
    total = (await session.execute(select(func.count(Event.id)))).scalar_one()

    status_rows = await session.execute(
        select(Event.extraction_status, func.count(Event.id)).group_by(Event.extraction_status)
    )
    geo_rows = await session.execute(
        select(Event.is_geolocated, func.count(Event.id)).group_by(Event.is_geolocated)
    )
    # The NULL bucket fuses the pre-existing archive with writers that do not tag
    # (demo.py, the OSINT import, dedup merges) — split it so it stays readable.
    untagged_rows = await session.execute(
        select(Event.source, func.count(Event.id))
        .where(Event.extraction_status.is_(None))
        .group_by(Event.source)
    )

    return {
        "total": total,
        "by_extraction_status": {
            (status if status is not None else "null"): count
            for status, count in status_rows.all()
        },
        "by_is_geolocated": {
            (str(flag).lower() if flag is not None else "null"): count
            for flag, count in geo_rows.all()
        },
        "untagged_by_source": {
            (source if source else "null"): count
            for source, count in untagged_rows.all()
        },
    }


# ──────────────────────────────────────────────────────────────────────────────
# ADMIN: One-time import of danielrosehill OSINT confirmed wave dataset
# Source: https://github.com/danielrosehill/Iran-Israel-War-2026-OSINT-Data
# ──────────────────────────────────────────────────────────────────────────────

_OSINT_WAVES_URL = (
    "https://raw.githubusercontent.com/danielrosehill/"
    "Iran-Israel-War-2026-OSINT-Data/main/data/tp4-2026/waves.json"
)
_OSINT_SOURCE = "danielrosehill-osint"
_OSINT_CHANNEL = "danielrosehill-osint"
_OSINT_RELIABILITY = 5


def _wave_severity(wave: dict) -> int:
    base = 6
    esc = wave.get("escalation") or {}
    imp = wave.get("impact") or {}
    bonus = 0
    if esc.get("new_country_targeted"):
        bonus += 1
    if esc.get("new_weapon_first_use"):
        bonus += 1
    fatalities = imp.get("fatalities") or 0
    if fatalities > 5:
        bonus += 1
    if fatalities > 20:
        bonus += 1
    return min(10, base + bonus)


def _wave_summary(wave: dict) -> str:
    desc = wave.get("description") or ""
    weapons = (wave.get("weapons") or {}).get("payload") or ""
    targets_str = (wave.get("targets") or {}).get("targets") or ""
    imp = wave.get("impact") or {}
    fatalities = imp.get("fatalities") or 0
    injuries = imp.get("injuries") or 0
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


def _wave_location(wave: dict) -> str:
    targets_str = (wave.get("targets") or {}).get("targets") or ""
    if targets_str:
        return targets_str.split(",")[0].strip()[:100]
    return "Middle East"


# Fallback coordinates when target_coordinates is null.
# Priority: Israeli city flags → US base countries → landing countries → Iran default.
_COUNTRY_COORDS: dict[str, tuple[float, float]] = {
    "IL": (32.0853, 34.7818),   # Tel Aviv
    "BH": (26.2235, 50.5876),   # Manama / Mina Salman
    "AE": (24.4539, 54.3773),   # Abu Dhabi / Al Dhafra
    "QA": (25.1188, 51.3246),   # Doha / Al Udeid
    "SA": (26.3055, 50.1083),   # Ras Tanura / Dhahran
    "IQ": (33.3128, 44.3615),   # Baghdad
    "JO": (31.9522, 35.9310),   # Amman
    "OM": (23.5880, 58.3829),   # Muscat / Duqm
    "KW": (29.3759, 47.9774),   # Kuwait City
    "SY": (33.5102, 36.2913),   # Damascus
    "LB": (33.8938, 35.5018),   # Beirut
    "YE": (15.3694, 44.1910),   # Sana'a
    "IR": (35.6892, 51.3890),   # Tehran
    "PK": (33.7290, 73.0931),   # Islamabad
}
_ISRAELI_LOC_COORDS: dict[str, tuple[float, float]] = {
    "targeted_tel_aviv":         (32.0853, 34.7818),
    "targeted_jerusalem":        (31.7683, 35.2137),
    "targeted_haifa":            (32.8191, 34.9983),
    "targeted_negev_beersheba":  (31.2518, 34.7913),
    "targeted_northern_periphery":(33.0735, 35.1215),
    "targeted_eilat":            (29.5577, 34.9519),
}


def _wave_coords(wave: dict) -> tuple[float | None, float | None]:
    """Return (lat, lon) for a wave, with multi-level fallback."""
    targets = wave.get("targets") or {}

    # 1. Explicit coordinates
    tc = targets.get("target_coordinates") or {}
    lat, lon = tc.get("lat"), tc.get("lon")
    if lat is not None and lon is not None:
        return float(lat), float(lon)

    # 2. Israeli city flags (most specific)
    il_locs = targets.get("israeli_locations") or {}
    for flag, coords in _ISRAELI_LOC_COORDS.items():
        if il_locs.get(flag):
            return coords

    # 3. US base country codes
    for base in targets.get("us_bases") or []:
        cc = (base.get("country_code") or "").upper()
        if cc in _COUNTRY_COORDS:
            return _COUNTRY_COORDS[cc]

    # 4. First landing country
    for cc in targets.get("landings_countries") or []:
        if cc and cc.upper() in _COUNTRY_COORDS:
            return _COUNTRY_COORDS[cc.upper()]

    # 5. If Israel targeted, default Tel Aviv
    if targets.get("israel_targeted"):
        return _COUNTRY_COORDS["IL"]

    return None, None


async def _import_osint_waves_task():
    """Background: fetch and import confirmed OSINT wave dataset into DB."""
    logger.info("OSINT import: fetching %s", _OSINT_WAVES_URL)
    try:
        with urllib.request.urlopen(_OSINT_WAVES_URL, timeout=30) as resp:
            raw = resp.read()
        data = json.loads(raw)
    except Exception as e:
        logger.error("OSINT import: failed to fetch dataset: %s", e)
        return

    # Dataset may be list or dict with waves nested somewhere
    if isinstance(data, list):
        waves = data
    elif isinstance(data, dict):
        waves = data.get("waves") or data.get("data") or []
        if not waves:
            for v in data.values():
                if isinstance(v, list) and v and isinstance(v[0], dict) and "wave_number" in v[0]:
                    waves = v
                    break
    else:
        logger.error("OSINT import: unexpected data type %s", type(data))
        return

    if not waves:
        logger.error("OSINT import: no waves found. Keys: %s", list(data.keys()) if isinstance(data, dict) else "N/A")
        return

    logger.info("OSINT import: processing %d waves", len(waves))
    imported = 0
    skipped = 0

    for wave in waves:
        wave_num = wave.get("wave_number") or 0
        source_url = (
            f"https://github.com/danielrosehill/Iran-Israel-War-2026-OSINT-Data"
            f"/blob/main/data/tp4-2026/waves.json#wave-{wave_num}"
        )

        async with make_session() as session:
            # Dedup by source_url
            existing = await session.execute(
                select(Event.id).where(Event.source_url == source_url).limit(1)
            )
            if existing.scalar_one_or_none() is not None:
                logger.debug("Wave %d already imported — skipping", wave_num)
                skipped += 1
                continue

            # Coordinates — try explicit coords first, then multi-level fallback
            lat, lon = _wave_coords(wave)
            if lat is None or lon is None:
                logger.warning("Wave %d: no coordinates even after fallback — skipping", wave_num)
                skipped += 1
                continue
            logger.debug("Wave %d coords: (%.4f, %.4f)", wave_num, lat, lon)

            # Timestamp
            timing = wave.get("timing") or {}
            ts_str = timing.get("announced_utc") or timing.get("probable_launch_time")
            if not ts_str:
                logger.warning("Wave %d: no timestamp — skipping", wave_num)
                skipped += 1
                continue
            try:
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            except ValueError:
                logger.warning("Wave %d: bad timestamp '%s' — skipping", wave_num, ts_str)
                skipped += 1
                continue

            summary = _wave_summary(wave)
            raw_text = json.dumps(wave, ensure_ascii=False)[:5000]
            severity = _wave_severity(wave)
            location_name = _wave_location(wave)
            geometry = from_shape(Point(lon, lat), srid=4326)

            db_event = Event(
                source=_OSINT_SOURCE,
                channel_name=_OSINT_CHANNEL,
                raw_text=raw_text,
                summary=summary,
                event_type="military",
                severity=severity,
                lat=lat,
                lon=lon,
                geometry=geometry,
                timestamp=ts,
                report_count=1,
                reporting_channels=_OSINT_CHANNEL,
                source_reliability=_OSINT_RELIABILITY,
                location_name=location_name,
                source_url=source_url,
            )
            session.add(db_event)
            # This is a third writer of events, so it writes its report row too
            # — not because anything here merges, but because report_count
            # already says 1 and a row with no report under it would read as a
            # report whose text we cannot show. (The startup backfill would
            # pick it up on the next boot; this keeps the difference at 0 from
            # the moment of import.)
            await session.flush()
            session.add(EventReport(
                event_id=db_event.id,
                source=_OSINT_SOURCE,
                channel=_OSINT_CHANNEL,
                raw_text=raw_text,
                summary=summary,
                source_url=source_url,
                reported_at=ts,
            ))
            await session.commit()
            imported += 1
            wave_name = wave.get("wave_codename_english") or f"Wave {wave_num}"
            logger.info(
                "OSINT wave %02d imported | %s | sev=%d | (%.2f, %.2f) | %s",
                wave_num, ts.strftime("%Y-%m-%d %H:%M"), severity, lat, lon, wave_name,
            )

    logger.info(
        "OSINT import complete: %d imported, %d skipped (already existed)",
        imported, skipped,
    )


@router.post("/admin/import-osint-dataset")
async def import_osint_dataset(background_tasks: BackgroundTasks):
    """
    One-time import of the danielrosehill confirmed OSINT wave dataset
    (27 geolocated missile attack waves, Feb 28–Mar 7 2026).
    Safe to call repeatedly — skips already-imported waves.
    Runs in background; check logs for progress.
    """
    background_tasks.add_task(_import_osint_waves_task)
    return {
        "status": "started",
        "message": (
            "Importing confirmed OSINT waves from danielrosehill dataset. "
            "Geolocated missile attack waves (Feb 28 – Mar 7 2026). "
            "Check backend logs for progress. Safe to call again if interrupted."
        ),
        "source": _OSINT_WAVES_URL,
    }
