"""The two reads behind the timeline axis, against a real Postgres.

C81 (docs/FINDINGS.md): the timeline's axis spanned the whole table while its
ticks came from the 200 events the client held, so on the archive most of the
axis drew as quiet. /events/density is the whole table's shape; /health/lane is
whether the monitor was listening while it was quiet.
"""

import datetime

import pytest
from fastapi import HTTPException

from app import feeds
from app.feed_store import NOT_RETAINED, UNKNOWN, liveness_lane
from app.models import FeedHeartbeat, FeedTransition
from app.routes.events import MAX_DENSITY_BUCKETS, event_density

from tests.db.conftest import NOW, make_event

UTC = datetime.timezone.utc
H = datetime.timedelta(hours=1)


async def _density(session, **kw):
    kw.setdefault("bucket_s", 3600)
    kw.setdefault("after", None)
    kw.setdefault("before", None)
    return await event_density(session=session, **kw)


async def test_density_counts_every_row_and_places_only_located_ones(session):
    session.add_all([
        make_event("placed, 10:05", lat=33.5, lon=36.3, when=NOW + 5 * H / 60),
        make_event("unplaced, 10:40", when=NOW + 40 * H / 60),
        make_event("placed, 11:20", lat=31.0, lon=35.0, when=NOW + H + 20 * H / 60),
    ])
    await session.flush()

    out = await _density(session)

    assert out["bucket_s"] == 3600
    assert [(b["total"], b["located"]) for b in out["buckets"]] == [(2, 1), (1, 1)]
    assert [b["at"] for b in out["buckets"]] == [NOW.isoformat(), (NOW + H).isoformat()]
    # The echoed span is the table's own bounds, not the bucket edges.
    assert out["after"] == (NOW + 5 * H / 60).isoformat()
    assert out["before"] == (NOW + H + 20 * H / 60).isoformat()


async def test_density_omits_empty_buckets_rather_than_writing_zero(session):
    session.add_all([make_event("a", when=NOW), make_event("b", when=NOW + 5 * H)])
    await session.flush()
    out = await _density(session)
    assert [b["at"] for b in out["buckets"]] == [NOW.isoformat(), (NOW + 5 * H).isoformat()]


async def test_density_over_an_empty_table_says_so(session):
    out = await _density(session)
    assert out == {"bucket_s": 3600, "after": None, "before": None, "buckets": []}


async def test_density_narrows_to_after_and_before_and_takes_naive_as_utc(session):
    session.add_all([make_event("a", when=NOW), make_event("b", when=NOW + 5 * H)])
    await session.flush()
    naive_after = (NOW + 4 * H).replace(tzinfo=None)
    out = await _density(session, after=naive_after)
    assert [b["total"] for b in out["buckets"]] == [1]
    assert out["after"] == (NOW + 4 * H).isoformat()


async def test_density_refuses_a_span_it_cannot_answer_in_one_body(session):
    with pytest.raises(HTTPException) as exc:
        await _density(session, bucket_s=60,
                       after=NOW, before=NOW + (MAX_DENSITY_BUCKETS + 1) * datetime.timedelta(minutes=1))
    assert exc.value.status_code == 422
    assert "bucket_s must be at least" in exc.value.detail


# ── the lane ──────────────────────────────────────────────────────────────

T0 = 1_800_000_000  # a multiple of 3600


def _at(epoch):
    return datetime.datetime.fromtimestamp(epoch, tz=UTC)


async def test_lane_aggregates_heartbeats_per_bucket_with_the_worst_state(session):
    feed = "telegram"
    session.add(FeedTransition(feed=feed, at=_at(T0), from_state=None, to_state="process_start"))
    for m in range(60):
        session.add(FeedHeartbeat(feed=feed, at=_at(T0 + 60 * m), state="live",
                                  attempts=1, successes=1, failures={}))
    for m in range(60):
        state = "retrying" if m == 30 else "live"
        session.add(FeedHeartbeat(feed=feed, at=_at(T0 + 3600 + 60 * m), state=state,
                                  attempts=1, successes=0 if m == 30 else 1, failures={}))
    await session.flush()

    out = await liveness_lane(session, feed, T0, T0 + 4 * 3600, 3600, now=T0 + 5 * 3600)

    assert [b["state"] for b in out] == ["live", "retrying", UNKNOWN, UNKNOWN]
    assert [(b["attempts"], b["successes"]) for b in out][:2] == [(60, 60), (60, 59)]


async def test_lane_tells_pruned_from_never_recorded(session):
    """Before the first transition the feed did not exist: unknown. After it,
    an empty stretch older than retention is not_retained."""
    feed = "telegram"
    session.add(FeedTransition(feed=feed, at=_at(T0 + 3600), from_state=None, to_state="process_start"))
    await session.flush()
    now = T0 + 3600 * 5 + 30 * 86400  # retention cutoff lands at T0 + 5h
    out = await liveness_lane(session, feed, T0, T0 + 7 * 3600, 3600, now=now)
    assert [b["state"] for b in out] == [
        UNKNOWN, NOT_RETAINED, NOT_RETAINED, NOT_RETAINED, NOT_RETAINED, UNKNOWN, UNKNOWN,
    ]


async def test_lane_knows_every_registered_feed_and_region():
    """The route 404s on a feed the registry does not know, so a typo cannot
    draw an all-unknown lane that looks like an outage."""
    from app.main import health_lane

    with pytest.raises(HTTPException) as exc:
        await health_lane(feed="telegrams", after=T0, before=T0 + 3600)
    assert exc.value.status_code == 404
    assert "vessels:gulf" in feeds.all_trackers()
    with pytest.raises(HTTPException) as exc:
        await health_lane(feed="telegram", after=T0, before=T0 + 3600 * (5001), bucket_s=3600)
    assert exc.value.status_code == 422
    assert "bucket_s must be at least 3601" in exc.value.detail
