"""Test harness for the backend.

RUN IT (two lines, from the repo's conflict-monitor/ directory):

    docker compose up -d db
    docker compose run --rm -T --no-deps -e LLM_BACKEND=none backend pytest

`--no-deps` and `-e LLM_BACKEND=none` are the GPU guard: a one-off container,
nothing started beside it, and a classifier that returns a fallback instead of
reaching Ollama if anything ever calls it unmocked. The network guard is
separate and lives in `_ban_network` below.

Every test here is a regression test for a defect that actually happened. Each
one names the commit it protects in its docstring, so deleting it is a recorded
decision rather than a tidy-up.

WHAT THIS SUITE CANNOT SEE, stated because a green run must not be read as more
than it is: the test database's schema comes from `Base.metadata.create_all` on
an empty database, while production's comes from the archive restore plus the
hand-kept ALTER list in main.py (`C63`). The first column added to models.py and
not to that list passes here — create_all made it — and fails on production's
INSERT. The test that would catch it builds the OLD table shape, runs the
migration and asserts every models.py column exists afterwards; it is not here
because that fixture is a second hand-written copy of the schema. It is the
single most valuable test to add next.
"""

import asyncio
import os

import pytest

# ── Isolation from the dev database ──────────────────────────────────────────
# This runs at conftest IMPORT, before any `app.*` module is imported, and that
# is the whole point of it living here rather than on the command line.
# app/db.py builds `create_async_engine(settings.database_url)` at module scope
# and app/config.py constructs `settings` at module scope, so whatever
# DATABASE_URL is in the environment at first import is what the app will talk
# to for the rest of the process. Set from the prompt it can be forgotten; set
# here it cannot be.
_DEV_URL = os.environ.get(
    "DATABASE_URL", "postgresql+asyncpg://postgres:postgres@db:5432/conflict_monitor"
)
_BASE, _, _DEV_NAME = _DEV_URL.rpartition("/")

TEST_DB_NAME = "conflict_monitor_test"
TEST_URL = f"{_BASE}/{TEST_DB_NAME}"

# A collection error cannot be skipped past. If this ever stops holding, the
# suite refuses to run rather than dropping tables in someone's dev database.
if not TEST_DB_NAME.endswith("_test") or TEST_DB_NAME == _DEV_NAME:
    raise RuntimeError(
        f"refusing to run: test database {TEST_DB_NAME!r} is not clearly "
        f"separate from {_DEV_NAME!r}"
    )

os.environ["DATABASE_URL"] = TEST_URL

# Plain DSNs for asyncpg, which does not understand the +asyncpg dialect prefix.
_PLAIN_BASE = _BASE.replace("postgresql+asyncpg://", "postgresql://")
MAINTENANCE_DSN = f"{_PLAIN_BASE}/postgres"
TEST_DSN = f"{_PLAIN_BASE}/{TEST_DB_NAME}"

# Imported only after DATABASE_URL is rewritten above.
import anthropic  # noqa: E402
import asyncpg  # noqa: E402
import httpx  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

from app.models import Base  # noqa: E402
from app.services import geocoder  # noqa: E402


def make_engine():
    """A fresh engine per test.

    NullPool and per-function construction are deliberate: asyncpg connections
    bind to the event loop that created them, and pytest-asyncio gives each
    test its own loop, so a shared pooled engine is a reliable source of
    "attached to a different loop" flakes. One connect per DB test is noise at
    this suite's size.
    """
    return create_async_engine(TEST_URL, poolclass=NullPool)


async def _build_test_database():
    conn = await asyncpg.connect(MAINTENANCE_DSN)
    try:
        # Drop at START, not at end: a crashed or Ctrl-C'd run leaves at most
        # one stale database and the next run destroys it before doing
        # anything. WITH (FORCE) so a leftover connection cannot silently turn
        # "recreate" into "reuse whatever was in there".
        await conn.execute(f'DROP DATABASE IF EXISTS "{TEST_DB_NAME}" WITH (FORCE)')
        await conn.execute(f'CREATE DATABASE "{TEST_DB_NAME}"')
    finally:
        await conn.close()

    conn = await asyncpg.connect(TEST_DSN)
    try:
        # The postgis image installs the extension into the default database
        # only, so a freshly created one needs its own CREATE EXTENSION.
        await conn.execute("CREATE EXTENSION IF NOT EXISTS postgis")
    finally:
        await conn.close()

    engine = make_engine()
    try:
        async with engine.begin() as c:
            await c.run_sync(Base.metadata.create_all)
    finally:
        await engine.dispose()


@pytest.fixture(scope="session", autouse=True)
def test_database():
    """Create the throwaway database once per session.

    Synchronous, driving asyncio.run over raw asyncpg, so it never shares a
    loop with pytest-asyncio and the loop-scope question disappears from
    setup entirely. It does NOT skip when Postgres is absent — it raises with
    the DSN in the message. `docker compose up -d db` is a precondition, not a
    condition.
    """
    asyncio.run(_build_test_database())
    yield


@pytest.fixture(autouse=True)
def _ban_network(monkeypatch):
    """Reaching the network is a failure with a stack trace, never a skip.

    This is the mechanism behind "the suite contains no conditional skips". A
    test that legitimately needs one of these installs its own recording fake,
    which shadows the ban; anything else raises.
    """

    async def _blocked(*args, **kwargs):
        raise RuntimeError("test attempted network I/O")

    def _blocked_sync(*args, **kwargs):
        raise RuntimeError("test attempted network I/O")

    monkeypatch.setattr(httpx.AsyncClient, "send", _blocked)
    monkeypatch.setattr(httpx.Client, "send", _blocked_sync)
    monkeypatch.setattr(geocoder, "_query_nominatim", _blocked)
    # The container really does carry ANTHROPIC_API_KEY from .env, so a test
    # that walks one branch too far in classify_message would otherwise spend
    # money. Constructing the client is the doorway; slam it.
    monkeypatch.setattr(anthropic.AsyncAnthropic, "__init__", _blocked_sync)


@pytest.fixture(autouse=True)
def _clear_geocoder_cache():
    """geocoder._cache is a module global. Without this a cached "Maarakeh"
    makes the next test's result a lie."""
    geocoder._cache.clear()
    yield
    geocoder._cache.clear()
