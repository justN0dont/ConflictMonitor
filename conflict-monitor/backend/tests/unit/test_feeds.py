"""feed_health's vocabulary and state machine (app/feeds.py).

Pins the decisions recorded in docs/FINDINGS.md, Roadmap > Phase 2 >
"feed_health — schema decisions": the worst-first order, unconfigured outside
the roll-up, state derived from three clocks against an injected `now`, a
failure never advancing the success clocks, and a missing upstream time never
replaced by receipt time. Everything here is pure; no test sleeps.
"""

import asyncio

import pytest

from app import feeds
from app.feeds import ErrorKind, FeedSpec, FeedState, FeedTracker

SPEC = FeedSpec(id="t", cadence_s=15, stale_after_s=45, max_stale_s=300,
                requires_env=(), silence_means="test")
T0 = 1_800_000_000.0


def _tracker(**kw):
    return FeedTracker(SPEC, **kw)


def _live(t=None, now=T0):
    t = t or _tracker()
    t.succeeded(now, count=5, source="up", source_epoch=now)
    return t


# ── The vocabulary ───────────────────────────────────────────────────────────

def test_the_severity_order_is_the_declaration_order_worst_first():
    """Reordering the enum silently changes every roll-up, so the order is pinned."""
    assert [s.value for s in FeedState] == [
        "dead", "auth_failed", "unavailable", "retrying", "stale", "pending", "live",
        "unconfigured",
    ]
    assert set(feeds.RANK) == set(FeedState) - {FeedState.UNCONFIGURED}


def test_the_roll_up_reports_the_worst_member_and_ignores_unconfigured():
    assert feeds.worst([FeedState.LIVE, FeedState.STALE, FeedState.RETRYING]) is FeedState.RETRYING
    assert feeds.worst([FeedState.LIVE, FeedState.UNCONFIGURED]) is FeedState.LIVE
    assert feeds.worst([FeedState.UNCONFIGURED]) is None


def test_the_roll_up_raises_on_a_value_it_does_not_rank():
    """GEV's roll-up silently skips its unranked `partial` (docs/GODS-EYE-VIEW.md).
    Here an unknown value is an error, not a gap."""
    with pytest.raises(KeyError):
        feeds.worst([FeedState.LIVE, "partial"])


def test_success_and_failure_kinds_cannot_be_crossed():
    t = _tracker()
    with pytest.raises(ValueError):
        t.succeeded(T0, count=0, source="up", source_epoch=T0, kind=ErrorKind.TIMEOUT)
    with pytest.raises(ValueError):
        t.failed(T0, ErrorKind.EMPTY)


# ── The state machine, one reachable path per state ──────────────────────────

def test_a_configured_feed_that_has_never_been_polled_is_pending():
    t = _tracker()
    assert t.state(T0) is FeedState.PENDING
    assert t.reason(T0) == "not yet polled"


def test_a_success_by_a_current_upstream_clock_is_live():
    assert _live().state(T0 + 10) is FeedState.LIVE


def test_one_failure_after_a_success_is_retrying_and_keeps_the_success_clocks():
    """C31: the failure is recorded, and it advances nothing that claims data."""
    t = _live()
    t.failed(T0 + 15, ErrorKind.TIMEOUT, "adsb.lol ReadTimeout")
    assert t.state(T0 + 15) is FeedState.RETRYING
    assert t.last_attempt_at == T0 + 15
    assert t.last_success_at == T0
    assert t.source_epoch == T0
    assert t.reason(T0 + 15) == "timeout: adsb.lol ReadTimeout"


def test_no_success_for_longer_than_max_stale_is_unavailable():
    t = _live()
    t.failed(T0 + 15, ErrorKind.FETCH_ERROR)
    assert t.state(T0 + SPEC.max_stale_s + 1) is FeedState.UNAVAILABLE


def test_a_feed_that_never_succeeded_becomes_unavailable_not_pending_forever():
    t = _tracker()
    t.failed(T0, ErrorKind.FETCH_ERROR, "ConnectionClosedError")
    assert t.state(T0 + 10) is FeedState.PENDING
    # Polled and failing is not "not yet polled": the reason names the failure.
    assert t.reason(T0 + 10) == "no success yet · fetch_error: ConnectionClosedError"
    assert t.state(T0 + SPEC.max_stale_s + 1) is FeedState.UNAVAILABLE


def test_an_upstream_that_gives_no_time_is_stale_never_live():
    """Receipt time is never substituted for a missing upstream time."""
    t = _tracker()
    t.succeeded(T0, count=5, source="up", source_epoch=None)
    assert t.source_epoch is None
    assert t.freshness(T0) == "unknown"
    assert t.state(T0) is FeedState.STALE
    assert t.reason(T0) == "upstream gave no timestamp"


def test_successful_polls_of_old_upstream_data_are_stale():
    t = _tracker()
    t.succeeded(T0, count=5, source="up", source_epoch=T0 - SPEC.stale_after_s - 1)
    assert t.state(T0) is FeedState.STALE


