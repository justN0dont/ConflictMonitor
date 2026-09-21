"""The startup migrations, driven as the real statements. Protects 89c6f54
and 8523961.

These run against `run_startup_migrations(engine)` — the real function, not a
copy of its SQL. A copy would verify the copy, which is the "green run means
nothing" failure this suite exists to avoid. That is the whole reason the
block was lifted out of `lifespan`: entering `lifespan` also starts five
network pollers.

They commit for real, so they use `migration_engine` (TRUNCATE either side)
rather than the rolled-back `session`.
"""

import datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.main import run_startup_migrations

NOW = datetime.datetime(2026, 9, 20, 12, 0, tzinfo=datetime.timezone.utc)

# Raw SQL on purpose: the ORM would supply report_count / reporting_channels /
# location_name / source_url from its client-side defaults, and this file is
# about what the migration does to rows that are already on disk.
_INSERT_EVENT = text(
    "INSERT INTO events (summary, raw_text, event_type, timestamp, created_at, "
    "lat, lon, geometry, is_geolocated, geo_precision, geo_uncertainty_m, "
    "geo_method, killed_reported, extraction_status, channel_name, source, "
    "report_count, reporting_channels, location_name, source_url) "
    # ST_MakePoint(NULL, NULL) is NULL, so one statement covers the located
    # and the unlocated case. Built in SQL because a raw text() has no type
    # binding to turn a WKBElement into geometry.
    "VALUES (:summary, :raw_text, 'military', :ts, :ts, :lat, :lon, "
    "ST_SetSRID(ST_MakePoint(:lon, :lat), 4326), "
    ":is_geolocated, :geo_precision, :geo_uncertainty_m, :geo_method, "
    ":killed_reported, :extraction_status, :channel, 'telegram', "
    "1, '', '', '') RETURNING id"
)


def _event_params(summary, lat, lon, **kw):
    return {
        "summary": summary,
        "raw_text": kw.pop("raw_text", summary),
        "ts": NOW,
        "lat": lat,
        "lon": lon,
        "is_geolocated": kw.pop("is_geolocated", lat is not None),
        "geo_precision": kw.pop("geo_precision", "city" if lat is not None else None),
        "geo_uncertainty_m": kw.pop("geo_uncertainty_m", 10000 if lat is not None else None),
        "geo_method": kw.pop("geo_method", "table-exact" if lat is not None else None),
        "killed_reported": kw.pop("killed_reported", None),
        "extraction_status": kw.pop("extraction_status", None),
        "channel": kw.pop("channel", "chan"),
    }


# ── 89c6f54: retiring the Indian Ocean sentinel ──────────────────────────────


async def test_the_sentinel_becomes_an_honestly_unlocated_row(migration_engine):
    """89c6f54. Unlocated events used to be written to (-25.0, 80.0), open
    ocean south-west of Australia. When "located" moved to
    `geometry IS NOT NULL` underneath them, the archive read as MORE located
    than before: geo-stats counted them, _fix_null_coords_task could no
    longer see the only rows needing repair, and the map kept drawing them in
    the sea. Run against a restored archive this retired 39,949 rows."""
    async with migration_engine.begin() as conn:
        sentinel_id = (
            await conn.execute(_INSERT_EVENT, _event_params("parked", -25.0, 80.0))
        ).scalar_one()
        real_id = (
            await conn.execute(_INSERT_EVENT, _event_params("Tel Aviv", 32.0853, 34.7818))
        ).scalar_one()

    await run_startup_migrations(migration_engine)

    async with migration_engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT lat, lon, geometry IS NULL AS no_geom, is_geolocated, "
                    "geo_precision, geo_uncertainty_m, geo_method "
                    "FROM events WHERE id = :id"
                ),
                {"id": sentinel_id},
            )
        ).one()
        assert row.lat is None and row.lon is None
        assert row.no_geom is True
        assert row.is_geolocated is False
        # An unresolvable location writes NULL, never a sentinel and never a
        # borrowed bound.
        assert row.geo_precision is None
        assert row.geo_uncertainty_m is None
        assert row.geo_method is None

        kept = (
            await conn.execute(
                text("SELECT lat, lon, is_geolocated FROM events WHERE id = :id"),
                {"id": real_id},
            )
        ).one()
        assert (kept.lat, kept.lon) == (32.0853, 34.7818)
        assert kept.is_geolocated is True


