"""Per-region currency: a live feed does not vouch for waters it is not seeing.

The roadmap line (docs/FINDINGS.md, Phase 2): "a live socket delivering the
wrong ocean still reads GREEN". One AISStream subscription delivers the western
Mediterranean at hundreds of positions a minute while its Gulf has no receivers
(C30), and until this every one of those positions kept the whole vessel
picture LIVE. A region is a sub-tracker: only positions inside its box move its
clocks, its upstream clock is the position's own time, and it is never shown
better than its feed.
"""

import asyncio
from datetime import datetime, timezone

import pytest

from app import feeds
from app.config import settings
from app.feeds import ErrorKind, FeedState
from app.services import maritime
from tests.unit.test_maritime import _Connect, _FakeWS, _position, _run

T0 = 1_800_000_000.0
GULF = feeds.REGIONS["vessels:gulf"]
LEVANT = feeds.REGIONS["vessels:levant"]
WC_MED = (38.0, 12.5)   # off Sicily
LEVANT_AT = (33.0, 34.5)  # off Haifa


def _utc(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f +0000 UTC")


@pytest.fixture
def fresh(monkeypatch):
    tracker = feeds.FeedTracker(feeds.REGISTRY["vessels"])
    monkeypatch.setitem(feeds.TRACKERS, "vessels", tracker)
    monkeypatch.setattr(feeds, "REGION_TRACKERS",
                        {rid: feeds.RegionTracker(spec) for rid, spec in feeds.REGIONS.items()})
    monkeypatch.setattr(maritime, "_cache", {"vessels": {}, "last_update": 0})
    monkeypatch.setattr(maritime, "record_vessel_position", lambda *a: None)
    monkeypatch.setattr(settings, "aisstream_api_key", "test-key")
    return tracker


def _feed(now: float, *positions) -> None:
    """What the poller does per accepted frame: cache it, then tell the feed."""
    for i, (lat, lon, observed) in enumerate(positions):
        pos = _position(mmsi=str(200000000 + i), lat=lat, lon=lon,
                        time_utc=_utc(observed) if observed is not None else "garbage")
        assert maritime.handle_message(pos, now) == "accepted"
    vessels = maritime.get_vessels()
    newest = max((v.get("observed_at") or 0 for v in vessels), default=0) or None
    feeds.TRACKERS["vessels"].succeeded(now, count=len(vessels), source="aisstream", source_epoch=newest)


def _arm(now: float) -> None:
    for r in feeds.regions("vessels"):
        r.arm(now)


def _region(env: dict, key: str) -> dict:
    return next(r for r in env["regions"] if r["key"] == key)


def _envelope(monkeypatch, now: float) -> dict:
    monkeypatch.setattr(maritime.time, "time", lambda: now)
    return maritime.get_vessels_envelope()


# ── Where a position belongs ─────────────────────────────────────────────────

@pytest.mark.parametrize("lat, lon, key", [
    (26.5, 56.3, "gulf"),          # Strait of Hormuz
    (27.0, 50.5, "gulf"),          # off Bahrain
    (20.0, 38.5, "red_sea"),
    (12.5, 45.0, "red_sea"),       # Gulf of Aden
    (33.0, 34.5, "levant"),
    (38.0, 12.5, "wc_med"),
    (35.0, 25.0, "e_med"),         # on the seam between the two boxes: first match wins
    (33.3, 44.4, None),            # Baghdad: in the subscription, in no sea
])
def test_a_position_belongs_to_the_first_region_holding_it(lat, lon, key):
    assert feeds.region_of("vessels", lat, lon) == key


# ── The roadmap line ─────────────────────────────────────────────────────────

def test_a_live_socket_delivering_the_wrong_ocean_does_not_vouch_for_the_gulf(fresh, monkeypatch):
    _arm(T0)
    later = T0 + GULF.max_stale_s + 1
    _feed(later, *[(*WC_MED, later - 2)] * 3)

    env = _envelope(monkeypatch, later)
    assert env["state"] == "live"          # the socket IS delivering
    gulf = _region(env, "gulf")
    assert gulf["state"] == "unavailable"  # ...but not from here
    assert gulf["count"] is None
    assert "since the subscription opened" in gulf["reason"]
    assert "known gap" in gulf["reason"]
    assert _region(env, "wc_med")["state"] == "live"
    assert _region(env, "wc_med")["count"] == 3


async def test_the_subscription_arms_every_region(fresh, monkeypatch):
    """Silence is counted from the moment the socket subscribed, by the real loop."""
    monkeypatch.setattr(maritime, "RECV_DEADLINE_S", 0.01)
    connect = _Connect(_FakeWS([_position(lat=WC_MED[0], lon=WC_MED[1], time_utc=_utc(T0))]))
    await asyncio.wait_for(_run(monkeypatch, connect, cycles=1, clock=lambda: T0 + 3), 5)
    assert {r.first_attempt_at for r in feeds.regions("vessels")} == {T0 + 3}
    assert feeds.REGION_TRACKERS["vessels:wc_med"].last_success_at == T0 + 3
    assert feeds.REGION_TRACKERS["vessels:gulf"].last_success_at is None


# ── A region is never better than its feed ───────────────────────────────────

def test_a_region_is_never_better_than_its_feed(fresh, monkeypatch):
    _arm(T0)
    _feed(T0, (*LEVANT_AT, T0 - 2))
    fresh.failed(T0 + 1, ErrorKind.TIMEOUT, "no message for 300 s; socket recycled")
    env = _envelope(monkeypatch, T0 + 2)
    levant = _region(env, "levant")
    assert levant["state"] == "retrying"
    assert levant["reason"].startswith("feed retrying: timeout")
    assert levant["count"] is None


def test_an_unconfigured_feed_leaves_every_region_unconfigured(fresh, monkeypatch):
    fresh.configured = False
    assert {r.state(T0).value for r in feeds.regions("vessels")} == {"unconfigured"}


# ── The region's clock is upstream time, never receipt time ──────────────────

def test_the_region_clock_is_the_positions_own_time(fresh, monkeypatch):
    """A fresh Mediterranean keeps the FEED live; an old fix off Haifa leaves
    the Levant stale, though it was received this second."""
    _arm(T0)
    now = T0 + 60
    _feed(now, (*WC_MED, now - 2), (*LEVANT_AT, now - LEVANT.stale_after_s - 60))
    env = _envelope(monkeypatch, now)
    assert env["state"] == "live"
    levant = _region(env, "levant")
    assert levant["state"] == "stale"
    assert levant["freshness"] == "old"
    # Stale rows are real rows: counted, and said to be old.
    assert levant["count"] == 1


def test_a_position_without_a_time_does_not_refresh_the_region(fresh, monkeypatch):
    _arm(T0)
    now = T0 + 60
    _feed(now, (*WC_MED, now - 2), (*LEVANT_AT, now - LEVANT.stale_after_s - 60))
    # A second Levant fix, received now, with an unparseable time_utc.
    pos = _position(mmsi="299999999", lat=LEVANT_AT[0], lon=LEVANT_AT[1], time_utc="garbage")
    assert maritime.handle_message(pos, now + 1) == "accepted"
    assert _region(_envelope(monkeypatch, now + 1), "levant")["state"] == "stale"


def test_a_region_that_never_gave_a_time_is_stale_not_live(fresh, monkeypatch):
    _arm(T0)
    _feed(T0, (*WC_MED, T0 - 2), (*LEVANT_AT, None))
    levant = _region(_envelope(monkeypatch, T0), "levant")
    assert levant["state"] == "stale"
    assert levant["reason"] == "upstream gave no timestamp"


# ── Counts and roll-ups ──────────────────────────────────────────────────────

def test_region_counts_are_the_rows_served(fresh, monkeypatch):
    _arm(T0)
    _feed(T0, (*LEVANT_AT, T0 - 2), (33.1, 34.6, T0 - 2), (*WC_MED, T0 - 2), (33.3, 44.4, T0 - 2))
    env = _envelope(monkeypatch, T0)
    assert env["count"] == 4                       # the feed counts every row
    assert _region(env, "levant")["count"] == 2
    assert _region(env, "wc_med")["count"] == 1
    assert _region(env, "gulf")["count"] is None   # pending: nothing to vouch for
    assert sum(r["count"] or 0 for r in env["regions"]) == 3  # Baghdad is in no region


def test_regions_stay_out_of_the_health_rollup(fresh, monkeypatch):
    """A dark region is a coverage fact, not a failing collector: it is listed
    under its feed and does not drag /health's `worst` down."""
    monkeypatch.setattr(feeds, "TRACKERS", {"vessels": fresh})
    _arm(T0)
    later = T0 + GULF.max_stale_s + 1
    _feed(later, (*WC_MED, later - 2))
    health = feeds.health(later, T0)
    gulf = next(r for r in health["feeds"]["vessels"]["regions"] if r["key"] == "gulf")
    assert gulf["state"] == "unavailable"
    assert health["worst"] == FeedState.LIVE


def test_regions_are_flushed_only_with_their_feed(fresh, monkeypatch):
    assert {"vessels:gulf", "vessels:wc_med"} <= set(feeds.all_trackers())
    monkeypatch.setattr(feeds, "TRACKERS", {"aircraft": feeds.FeedTracker(feeds.REGISTRY["aircraft"])})
    assert set(feeds.all_trackers()) == {"aircraft"}
