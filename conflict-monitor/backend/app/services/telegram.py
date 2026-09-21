import logging
from datetime import datetime, timezone, timedelta

from geoalchemy2.shape import from_shape
from shapely.geometry import Point
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from telethon import TelegramClient, events
from telethon.sessions import StringSession

from app.config import settings
from app.db import async_session
from app.models import ChannelCheckpoint, Event, EventReport
from app.schemas import EventRead, EventWS
from app.seed_channels import DEFAULT_CHANNELS, get_reliability
from app.services.broadcaster import broadcaster
from app.services.classifier import classify_message
from app.services.dedup import check_duplicate, merge_duplicate
from app.services.geocoder import geocode

logger = logging.getLogger(__name__)

# ── CONSTANTS ────────────────────────────────────────────────────────────────

# There is no UNKNOWN_LAT/UNKNOWN_LON any more. An unresolvable location is
# written as NULL lat/lon/geometry with is_geolocated=False. The old Indian
# Ocean sentinel put 47.6% of the archive on a real point in open water and
# then had to be filtered back out by three separate query predicates.

MIN_SEVERITY = 3

# ── Noise phrases: messages matching any of these are dropped immediately ──────
# These are commentary, reactions, social-media meta-posts, and other non-events.
# They MUST NOT include legitimate event keywords (strikes, missiles, etc.)
NOISE_PHRASES = [
    # Social meta / channel admin
    "so how's everyone",
    "well hello there",
    "better test this",
    "not forgotten about",
    "just sitting back",
    "balancing real life",
    "bsky.social",
    "bluesky social media",
    "there is some beautiful irony",
    "follow us on",
    "subscribe to our",
    "join our channel",
    "turn on notifications",
    "share this post",
    "repost if you",
    "like and subscribe",
    "don't forget to",
    "link in bio",
    "check out our",
    "support us on",
    "patreon",
    # Pure reactions / commentary (no event content)
    "wait i thought",
    "i thought the war",
    "you had won",
    "the war was over",
    "is he fucking",
    "is he serious?",
    "is she serious?",
    "are they serious?",
    "what a surprise",
    "can you believe",
    "hot take:",
    "unpopular opinion:",
    "genuine question:",
    "genuine question,",
    "anyone else notice",
    "anyone else feel",
    "thoughts on this",
    "what do you think",
    "comment below",
    "your thoughts?",
    "let me know",
    "does anyone know",
    "asking for a friend",
    "just asking",
    "no way this is real",
    "this is insane",
    "this is crazy",
    "oh wow",
    "lol ",
    " lmao",
    " lmfao",
    " smh",
    "😂😂",
    "🤣🤣",
    # Raw URLs with no context (link dumps)
    "https://t.me/joinchat",
    "https://t.me/+",
    "t.me/joinchat",
    # Test / placeholder messages
    "this is a test",
    "testing 1 2 3",
    "hello world",
]

# Minimum word count for a message to be processed (filters very short reactions)
MIN_WORD_COUNT = 8

# Phrases that indicate a message is a reaction/commentary question with no event data
COMMENTARY_STARTERS = (
    "wait",
    "is he",
    "is she",
    "is this",
    "are they",
    "can you",
    "do you",
    "does anyone",
    "lol",
    "omg",
    "wow,",
    "wtf",
    "smh",
)


def _get_channels() -> list[str]:
    if settings.telegram_channels:
        return [c.strip() for c in settings.telegram_channels.split(",") if c.strip()]
    return DEFAULT_CHANNELS


def _get_conflict_cutoff() -> datetime:
    """Parse CONFLICT_START_DATE from settings and return as UTC datetime."""
    try:
        d = datetime.strptime(settings.conflict_start_date, "%Y-%m-%d")
        return d.replace(tzinfo=timezone.utc)
    except Exception:
        # Fallback: 30 days ago
        return datetime.now(timezone.utc) - timedelta(days=30)


