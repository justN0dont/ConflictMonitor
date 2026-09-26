"""feed_health persistence (app/feed_store.py) against a real Postgres.

Decision 3 in docs/FINDINGS.md: transition rows plus per-minute heartbeat
rows, a process_start marker at boot, and any heartbeat gap over 120 s read
as UNKNOWN - the one property that lets the Phase 3 liveness lane draw the
monitor's own outages as holes instead of stretching the last "ok" across them.
"""

import datetime

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app import feed_store, feeds
from app.feeds import ErrorKind, FeedTracker
from app.models import FeedHealth, FeedHeartbeat, FeedTransition

from tests.conftest import make_engine

T0 = 1_800_000_000.0
_TRUNCATE = "TRUNCATE feed_health, feed_transition, feed_heartbeat RESTART IDENTITY"


@pytest.fixture
async def factory():
    engine = make_engine()
    try:
        async with engine.begin() as conn:
            await conn.execute(text(_TRUNCATE))
        yield async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as conn:
            await conn.execute(text(_TRUNCATE))
    finally:
        await engine.dispose()


@pytest.fixture
def one_feed(monkeypatch):
    """A single aircraft tracker, so row counts are exact."""
    t = FeedTracker(feeds.REGISTRY["aircraft"])
    monkeypatch.setattr(feeds, "TRACKERS", {"aircraft": t})
    return t


async def _count(factory, model, **where):
    async with factory() as s:
        q = select(func.count()).select_from(model)
        for k, v in where.items():
            q = q.where(getattr(model, k) == v)
        return (await s.execute(q)).scalar_one()


async def test_boot_writes_one_process_start_per_feed(factory, one_feed):
    await feed_store.record_boot(factory, T0)
    async with factory() as s:
        rows = (await s.execute(select(FeedTransition))).scalars().all()
    assert [(r.feed, r.from_state, r.to_state) for r in rows] == [("aircraft", None, "process_start")]


async def test_a_flush_writes_a_heartbeat_a_transition_and_the_current_facts(factory, one_feed):
    one_feed.succeeded(T0, count=3, source="adsb.lol", source_epoch=T0)
    one_feed.failed(T0 + 15, ErrorKind.TIMEOUT, "adsb.lol ReadTimeout")
    states = await feed_store.flush(factory, T0 + 20, {"aircraft": "process_start"})
    assert states == {"aircraft": "retrying"}

    async with factory() as s:
        hb = (await s.execute(select(FeedHeartbeat))).scalar_one()
        tr = (await s.execute(select(FeedTransition))).scalar_one()
        fh = (await s.execute(select(FeedHealth))).scalar_one()
    assert (hb.state, hb.attempts, hb.successes, hb.failures) == ("retrying", 2, 1, {"timeout": 1})
    assert (tr.from_state, tr.to_state, tr.error_kind) == ("process_start", "retrying", "timeout")
    assert fh.state == "retrying"
    assert fh.last_success_at == datetime.datetime.fromtimestamp(T0, tz=datetime.timezone.utc)
    assert fh.error_detail == "adsb.lol ReadTimeout"


async def test_an_unchanged_state_adds_a_heartbeat_but_no_transition(factory, one_feed):
    one_feed.succeeded(T0, count=3, source="adsb.lol", source_epoch=T0)
    states = await feed_store.flush(factory, T0 + 1, {"aircraft": "live"})
    await feed_store.flush(factory, T0 + 2, states)
    assert await _count(factory, FeedTransition) == 0
    assert await _count(factory, FeedHeartbeat) == 2
    assert await _count(factory, FeedHealth) == 1  # upserted, not appended


async def test_a_flush_drains_the_minute_counters(factory, one_feed):
    one_feed.succeeded(T0, count=3, source="adsb.lol", source_epoch=T0)
    await feed_store.flush(factory, T0 + 1, {})
    await feed_store.flush(factory, T0 + 61, {"aircraft": "live"})
    async with factory() as s:
        attempts = [r for (r,) in (await s.execute(select(FeedHeartbeat.attempts).order_by(FeedHeartbeat.id)))]
    assert attempts == [1, 0]


