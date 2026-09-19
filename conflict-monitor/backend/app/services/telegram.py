import logging

from geoalchemy2.shape import from_shape
from sqlalchemy import select
from shapely.geometry import Point
from telethon import TelegramClient, events

from app.config import settings
from app.db import async_session
from app.models import Event
from app.schemas import EventRead, EventWS
from app.seed_channels import DEFAULT_CHANNELS, get_reliability
from app.services.broadcaster import broadcaster
from app.services.classifier import classify_message
from app.services.dedup import check_duplicate, merge_duplicate
from app.services.geocoder import geocode

logger = logging.getLogger(__name__)


def _get_channels() -> list[str]:
    if settings.telegram_channels:
        return [c.strip() for c in settings.telegram_channels.split(",") if c.strip()]
    return DEFAULT_CHANNELS


async def start_telegram_listener():
    channels = _get_channels()
    logger.info("Monitoring %d Telegram channels: %s", len(channels), channels)

    client = TelegramClient(
        "sessions/conflict_monitor",
        settings.telegram_api_id,
        settings.telegram_api_hash,
    )
    await client.start(phone=settings.telegram_phone)

    # Resolve channel entities so Telethon can match incoming messages
    resolved = []
    for ch in channels:
        try:
            entity = await client.get_entity(ch)
            resolved.append(entity)
            logger.info("Resolved channel: %s (id=%s)", ch, entity.id)
        except Exception as e:
            logger.error("Failed to resolve channel '%s': %s", ch, e)

    if not resolved:
        logger.error("No channels resolved — listener has nothing to monitor")
        return

    @client.on(events.NewMessage(chats=resolved))
    async def handler(event):
        try:
            raw_text = event.message.text
            if not raw_text:
                return

            channel_name = ""
            if hasattr(event.chat, "username") and event.chat.username:
                channel_name = event.chat.username
            elif hasattr(event.chat, "title"):
                channel_name = event.chat.title

            logger.info("New message from %s: %s", channel_name, raw_text[:80])

            # Stage 1: Classify with Claude (no lat/lon — just event_type, severity, location_name, summary)
            result = await classify_message(raw_text)

            # Stage 2: Geocode the location name via Nominatim / fallback table
            location_name = result.get("location_name", "Unknown")
            coords = await geocode(location_name)
            lat = coords[0] if coords else None
            lon = coords[1] if coords else None

            geometry = None
            if lat is not None and lon is not None:
                geometry = from_shape(Point(lon, lat), srid=4326)

            async with async_session() as session:
                # Check for duplicate
                existing = await check_duplicate(
                    session, result.get("summary", ""), result.get("event_type", "military"),
                    lat, lon, event.message.date,
                )
                if existing:
                    await merge_duplicate(session, existing, channel_name, result.get("severity", 5), lat, lon)
                    ws_payload = EventWS(type="new_event", event=EventRead.model_validate(existing))
                    await broadcaster.broadcast(ws_payload.model_dump_json())
                    return

                db_event = Event(
                    source="telegram",
                    channel_name=channel_name,
                    raw_text=raw_text,
                    summary=result.get("summary", raw_text[:200]),
                    event_type=result.get("event_type", "military"),
                    severity=result.get("severity", 5),
                    lat=lat,
                    lon=lon,
                    geometry=geometry,
                    timestamp=event.message.date,
                    source_reliability=get_reliability(channel_name),
                )
                session.add(db_event)
                await session.commit()
                await session.refresh(db_event)

                ws_payload = EventWS(
                    type="new_event",
                    event=EventRead.model_validate(db_event),
                )
                await broadcaster.broadcast(ws_payload.model_dump_json())
                logger.info("Event #%d broadcast (severity=%d, loc=%s → %s)", db_event.id, db_event.severity, location_name, coords)
        except Exception as e:
            logger.exception("Error processing message: %s", e)

    # Backfill recent messages from each channel
    logger.info("Backfilling recent messages...")
    for entity in resolved:
        try:
            channel_name = getattr(entity, "username", "") or getattr(entity, "title", "")
            async for message in client.iter_messages(entity, limit=20):
                raw_text = message.text
                if not raw_text:
                    continue

                # Already ingested on a previous run — skip before spending an API call
                async with async_session() as session:
                    already = await session.scalar(
                        select(Event.id)
                        .where(Event.channel_name == channel_name, Event.raw_text == raw_text)
                        .limit(1)
                    )
                if already is not None:
                    continue

                logger.info("Backfill from %s: %s", channel_name, raw_text[:80])
                result = await classify_message(raw_text)

                location_name = result.get("location_name", "Unknown")
                coords = await geocode(location_name)
                lat = coords[0] if coords else None
                lon = coords[1] if coords else None

                geometry = None
                if lat is not None and lon is not None:
                    geometry = from_shape(Point(lon, lat), srid=4326)

                async with async_session() as session:
                    existing = await check_duplicate(
                        session, result.get("summary", ""), result.get("event_type", "military"),
                        lat, lon, message.date,
                    )
                    if existing:
                        await merge_duplicate(session, existing, channel_name, result.get("severity", 5), lat, lon)
                        continue

                    db_event = Event(
                        source="telegram",
                        channel_name=channel_name,
                        raw_text=raw_text,
                        summary=result.get("summary", raw_text[:200]),
                        event_type=result.get("event_type", "military"),
                        severity=result.get("severity", 5),
                        lat=lat,
                        lon=lon,
                        geometry=geometry,
                        timestamp=message.date,
                        source_reliability=get_reliability(channel_name),
                    )
                    session.add(db_event)
                    await session.commit()
                    await session.refresh(db_event)

                    ws_payload = EventWS(
                        type="new_event",
                        event=EventRead.model_validate(db_event),
                    )
                    await broadcaster.broadcast(ws_payload.model_dump_json())
        except Exception as e:
            logger.exception("Backfill error for %s: %s", entity, e)

    logger.info("Backfill complete — event handler registered, waiting for new messages")
    await client.run_until_disconnected()