def _is_noise(text: str, severity: int | None) -> bool:
    """Return True if this message should be dropped as noise/commentary.

    severity=None means it was never measured. That is not a low score, so it
    does not trip the MIN_SEVERITY gate — an otherwise-fine event is never
    dropped for a number we failed to obtain. Only the text rules apply.
    """
    stripped = text.strip()

    # Too short to contain useful intelligence
    if len(stripped) < 30:
        return True

    # Word count check — reactions tend to be very short
    words = stripped.split()
    if len(words) < MIN_WORD_COUNT:
        return True

    tl = stripped.lower()

    # Noise phrase match (case-insensitive)
    for phrase in NOISE_PHRASES:
        if phrase in tl:
            logger.debug("Noise phrase match '%s': %s", phrase, stripped[:60])
            return True

    # Commentary starter — short sentence beginning with a reaction phrase
    # Only applies if message is ≤ 25 words (short commentary)
    if len(words) <= 25:
        first_word = tl.split()[0].rstrip(".,!?") if tl.split() else ""
        first_two = " ".join(tl.split()[:2]).rstrip(".,!?")
        for starter in COMMENTARY_STARTERS:
            if first_word == starter or first_two.startswith(starter):
                logger.debug("Commentary starter '%s': %s", starter, stripped[:60])
                return True

    # Severity below threshold — only when we actually have one
    if severity is not None and severity < MIN_SEVERITY:
        return True

    return False


def _build_source_url(channel_name: str, message_id: int) -> str:
    if not channel_name or not message_id:
        return ""
    return f"https://t.me/{channel_name.lstrip('@')}/{message_id}"


# ── Checkpoint helpers ────────────────────────────────────────────────────────

async def _get_checkpoint(channel_name: str) -> int:
    """Return the last saved message_id for this channel (0 if none)."""
    async with async_session() as session:
        result = await session.execute(
            select(ChannelCheckpoint).where(ChannelCheckpoint.channel_name == channel_name)
        )
        cp = result.scalar_one_or_none()
        return cp.last_message_id if cp else 0


async def _save_checkpoint(channel_name: str, message_id: int):
    """Upsert the checkpoint for this channel."""
    async with async_session() as session:
        result = await session.execute(
            select(ChannelCheckpoint).where(ChannelCheckpoint.channel_name == channel_name)
        )
        cp = result.scalar_one_or_none()
        if cp:
            if message_id > cp.last_message_id:
                cp.last_message_id = message_id
                cp.last_processed_at = datetime.now(timezone.utc)
        else:
            cp = ChannelCheckpoint(
                channel_name=channel_name,
                last_message_id=message_id,
                last_processed_at=datetime.now(timezone.utc),
            )
            session.add(cp)
        await session.commit()


async def _message_already_saved(session, channel_name: str, message_id: int) -> bool:
    """Has this exact message already been through the pipeline?

    Two things changed here at once, because the query had two defects and one
    rewrite fixes both.

    It reads `event_reports`, not `events`. A merged message writes no events
    row at all, so its id was nowhere and the old query could not see it: on
    every restart the message came round again, was re-classified on the GPU,
    and was re-merged, incrementing report_count for a report already counted
    (`C10`). The report table has a row for every report including merged ones,
    so it can now answer the question it was always asking.

    And the key is (channel, message id), not the id alone. Telegram message
    ids are per-channel; the old query let message 4821 from one channel block
    message 4821 from another, which is a genuinely new message dropped with
    "already saved" in the log — absent written as present, the bug class this
    project exists to remove. The archive cannot show the drops directly,
    because by construction the dropped ones are the rows that are missing:
    what it shows is five channel pairs whose id ranges overlap heavily and
    hold ZERO ids in common, where treating the two id sets as independent over
    the shared window predicts on the order of 2,200. That is ~4.5% of 49,369
    Telegram messages, so ingest volume either side of this change is not
    comparable — see the Corrections log.
    """
    result = await session.execute(
        select(EventReport.id).where(
            EventReport.channel == channel_name,
            EventReport.telegram_message_id == message_id,
        ).limit(1)
    )
    return result.scalar_one_or_none() is not None