async def test_a_failed_flush_loses_nothing(factory, one_feed):
    """The counters are drained only after a commit, so the minute a database
    error swallowed is carried by the next heartbeat."""
    one_feed.succeeded(T0, count=3, source="adsb.lol", source_epoch=T0)

    def broken():
        raise RuntimeError("database down")

    with pytest.raises(RuntimeError):
        await feed_store.flush(broken, T0 + 1, {})
    one_feed.succeeded(T0 + 15, count=3, source="adsb.lol", source_epoch=T0 + 15)
    await feed_store.flush(factory, T0 + 61, {})
    async with factory() as s:
        hb = (await s.execute(select(FeedHeartbeat))).scalar_one()
    assert (hb.attempts, hb.successes) == (2, 2)


async def test_the_flusher_survives_a_database_error_and_keeps_flushing(factory, one_feed):
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 2:  # boot succeeds, the first flush fails
            raise RuntimeError("database down")
        return factory()

    class _Stop(Exception):
        pass

    slept = []

    async def sleep(s):
        slept.append(s)
        if len(slept) > 3:
            raise _Stop

    clock = iter([T0, T0 + 60, T0 + 120, T0 + 180, T0 + 240])
    with pytest.raises(_Stop):
        await feed_store.start_feed_flusher(flaky, sleep=sleep, clock=lambda: next(clock))
    assert await _count(factory, FeedTransition, to_state="process_start") == 1
    assert await _count(factory, FeedHeartbeat) == 2  # three flushes, one failed


async def test_prune_deletes_old_heartbeats_and_keeps_transitions(factory, one_feed):
    old = T0 - (feed_store.RETENTION_DAYS + 1) * 86400
    await feed_store.flush(factory, old, {})
    await feed_store.flush(factory, T0, {})
    transitions_before = await _count(factory, FeedTransition)
    assert await feed_store.prune(factory, T0) == 1
    assert await _count(factory, FeedHeartbeat) == 1
    assert await _count(factory, FeedTransition) == transitions_before


async def _heartbeats(factory, *pairs):
    async with factory() as s:
        for t, state in pairs:
            s.add(FeedHeartbeat(feed="aircraft", at=feed_store._ts(t), state=state,
                                attempts=0, successes=0, failures={}))
        await s.commit()


async def test_a_gap_between_heartbeats_is_unknown_not_the_last_state(factory, one_feed):
    """The monitor stopped recording between T0+120 and T0+400. The last state
    before the gap was live; the timeline must not say so for the gap."""
    await _heartbeats(factory, (T0, "live"), (T0 + 60, "live"), (T0 + 120, "live"),
                      (T0 + 400, "retrying"), (T0 + 460, "retrying"))
    async with factory() as s:
        segs = await feed_store.liveness_timeline(s, "aircraft", T0 - 60, T0 + 460)
    assert [(s["state"], s["start"] - T0, s["end"] - T0) for s in segs] == [
        ("live", -60, 120),
        ("unknown", 120, 340),
        ("retrying", 340, 460),
    ]


async def test_no_heartbeats_at_all_is_unknown_for_the_whole_window(factory, one_feed):
    async with factory() as s:
        segs = await feed_store.liveness_timeline(s, "aircraft", T0, T0 + 3600)
    assert segs == [{"start": T0, "end": T0 + 3600, "state": "unknown"}]


async def test_silence_after_the_last_heartbeat_becomes_unknown(factory, one_feed):
    await _heartbeats(factory, (T0, "live"))
    async with factory() as s:
        segs = await feed_store.liveness_timeline(s, "aircraft", T0 - 60, T0 + 600)
    assert [s["state"] for s in segs] == ["live", "unknown"]
    assert segs[1]["start"] == T0