def test_an_upstream_clock_far_ahead_of_ours_is_not_believed():
    t = _tracker()
    t.succeeded(T0, count=5, source="up", source_epoch=T0 + feeds.CLOCK_SKEW_S + 1)
    assert t.freshness(T0) == "unknown"
    assert t.state(T0) is FeedState.STALE
    assert t.reason(T0) == "clock_skew"


def test_a_refused_credential_is_auth_failed():
    t = _live()
    t.failed(T0 + 15, ErrorKind.AUTH_FAILED, "adsb.lol HTTP 403")
    assert t.state(T0 + 15) is FeedState.AUTH_FAILED


async def test_an_exited_poller_task_is_dead_whatever_its_last_success():
    """A poller that crashed stops calling the tracker; without this its last
    state would read live until max_stale ran out."""
    task = asyncio.get_running_loop().create_future()
    task.set_result(None)
    t = _live()
    t.task = task
    assert t.state(T0 + 1) is FeedState.DEAD


def test_a_feed_missing_its_key_is_unconfigured():
    assert _tracker(configured=False).state(T0) is FeedState.UNCONFIGURED


def test_every_state_is_reachable():
    """A state no path produces would be a lie in the enum. The tests above
    reach each one; this lists them so a new member cannot slip in unreached."""
    reached = {
        _tracker().state(T0),                               # pending
        _live().state(T0),                                  # live
        _tracker(configured=False).state(T0),               # unconfigured
    }
    t = _live(); t.failed(T0 + 1, ErrorKind.TIMEOUT); reached.add(t.state(T0 + 1))          # retrying
    reached.add(t.state(T0 + SPEC.max_stale_s + 1))                                          # unavailable
    t = _live(); t.failed(T0 + 1, ErrorKind.AUTH_FAILED); reached.add(t.state(T0 + 1))      # auth_failed
    t = _tracker(); t.succeeded(T0, count=1, source="up", source_epoch=None); reached.add(t.state(T0))  # stale
    done = type("Done", (), {"done": lambda self: True})()
    t = _live(); t.task = done; reached.add(t.state(T0))                                     # dead
    assert reached == set(FeedState)


def test_error_detail_never_carries_a_query_string():
    t = _tracker()
    t.failed(T0, ErrorKind.HTTP_ERROR, "GET https://x.example/api?key=SECRET&q=1 -> 500")
    assert "SECRET" not in t.error_detail
    assert len(t.error_detail) <= feeds.DETAIL_MAX


# ── The envelope and /health ─────────────────────────────────────────────────

@pytest.fixture
def aircraft_tracker(monkeypatch):
    t = FeedTracker(feeds.REGISTRY["aircraft"])
    monkeypatch.setitem(feeds.TRACKERS, "aircraft", t)
    return t


def test_a_retrying_feed_keeps_its_items_but_prints_no_count(aircraft_tracker):
    aircraft_tracker.succeeded(T0, count=2, source="adsb.lol", source_epoch=T0)
    aircraft_tracker.failed(T0 + 15, ErrorKind.TIMEOUT)
    env = feeds.envelope("aircraft", [{"icao24": "a"}, {"icao24": "b"}], T0 + 15)
    assert env["state"] == "retrying"
    assert env["count"] is None
    assert env["verdict"] == "unproven"
    assert len(env["items"]) == 2
    assert env["age_s"] == 15.0


def test_a_live_feed_counts_and_a_present_count_is_present(aircraft_tracker):
    aircraft_tracker.succeeded(T0, count=2, source="adsb.lol", source_epoch=T0)
    env = feeds.envelope("aircraft", [{}, {}], T0 + 1)
    assert (env["state"], env["count"], env["verdict"]) == ("live", 2, "present")
    assert env["schema_version"] == 1


def test_demo_data_says_it_is_synthetic(aircraft_tracker):
    aircraft_tracker.succeeded(T0, count=1, source="demo", source_epoch=T0, synthetic=True)
    assert feeds.envelope("aircraft", [{}], T0)["synthetic"] is True
    assert feeds.health(T0, T0 - 5)["synthetic"] is True


def test_health_rolls_up_configured_feeds_and_lists_the_unconfigured(monkeypatch):
    live = _live(FeedTracker(feeds.REGISTRY["aircraft"]))
    keyed = FeedTracker(FeedSpec(id="ais", cadence_s=10, stale_after_s=120, max_stale_s=600,
                                 requires_env=("AISSTREAM_API_KEY",), silence_means="x"),
                        configured=False)
    monkeypatch.setattr(feeds, "TRACKERS", {"aircraft": live, "ais": keyed})
    h = feeds.health(T0 + 1, T0 - 60)
    assert h["worst"] == "live"          # the unconfigured feed does not make worst "down"
    assert h["not_collected"] == [{"feed": "ais", "missing_env": ["AISSTREAM_API_KEY"],
                                   "reason": "set AISSTREAM_API_KEY"}]
    assert h["feeds"]["ais"]["state"] == "unconfigured"