async def test_the_sentinel_migration_is_idempotent(migration_engine):
    """89c6f54. (-25.0, 80.0) is not a place any event has ever happened and
    nothing geocodes there any more, so this cannot destroy a real
    coordinate — but it runs on every boot, so "after the first run no row
    matches" has to be true rather than assumed."""
    async with migration_engine.begin() as conn:
        await conn.execute(_INSERT_EVENT, _event_params("parked", -25.0, 80.0))

    await run_startup_migrations(migration_engine)
    await run_startup_migrations(migration_engine)

    async with migration_engine.connect() as conn:
        still_parked = (
            await conn.execute(
                text("SELECT count(*) FROM events WHERE lat = -25.0 AND lon = 80.0")
            )
        ).scalar_one()
    assert still_parked == 0


# ── 8523961: the first-report backfill ───────────────────────────────────────


async def test_every_event_without_a_report_gets_exactly_one(migration_engine):
    """8523961. Without this, _message_already_saved and the RSS URL guard —
    which now read event_reports — know nothing of 49,369 archived Telegram
    messages, and the next sweep re-ingests and re-classifies the lot on the
    GPU. The backfill is a precondition of that guard change, not a nicety."""
    async with migration_engine.begin() as conn:
        event_id = (
            await conn.execute(
                _INSERT_EVENT,
                _event_params(
                    "Strike on Natanz",
                    None,
                    None,
                    killed_reported=17,
                    extraction_status="ok",
                    channel="chan_a",
                ),
            )
        ).scalar_one()

    await run_startup_migrations(migration_engine)

    async with migration_engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT event_id, channel, raw_text, killed_reported, "
                    "extraction_status FROM event_reports WHERE event_id = :id"
                ),
                {"id": event_id},
            )
        ).all()
    assert len(rows) == 1
    (row,) = rows
    # killed_reported and extraction_status transfer TOGETHER. The count on
    # its own would be a lie by omission at archive scale: 83,938 rows whose
    # NULL a reader is invited to read as "this source stated no count".
    assert row.killed_reported == 17
    assert row.extraction_status == "ok"
    assert row.channel == "chan_a"
    assert row.raw_text == "Strike on Natanz"


async def test_a_null_count_carries_the_status_that_explains_it(migration_engine):
    """8523961 / models.py. NULL count beside a NULL status says "I cannot
    tell you why the count is missing", which is true. What it must never say
    is "nobody was killed"."""
    async with migration_engine.begin() as conn:
        event_id = (
            await conn.execute(
                _INSERT_EVENT,
                _event_params(
                    "Unclassifiable text",
                    None,
                    None,
                    killed_reported=None,
                    extraction_status="no_backend",
                ),
            )
        ).scalar_one()

    await run_startup_migrations(migration_engine)

    async with migration_engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT killed_reported, extraction_status FROM event_reports "
                    "WHERE event_id = :id"
                ),
                {"id": event_id},
            )
        ).one()
    assert row.killed_reported is None
    assert row.extraction_status == "no_backend"


async def test_the_backfill_does_not_run_twice(migration_engine):
    """8523961. The WHERE NOT EXISTS is what makes this safe on every boot;
    a second row per event would double every event's evidence."""
    async with migration_engine.begin() as conn:
        event_id = (
            await conn.execute(_INSERT_EVENT, _event_params("Strike", None, None))
        ).scalar_one()

    await run_startup_migrations(migration_engine)
    await run_startup_migrations(migration_engine)

    async with migration_engine.connect() as conn:
        count = (
            await conn.execute(
                text("SELECT count(*) FROM event_reports WHERE event_id = :id"),
                {"id": event_id},
            )
        ).scalar_one()
    assert count == 1