# ── Core message processor ────────────────────────────────────────────────────

async def _process_message(
    raw_text: str,
    channel_name: str,
    message_date: datetime,
    message_id: int,
):
    if not raw_text or not raw_text.strip():
        return

    # Drop messages before the conflict start date
    conflict_cutoff = _get_conflict_cutoff()
    if message_date.tzinfo is None:
        message_date = message_date.replace(tzinfo=timezone.utc)
    if message_date < conflict_cutoff:
        logger.debug(
            "Skipping pre-conflict message from %s (date=%s)", channel_name,
            message_date.strftime("%Y-%m-%d")
        )
        return

    # Dedup by exact Telegram (channel, message_id)
    async with async_session() as session:
        if await _message_already_saved(session, channel_name, message_id):
            logger.debug("Skipping already-saved msg_id=%d from %s", message_id, channel_name)
            return

    # Pre-filter using local noise rules (fast, no API call).
    # severity=None here because we have not classified yet — the text rules
    # run, the severity gate does not. (It used to pass a literal 5, which was
    # the same thing said dishonestly.)
    if _is_noise(raw_text, None):
        logger.debug("Pre-filter noise: %s", raw_text[:60])
        return

    # Classify. severity may be None: the classifier no longer invents one.
    result = await classify_message(raw_text)
    severity = result.get("severity")

    # Classifier returned [NOISE] tag or severity=1 — drop immediately
    if result.get("is_noise") or _is_noise(raw_text, severity):
        logger.debug("Skipping noise (sev=%s): %s", severity, raw_text[:60])
        return

    # Geocode
    location_name = result.get("location_name", "Unknown")
    geo = await geocode(location_name)

    if geo:
        lat, lon = geo.lat, geo.lon
        geometry = from_shape(Point(lon, lat), srid=4326)
        is_geolocated = True
    else:
        # Unresolvable. Write NULL, not a coordinate — is_geolocated carries
        # the fact and the row stays off the map instead of onto open ocean.
        lat = lon = geometry = None
        is_geolocated = False
        logger.info(
            "No coords for '%s' — stored unlocated (msg_id=%d)", location_name, message_id
        )

    source_url = _build_source_url(channel_name, message_id)

    # Built before the duplicate check, because it is a fact about THIS message
    # whichever event it turns out to belong to. It is then attached to the
    # event that already exists, or to the one created below — "link, don't
    # merge" falls out of the ordering instead of being engineered.
    report = EventReport(
        source="telegram",
        channel=channel_name,
        raw_text=raw_text,
        summary=result.get("summary", raw_text[:200]),
        source_url=source_url,
        telegram_message_id=message_id,
        killed_reported=result.get("killed_reported"),
        # Beside the count, from the same result, so a NULL count on this row
        # can be read as "the message stated none" or "the classifier never
        # produced one" — see models.py. A fallback carries no killed_reported
        # key at all, which is why the status has to travel with it.
        extraction_status=result.get("extraction_status"),
        reported_at=message_date,
    )

    # The UNIQUE index on (channel, telegram_message_id) is the database half of
    # _message_already_saved, which is check-then-act with a wide window: the
    # guard's session is closed again before classification and geocoding
    # (seconds), and the live handler, the startup sweep and an admin
    # trigger_backfill can all be inside that window at once. When two of them
    # race, one INSERT loses and Postgres raises here.
    #
    # It is caught because of what it did otherwise: _backfill_entity wraps its
    # ENTIRE async-for in a single try, so one lost race ended the sweep for
    # that channel — every later message never ingested, nothing retrying, and
    # a log line that reads like a transient fetch failure. "I was not looking"
    # recorded as "nothing happened", by the backstop added to prevent exactly
    # that. The losing transaction has already rolled back and the winner
    # stored this message, so the work is done: log the guard firing and let
    # the sweep continue. No checkpoint is written — the message belongs to the
    # winner's run, and the guard skips it next time round anyway.
    try:
        async with async_session() as session:
            existing = await check_duplicate(
                session,
                result.get("summary", ""),
                result.get("event_type", "military"),
                lat, lon,
                message_date,
            )
            if existing:
                # Only a row whose own classification succeeded may take a
                # severity from another report. Otherwise the surviving row
                # reads "extraction_status=api_400, extraction_model=NULL"
                # while carrying a qwen3 number — a measurement its own
                # provenance says nothing produced. Those rows are healed by
                # re-classifying them, not by having a number quietly appear
                # on them.
                if severity is not None and existing.extraction_status != "ok":
                    logger.info(
                        "Not raising severity of #%s (extraction_status=%s) from a %s report",
                        existing.id, existing.extraction_status,
                        result.get("extraction_status"),
                    )
                    severity = None
                # The whole report is kept: merge_duplicate stores it as a row
                # on this event, in the same transaction as the counter it
                # bumps. The incoming killed_reported rides on that row, beside
                # the text that stated it; existing.killed_reported is still
                # never written.
                await merge_duplicate(session, existing, report, severity)
                ws_payload = EventWS(type="new_event", event=EventRead.model_validate(existing))
                await broadcaster.broadcast(ws_payload.model_dump_json())
                return

            db_event = Event(
                source="telegram",
                channel_name=channel_name,
                raw_text=raw_text,
                summary=result.get("summary", raw_text[:200]),
                event_type=result.get("event_type", "military"),
                severity=severity,
                killed_reported=result.get("killed_reported"),
                lat=lat,
                lon=lon,
                geometry=geometry,
                timestamp=message_date,
                source_reliability=get_reliability(channel_name),
                location_name=location_name,
                telegram_message_id=message_id,
                source_url=source_url,
                reporting_channels=channel_name,
                extraction_status=result.get("extraction_status"),
                extraction_model=result.get("extraction_model"),
                is_geolocated=is_geolocated,
                geo_precision=geo.precision if geo else None,
                geo_uncertainty_m=geo.uncertainty_m if geo else None,
                geo_method=geo.method if geo else None,
            )
            session.add(db_event)
            # flush, not commit: db_event.id does not exist until the INSERT
            # runs, and the report needs it. One commit still covers both, so
            # an event is never on disk without its first report.
            await session.flush()
            report.event_id = db_event.id
            session.add(report)
            await session.commit()
            await session.refresh(db_event)

            ws_payload = EventWS(type="new_event", event=EventRead.model_validate(db_event))
            await broadcaster.broadcast(ws_payload.model_dump_json())
            logger.info(
                "Event #%d saved | sev=%s | loc='%s' -> %s | geo=%s | %s",
                db_event.id, severity, location_name,
                f"({lat:.2f},{lon:.2f}) {geo.precision} ±{geo.uncertainty_m}m" if geo else "NULL",
                is_geolocated, source_url,
            )
    except IntegrityError:
        logger.info(
            "msg_id=%d from %s was stored by another worker while this one was "
            "classifying — refused by the UNIQUE index. The guard fired; the "
            "sweep continues", message_id, channel_name,
        )
        return

    # Update checkpoint so next restart won't re-process this message
    await _save_checkpoint(channel_name, message_id)


