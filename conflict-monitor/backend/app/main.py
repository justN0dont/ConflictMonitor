import asyncio
import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app import feeds
from app.config import settings
from app.db import engine
from app.models import Base
from app.routes.channels import router as channels_router
from app.routes.events import router as events_router
from app.routes.tracking import router as tracking_router
from app.routes.ws import router as ws_router
from app.services.classifier import evidence_span
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


async def run_startup_migrations(engine):
    """Schema repair and the two one-off data migrations, run once at boot.

    A pure move out of `lifespan`, which is still its only production caller.
    It is a separate function so it can be driven against a throwaway database
    on its own: entering `lifespan` also starts five network pollers, so a test
    for the sentinel retirement or the backfill could otherwise only be written
    against a COPY of these statements — which would verify the copy.
    """
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
            ("killed_reported",     "INTEGER", "NULL"),
            ("extraction_status",   "TEXT",    "NULL"),
            ("extraction_model",    "TEXT",    "NULL"),
            ("is_geolocated",       "BOOLEAN", "NULL"),
            ("geo_precision",       "TEXT",    "NULL"),
            ("geo_uncertainty_m",   "INTEGER", "NULL"),
            ("geo_method",          "TEXT",    "NULL"),
            # DEFAULT NULL, not '', and the difference is the whole column:
            # '' is a measurement ("nothing in this text spells this place")
            # and NULL is the absence of one. Defaulting to '' would stamp the
            # measurement on 83,938 rows nothing had read. The repair pass
            # below is what turns those NULLs into answers.
            ("evidence_span",       "TEXT",    "NULL"),
        ]
        for col, col_type, default in migrations:
            try:
                await conn.execute(text(
                    f"ALTER TABLE events ADD COLUMN IF NOT EXISTS {col} "
                    f"{col_type} DEFAULT {default}"
                ))
            except Exception:
                # Logged, not passed silently. ADD COLUMN IF NOT EXISTS does
                # not fail in normal operation, so a failure here is real —
                # and in Postgres it also ABORTS this transaction, so every
                # statement after it fails too. With `pass` the only symptom
                # was the later statement's error, naming a statement that was
                # fine. The failure that caused it has to be in the log or the
                # boot is unreadable.
                logger.exception("Migration ALTER for events.%s failed", col)

        # severity was created NOT NULL DEFAULT 5, which made "we did not
        # measure severity" unrepresentable and stamped the classifier's
        # fallback on disk as a finding. create_all never alters an existing
        # column, so the constraint has to be dropped explicitly. Idempotent:
        # DROP NOT NULL on an already-nullable column is a no-op.
        try:
            await conn.execute(text("ALTER TABLE events ALTER COLUMN severity DROP NOT NULL"))
            await conn.execute(text("ALTER TABLE events ALTER COLUMN severity DROP DEFAULT"))
        except Exception:
            # Same rule as the loop above: a swallowed failure here aborts the
            # transaction and the next statement takes the blame.
            logger.exception("Dropping severity NOT NULL/DEFAULT failed")

        # ── One-off: retire the Indian Ocean sentinel ───────────────────────
        # Unlocated events used to be written to (-25.0, 80.0), open ocean
        # south-west of Australia, because the schema had no way to say "we
        # could not place this". They are now written with NULL coordinates,
        # but every row already on disk still carries the sentinel — and the
        # definition of "located" moved to "geometry IS NOT NULL" underneath
        # them. That made the archive read as MORE located than before this
        # change: geo-stats counted them as geolocated, _fix_null_coords_task
        # (which now selects geometry IS NULL) could no longer see the only
        # rows that actually need repair, and the map kept drawing them in
        # the sea. Normalising them is what makes the new definition true of
        # the whole table instead of only of new writes.
        #
        # (-25.0, 80.0) is not a place any event has ever happened, and
        # nothing geocodes there any more, so this cannot destroy a real
        # coordinate. Idempotent: after the first run no row matches.
        #
        # Deliberately NOT done here: nulling severity on rows whose
        # extraction_status is a failure value. Those 5s are provably the old
        # _build_fallback default rather than measurements, but the
        # re-classify admin task writes a real severity without clearing the
        # stale status, so the same predicate would also erase measured
        # values. That one needs a decision, not a startup migration.
        try:
            res = await conn.execute(text(
                "UPDATE events SET lat = NULL, lon = NULL, geometry = NULL, "
                "is_geolocated = false, geo_precision = NULL, "
                "geo_uncertainty_m = NULL, geo_method = NULL "
                "WHERE lat = -25.0 AND lon = 80.0"
            ))
            if res.rowcount:
                logger.info(
                    "  Retired Indian Ocean sentinel on %d event(s) — "
                    "they are now honestly unlocated", res.rowcount,
                )
        except Exception:
            logger.exception("Sentinel retirement migration failed")

        # Index on telegram_message_id for fast dedup lookups.
        # Kept: the dedup guard reads event_reports now, but events.py and the
        # archive tools still query this column.
        #
        # It lives in THIS block, with the other statements about `events`,
        # and not beside the backfill below. A caught failure here would abort
        # whichever transaction it is in, and in the backfill's transaction
        # that means the INSERT is rolled back at COMMIT after "Backfilled N"
        # has already been logged — a boot that reports work it did not keep.
        try:
            await conn.execute(text(
                "CREATE INDEX IF NOT EXISTS idx_events_tg_msg_id "
                "ON events(telegram_message_id) WHERE telegram_message_id IS NOT NULL"
            ))
        except Exception:
            logger.exception("Index idx_events_tg_msg_id could not be created")

    # ── One-off: give every existing event its first report row ─────────────
    # In its OWN transaction, and that is the point of the split. Everything
    # above is idempotent schema repair whose failures are caught; in Postgres
    # a failed statement aborts the whole transaction and SQLAlchemy takes no
    # savepoint per execute, so one caught failure up there leaves every later
    # statement raising InFailedSQLTransactionError. The backfill below is
    # deliberately NOT caught (see next paragraph), so it would be the
    # statement that kills the boot — naming itself in "Application startup
    # failed" while the statement that actually failed had been swallowed. A
    # separate transaction lets it fail on its own merits. It does not make the
    # loop above per-statement transactional: a failure there still rolls the
    # whole schema block back at COMMIT, which is now at least logged.
    async with engine.begin() as conn:
        # event_reports is a new TABLE, so create_all above already made it —
        # with every column this commit gives it, extraction_status included —
        # and the hand-kept ALTER list in the block above does not need a line;
        # it could not serve one anyway, being hardcoded to "ALTER TABLE
        # events". C63's cost is deferred to the next column added to
        # event_reports AFTER this commit, not paid here.
        #
        # The DATA is the part that is not free. On a database that already has
        # rows — the dev DB, or a restored archive — every event predates the
        # table, and two things break if that is left to "going forward only":
        #
        #   - _message_already_saved and the RSS URL guard now read
        #     event_reports. With no rows there, all 49,369 archived Telegram
        #     messages and every stored article URL are unknown to them, and
        #     the next sweep re-ingests and re-classifies the lot on the GPU.
        #     The backfill is a precondition of that guard change, not a
        #     nicety, which is why they ship together.
        #   - report_count would sit beside zero reports on 83,938 events that
        #     really were reported: "the table did not exist when this was
        #     ingested" written into the field a reader takes as "nobody
        #     reported this".
        #
        # Every column below is a fact about that one report, because that
        # report's raw_text IS the event's raw_text — one string, one
        # classifier run over it.
        #
        # killed_reported and extraction_status therefore transfer TOGETHER,
        # and only together. The count on its own would be a lie by omission at
        # archive scale: restore the 2026-08-18 dump and this INSERT writes
        # 83,938 rows whose killed_reported is NULL — not because those sources
        # stated no toll, but because that dump carries no killed_reported
        # column at all (see "The sentinel migration, audited before it runs")
        # — into a table whose NULL a reader is invited to read as "this source
        # stated no count". Carrying the event's own extraction_status across
        # is what keeps those rows readable, and it is honest for the same
        # reason every other column here is: it describes a classification of
        # THIS report's text. Where we have nothing to say it stays NULL —
        # every row written before events.extraction_status existed, which on
        # that dump is all of them. NULL status beside a NULL count says "I
        # cannot tell you why the count is missing", which is true; what it
        # must never say is "nobody was killed".
        #
        # What this does NOT do is recover the reports merge destroyed —
        # 19,027 of them on the archive, and their text is gone. report_count
        # and reporting_channels therefore keep their stored values and are not
        # derived from this table: they are the only surviving record of those
        # reports, so `report_count - count(reports)` is the per-row size of
        # the loss. Zero for everything ingested after this; 213 on the one
        # archive row that claims 214; negative on the 480 archive rows whose
        # report_count is 0, a value no writer in this tree can produce.
        #
        # Deliberately NOT caught at all, unlike the schema statements above,
        # which are caught and logged. A skipped backfill is not a missing
        # column, it is 49,369 classifier calls on the next sweep, so it fails
        # the boot instead — the same preference C63 argued for. That is only
        # safe now that it has a transaction of its own to fail in.
        res = await conn.execute(text("""
            INSERT INTO event_reports (event_id, source, channel, raw_text, summary,
                                       source_url, telegram_message_id,
                                       killed_reported, extraction_status,
                                       reported_at, ingested_at)
            SELECT e.id, e.source, e.channel_name, e.raw_text, e.summary,
                   e.source_url, e.telegram_message_id,
                   e.killed_reported, e.extraction_status, e.timestamp, e.created_at
            FROM events e
            WHERE NOT EXISTS (
                SELECT 1 FROM event_reports r WHERE r.event_id = e.id
            )
        """))
        if res.rowcount:
            logger.info(
                "  Backfilled %d event(s) with their first report row. After "
                "the first boot this prints nothing; a non-zero number later "
                "means a writer stopped recording its report", res.rowcount,
            )

        # The key _message_already_saved now uses. UNIQUE because that guard is
        # check-then-act with a wide window: the check runs in a session that
        # is closed again before classification and geocoding (seconds), and
        # the live handler and a backfill sweep can both be inside it. This is
        # the database half; _process_message catches the IntegrityError it
        # raises and logs the guard firing, so expect that line during a
        # backfill and read it as the guard working, not as a new bug. It only
        # reads that way because it is caught: uncaught, one lost race ended
        # the whole channel sweep, which is the opposite of a backstop.
        # Pre-flighted before it was written: the archive has 49,369 Telegram
        # rows, 49,369 distinct message ids and zero (channel, id) duplicates;
        # the dev DB has no Telegram rows at all. Left to raise for the same
        # reason as the backfill — a silently skipped unique index leaves the
        # guard with no backstop and nothing saying so.
        await conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_event_reports_tg_msg "
            "ON event_reports(channel, telegram_message_id) "
            "WHERE telegram_message_id IS NOT NULL"
        ))
        # The key the RSS guard uses. NOT unique: 49 archive URLs already sit
        # on two or more event rows, so a unique index would fail the backfill
        # above. events.source_url has never had an index at all.
        await conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_event_reports_source_url "
            "ON event_reports(source_url) WHERE source_url <> ''"
        ))

    # ── Repair pass: give every row on disk its evidence_span ───────────────
    # In Python, and NOT in SQL, on purpose. Postgres can express a word-
    # anchored case-insensitive match, so this loop could have been one UPDATE
    # — and then there would be two definitions of what counts as a quote,
    # drifting apart, with the column unable to say which one wrote it. The
    # writers call classifier.evidence_span(); so does this. One rule.
    #
    # Its own transaction, for the reason the report backfill gives above. It
    # is CAUGHT, unlike that one, and the difference is what a skip costs: a
    # skipped report backfill is 49,369 classifier calls on the next sweep, a
    # skipped pass here leaves NULLs, and NULL already means exactly "nothing
    # has looked at this row". The failure mode is the honest value, so it does
    # not get to fail the boot.
    #
    # Batched because a restored archive is 83,938 rows, and walked by a
    # CURSOR on id rather than by re-reading the head of the same predicate.
    # `WHERE evidence_span IS NULL` alone would make termination depend on
    # evidence_span() never returning None: with the cursor removed and the
    # function stubbed to None the loop re-selects the same 5,000 rows for
    # ever, inside engine.begin(), during lifespan — a held transaction and an
    # app that never serves. `AND id > :last` bounds it structurally instead.
    # Every iteration strictly advances `last`, so the pass ends after at most
    # ceil(rows/5000) of them whatever the function returns, and a row it
    # somehow failed to fill stays NULL — which is this column's honest value
    # for "nothing looked" and is repaired on the next boot.
    try:
        async with engine.begin() as conn:
            repaired = quoted = 0
            last_id = 0
            while True:
                rows = (await conn.execute(text(
                    "SELECT id, raw_text, location_name FROM events "
                    "WHERE evidence_span IS NULL AND id > :last "
                    "ORDER BY id LIMIT 5000"
                ), {"last": last_id})).all()
                if not rows:
                    break
                last_id = rows[-1].id
                params = [
                    {
                        "id": r.id,
                        "span": evidence_span(r.raw_text or "", r.location_name or ""),
                    }
                    for r in rows
                ]
                await conn.execute(
                    text("UPDATE events SET evidence_span = :span WHERE id = :id"),
                    params,
                )
                repaired += len(params)
                # Counted from what this pass wrote, not from a SELECT over the
                # whole column: on a later boot that repairs three new rows, a
                # table-wide count would report the archive's total beside them.
                quoted += sum(1 for p in params if p["span"])
            if repaired:
                logger.info(
                    "  Gave %d event(s) an evidence_span; %d of them quote their "
                    "own location_name. After the first boot this prints nothing",
                    repaired, quoted,
                )
    except Exception:
        logger.exception("evidence_span repair pass failed — rows stay NULL")

    logger.info("Database tables ready")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await run_startup_migrations(engine)

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
        feeds.tracker("aircraft").task = tasks[-1]
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
        feeds.tracker("aircraft").task = tasks[-1]
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


_PROCESS_STARTED_AT = time.time()


@app.get("/")
async def root():
    """Process liveness only: it says the process answers, nothing about feeds."""
    return {"status": "ok"}


@app.get("/health")
async def health():
    """Feed health, read from memory so it answers with the database down.

    Always HTTP 200: what is wrong is said in the body. A non-200 would let a
    container healthcheck restart the backend because adsb.lol went down.
    Only the aircraft feed reports so far; the others join as feed_health
    reaches them (docs/FINDINGS.md, Phase 2).
    """
    return feeds.health(time.time(), _PROCESS_STARTED_AT)


@app.get("/config")
async def config():
    return {"demo_mode": settings.demo_mode}
