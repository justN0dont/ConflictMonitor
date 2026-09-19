import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.config import settings
from app.db import engine
from app.models import Base
from app.routes.channels import router as channels_router
from app.routes.events import router as events_router
from app.routes.tracking import router as tracking_router
from app.routes.ws import router as ws_router
from app.services.connectivity import start_connectivity_poller
from app.services.satellites import start_tle_fetcher

logger = logging.getLogger("conflict-monitor")
logging.basicConfig(level=logging.INFO)


def _log_task_exception(task: asyncio.Task) -> None:
    """Background tasks are fire-and-forget; without this their errors vanish."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("Background task died: %r", exc, exc_info=exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Create tables (replaced by Alembic in production)
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS postgis"))
        await conn.run_sync(Base.metadata.create_all)
        # Add columns that may be missing on existing tables (safe, idempotent)
        migrations = [
            ("report_count",        "INTEGER", "1"),
            ("reporting_channels",  "TEXT",    "''"),
            ("source_reliability",  "INTEGER", "NULL"),
            ("location_name",       "TEXT",    "''"),
            ("telegram_message_id", "INTEGER", "NULL"),
            ("source_url",          "TEXT",    "''"),
            ("extraction_status",   "TEXT",    "NULL"),
            ("is_geolocated",       "BOOLEAN", "NULL"),
        ]
        for col, col_type, default in migrations:
            try:
                await conn.execute(text(
                    f"ALTER TABLE events ADD COLUMN IF NOT EXISTS {col} "
                    f"{col_type} DEFAULT {default}"
                ))
            except Exception:
                pass

        # Index on telegram_message_id for fast dedup lookups
        try:
            await conn.execute(text(
                "CREATE INDEX IF NOT EXISTS idx_events_tg_msg_id "
                "ON events(telegram_message_id) WHERE telegram_message_id IS NOT NULL"
            ))
        except Exception:
            pass
    logger.info("Database tables ready")

    tasks: list[asyncio.Task] = []

    if settings.demo_mode:
        # ── DEMO MODE ──────────────────────────────────────────────
        logger.info("=" * 60)
        logger.info("  DEMO MODE ACTIVE — generating synthetic data")
        logger.info("  No Telegram, Anthropic, or AISStream keys required")
        logger.info("=" * 60)

        from app.services.demo import (
            seed_demo_history,
            start_demo_event_generator,
            start_demo_aircraft_poller,
            start_demo_vessel_poller,
        )

        # Seed historical events
        await seed_demo_history(300)

        # Start synthetic generators
        tasks.append(asyncio.create_task(start_demo_event_generator()))
        tasks.append(asyncio.create_task(start_demo_aircraft_poller()))
        tasks.append(asyncio.create_task(start_demo_vessel_poller()))

        # CelesTrak is free, still use real satellite data
        tasks.append(asyncio.create_task(start_tle_fetcher()))
        logger.info("CelesTrak TLE fetcher started (real data)")

        # IODA is free, still use real internet-disruption data
        tasks.append(asyncio.create_task(start_connectivity_poller()))
        logger.info("IODA connectivity poller started (real data)")

    else:
        # ── PRODUCTION MODE ────────────────────────────────────────
        from app.services.telegram import start_telegram_listener
        from app.services.opensky import start_opensky_poller
        from app.services.maritime import start_maritime_poller

        # Telegram listener
        if settings.telegram_api_id and settings.telegram_api_hash:
            tasks.append(asyncio.create_task(start_telegram_listener()))
            logger.info("Telegram listener started")
        else:
            logger.warning("Telegram credentials not set — listener disabled")

        # Aircraft tracking
        tasks.append(asyncio.create_task(start_opensky_poller()))
        logger.info("Aircraft poller started")

        # Satellite TLEs
        tasks.append(asyncio.create_task(start_tle_fetcher()))
        logger.info("CelesTrak TLE fetcher started")

        # Internet-disruption sensors
        tasks.append(asyncio.create_task(start_connectivity_poller()))
        logger.info("IODA connectivity poller started")

        # Maritime vessels
        tasks.append(asyncio.create_task(start_maritime_poller()))
        logger.info("Maritime poller started")

        # News feed ingestion (RSS from Reuters, BBC, Al Jazeera, Times of Israel, etc.)
        from app.services.news_feeds import start_news_feed_poller
        tasks.append(asyncio.create_task(start_news_feed_poller()))
        logger.info("News feed poller started (RSS: Reuters, BBC, Al Jazeera, Times of Israel, Iran International, RFI, MEE)")

    for task in tasks:
        task.add_done_callback(_log_task_exception)

    yield

    for task in tasks:
        task.cancel()


app = FastAPI(title="Conflict Monitor", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(channels_router)
app.include_router(events_router)
app.include_router(tracking_router)
app.include_router(ws_router)


@app.get("/")
async def health():
    return {"status": "ok"}


@app.get("/config")
async def config():
    return {"demo_mode": settings.demo_mode}