# ── Backfill helper — date-paginated, no fixed message count limit ─────────────

async def _backfill_entity(client: TelegramClient, entity, conflict_cutoff: datetime):
    """Fetch ALL messages for one channel since conflict_cutoff, oldest-first.

    Strategy:
    - If checkpoint exists: incremental — only messages with id > checkpoint
    - If no checkpoint: full historical sweep from conflict_cutoff to now
    Both approaches stop at the conflict_cutoff date, not at a message count.
    """
    channel_name = getattr(entity, "username", "") or getattr(entity, "title", "")
    last_id = await _get_checkpoint(channel_name)

    if last_id > 0:
        logger.info("Backfill %s: incremental from message_id > %d", channel_name, last_id)
        # Incremental: only new messages since last checkpoint
        iter_kwargs = dict(min_id=last_id, reverse=True)
    else:
        logger.info(
            "Backfill %s: full sweep from conflict start %s",
            channel_name, conflict_cutoff.strftime("%Y-%m-%d"),
        )
        # Full sweep: oldest-first from conflict_cutoff
        # reverse=True + offset_date = start from that date going forward
        iter_kwargs = dict(reverse=True, offset_date=conflict_cutoff)

    count = 0
    try:
        async for message in client.iter_messages(entity, **iter_kwargs):
            if not message.date:
                continue
            msg_date = message.date
            if msg_date.tzinfo is None:
                msg_date = msg_date.replace(tzinfo=timezone.utc)
            if msg_date < conflict_cutoff:
                continue  # shouldn't happen with offset_date, but safety
            if not message.text:
                continue
            await _process_message(
                raw_text=message.text,
                channel_name=channel_name,
                message_date=msg_date,
                message_id=message.id,
            )
            count += 1
            if count % 100 == 0:
                logger.info("Backfill %s: %d messages processed so far...", channel_name, count)
        logger.info("Backfill done for %s: %d total messages processed", channel_name, count)
    except Exception as e:
        logger.exception("Backfill error for %s: %s", channel_name, e)