async def test_an_event_that_already_has_a_report_gets_none(migration_engine):
    """8523961. An event ingested after the table existed already has its
    report row; the backfill must leave it alone rather than add a duplicate
    account of the same report."""
    async with migration_engine.begin() as conn:
        event_id = (
            await conn.execute(_INSERT_EVENT, _event_params("Strike", None, None))
        ).scalar_one()
        await conn.execute(
            text(
                "INSERT INTO event_reports (event_id, source, channel, raw_text, summary, "
                "source_url) VALUES (:id, 'telegram', 'live', 'live text', 'Strike', '')"
            ),
            {"id": event_id},
        )

    await run_startup_migrations(migration_engine)

    async with migration_engine.connect() as conn:
        rows = (
            await conn.execute(
                text("SELECT raw_text FROM event_reports WHERE event_id = :id"),
                {"id": event_id},
            )
        ).all()
    assert [r.raw_text for r in rows] == ["live text"]


# ── 8523961: the partial UNIQUE index behind the ingest guard ────────────────


async def test_the_same_message_twice_on_one_channel_is_refused(migration_engine):
    """8523961. _message_already_saved is check-then-act with a wide window —
    the check runs in a session closed again before classification and
    geocoding (seconds), and the live handler and a backfill sweep can both
    be inside it. This is the database half; _process_message catches the
    IntegrityError."""
    await run_startup_migrations(migration_engine)

    async with migration_engine.begin() as conn:
        event_id = (
            await conn.execute(_INSERT_EVENT, _event_params("Strike", None, None))
        ).scalar_one()

    insert = text(
        "INSERT INTO event_reports (event_id, source, channel, telegram_message_id, summary, "
        "raw_text, source_url) VALUES (:id, 'telegram', 'chan_a', 4821, 's', 's', '')"
    )
    async with migration_engine.begin() as conn:
        await conn.execute(insert, {"id": event_id})

    with pytest.raises(IntegrityError):
        async with migration_engine.begin() as conn:
            await conn.execute(insert, {"id": event_id})


async def test_non_telegram_reports_are_not_constrained(migration_engine):
    """8523961. The index is PARTIAL. NULL telegram_message_id means "this
    report did not come from Telegram" — every RSS article — so two of them
    on one source must both insert."""
    await run_startup_migrations(migration_engine)

    async with migration_engine.begin() as conn:
        event_id = (
            await conn.execute(_INSERT_EVENT, _event_params("Strike", None, None))
        ).scalar_one()
        for i in range(2):
            await conn.execute(
                text(
                    "INSERT INTO event_reports (event_id, source, channel, "
                    "telegram_message_id, summary, raw_text, source_url) "
                    "VALUES (:id, 'rss', 'Reuters', NULL, :s, :s, '')"
                ),
                {"id": event_id, "s": f"article {i}"},
            )

    async with migration_engine.connect() as conn:
        count = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM event_reports WHERE event_id = :id "
                    "AND telegram_message_id IS NULL"
                ),
                {"id": event_id},
            )
        ).scalar_one()
    assert count == 2


# ── 89c6f54: severity is nullable, and stays that way ────────────────────────


async def test_severity_can_be_null_after_the_migration(migration_engine):
    """89c6f54. severity was created NOT NULL DEFAULT 5, which made "we did
    not measure severity" unrepresentable and stamped the classifier's
    fallback on disk as a finding — 86.3% of the archive is exactly 5.
    create_all never alters an existing column, so the constraint has to be
    dropped explicitly.

    The three statements below are the test, not setup noise. This database's
    schema came from create_all, where severity is ALREADY nullable, so the
    migration would have nothing to do and this test passed with both ALTERs
    deleted — it was asserting a property the fixture handed it for free.
    Putting the column back into production's shape first is what makes the
    assertion below about the migration. It needs no teardown: the statement
    under test is what makes the column nullable again."""
    async with migration_engine.begin() as conn:
        await conn.execute(
            text("ALTER TABLE events ALTER COLUMN severity SET DEFAULT 5")
        )
        await conn.execute(
            text("ALTER TABLE events ALTER COLUMN severity SET NOT NULL")
        )

    await run_startup_migrations(migration_engine)

    async with migration_engine.begin() as conn:
        await conn.execute(_INSERT_EVENT, _event_params("Unmeasured", None, None))
        row = (
            await conn.execute(
                text("SELECT severity FROM events WHERE summary = 'Unmeasured'")
            )
        ).one()
    assert row.severity is None, "nothing may substitute 5 for an unmeasured severity"
