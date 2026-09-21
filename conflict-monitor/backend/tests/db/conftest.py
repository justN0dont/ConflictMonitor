"""Database fixtures.

Two isolation strategies, because the code under test wants two different
things:

  `session` — a connection-level transaction that is rolled back. The ORM
  session joins it with join_transaction_mode="create_savepoint", which is
  REQUIRED and not stylistic: merge_duplicate calls `await session.commit()`
  and `await session.refresh(existing)` itself, and in savepoint mode that
  inner commit releases a savepoint so the outer rollback still erases the
  row. Without it every merge test would leak into the next.

  `migration_engine` — the real thing, committing for real, because
  run_startup_migrations owns its own `engine.begin()` blocks and cannot be
  handed a session. Isolated by TRUNCATE before and after, RESTART IDENTITY
  because several assertions read ids.
"""

import datetime

import pytest
from geoalchemy2.shape import from_shape
from shapely.geometry import Point
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Event

from tests.conftest import make_engine

_TRUNCATE = (
    "TRUNCATE events, event_reports, channel_checkpoints RESTART IDENTITY CASCADE"
)


@pytest.fixture
async def session():
    engine = make_engine()
    try:
        async with engine.connect() as conn:
            trans = await conn.begin()
            # expire_on_commit=False because app/db.py's sessionmaker sets
            # it, and merge_duplicate depends on it: its closing log line
            # reads report.id AFTER its own commit, which on an expiring
            # session is a lazy reload in a sync context (MissingGreenlet).
            # The fixture matches how the code is really called, or the test
            # is about a session nobody builds.
            s = AsyncSession(
                bind=conn,
                join_transaction_mode="create_savepoint",
                expire_on_commit=False,
            )
            try:
                yield s
            finally:
                await s.close()
                await trans.rollback()
    finally:
        await engine.dispose()


@pytest.fixture
async def migration_engine():
    engine = make_engine()
    try:
        async with engine.begin() as conn:
            await conn.execute(text(_TRUNCATE))
        yield engine
        async with engine.begin() as conn:
            await conn.execute(text(_TRUNCATE))
    finally:
        await engine.dispose()


NOW = datetime.datetime(2026, 9, 20, 12, 0, tzinfo=datetime.timezone.utc)


def make_event(summary, *, lat=None, lon=None, when=NOW, event_type="military", **kw):
    """Seed one events row. lat/lon None means NULL geometry — the honest
    unlocated state that replaced the (-25, 80) sentinel in 89c6f54."""
    geometry = from_shape(Point(lon, lat), srid=4326) if lat is not None else None
    return Event(
        summary=summary,
        raw_text=kw.pop("raw_text", summary),
        event_type=event_type,
        timestamp=when,
        lat=lat,
        lon=lon,
        geometry=geometry,
        **kw,
    )