# Shared client reference for on-demand backfill
_shared_client: TelegramClient | None = None


async def trigger_backfill() -> dict:
    """Trigger a full re-backfill from conflict start using the live client.
    Called by the admin API endpoint — does not restart the listener.
    """
    if _shared_client is None:
        return {"status": "error", "message": "Telegram client not yet initialised — wait for startup"}

    conflict_cutoff = _get_conflict_cutoff()
    channels = _get_channels()
    results = {}

    for ch in channels:
        try:
            entity = await _shared_client.get_entity(ch)
            await _backfill_entity(_shared_client, entity, conflict_cutoff)
            results[ch] = "ok"
        except Exception as e:
            results[ch] = f"error: {e}"
            logger.warning("On-demand backfill failed for %s: %s", ch, e)

    return {"status": "complete", "channels": results}


# ── Main listener ─────────────────────────────────────────────────────────────

async def start_telegram_listener():
    channels = _get_channels()
    conflict_cutoff = _get_conflict_cutoff()
    logger.info(
        "Monitoring %d channels from conflict start: %s",
        len(channels), conflict_cutoff.strftime("%Y-%m-%d")
    )

    global _shared_client

    # Prefer StringSession (written by auth.py, stored in .env) — works on
    # Windows Docker Desktop where bind-mount writes fail for SQLite files.
    if settings.telegram_session:
        logger.info("Using StringSession from TELEGRAM_SESSION env var")
        session = StringSession(settings.telegram_session)
    else:
        logger.info("Using file session: sessions/conflict_monitor.session")
        session = "sessions/conflict_monitor"

    client = TelegramClient(
        session,
        settings.telegram_api_id,
        settings.telegram_api_hash,
    )
    await client.start(phone=settings.telegram_phone)
    _shared_client = client  # expose for on-demand backfill

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
            channel_name = ""
            if hasattr(event.chat, "username") and event.chat.username:
                channel_name = event.chat.username
            elif hasattr(event.chat, "title"):
                channel_name = event.chat.title

            logger.info(
                "New message from %s [id=%d]: %s",
                channel_name, event.message.id, (event.message.text or "")[:80],
            )
            await _process_message(
                raw_text=event.message.text or "",
                channel_name=channel_name,
                message_date=event.message.date,
                message_id=event.message.id,
            )
        except Exception as e:
            logger.exception("Error processing live message: %s", e)

    # ── Full historical backfill from conflict start ───────────────────────────
    # Uses date-based iteration instead of a fixed message count so we never
    # miss history regardless of how active a channel is.
    for entity in resolved:
        await _backfill_entity(client, entity, conflict_cutoff)

    logger.info("All backfill complete — live listener active")
    await client.run_until_disconnected()
