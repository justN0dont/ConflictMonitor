"""The AIS feed: what a missing key, a silent socket and a refused key look like.

Closes the untriaged "maritime reconnect" item (docs/FINDINGS.md, Collection:
"maritime.py returns from _run_websocket on any server error frame and
start_maritime_poller reconnects every 10s forever with no backoff and no
surfaced state"). The connection loop is driven for real through an injected
`connect`, `sleep` and `clock`: no socket, no network, no waiting.
"""

import asyncio
import json
from datetime import datetime, timezone

import pytest

from app import feeds
from app.config import settings
from app.feeds import ErrorKind, FeedState
from app.services import maritime

T0 = 1_800_000_000.0


@pytest.fixture
def fresh(monkeypatch):
    tracker = feeds.FeedTracker(feeds.REGISTRY["vessels"])
    monkeypatch.setitem(feeds.TRACKERS, "vessels", tracker)
    monkeypatch.setattr(maritime, "_cache", {"vessels": {}, "last_update": 0})
    monkeypatch.setattr(maritime, "record_vessel_position", lambda *a: None)
    monkeypatch.setattr(settings, "aisstream_api_key", "test-key")
    return tracker


def _position(mmsi="123456789", lat=26.5, lon=56.3, time_utc="2027-01-15 08:00:00.000000 +0000 UTC"):
    return {"MessageType": "PositionReport",
            "MetaData": {"MMSI": mmsi, "ShipName": "TEST", "time_utc": time_utc},
            "Message": {"PositionReport": {"Latitude": lat, "Longitude": lon, "Sog": 10}}}


# ── handle_message: what each frame is ───────────────────────────────────────

def test_a_usable_position_is_accepted_and_cached(fresh):
    assert maritime.handle_message(_position(), T0) == "accepted"
    v = maritime._cache["vessels"]["123456789"]
    assert (v["lat"], v["lon"], v["last_seen"]) == (26.5, 56.3, T0)


def test_a_null_island_or_positionless_report_is_not_accepted(fresh):
    assert maritime.handle_message(_position(lat=0, lon=0), T0) == "ignored"
    assert maritime.handle_message({"MessageType": "PositionReport", "MetaData": {}}, T0) == "ignored"
    assert maritime._cache["vessels"] == {}


def test_an_error_frame_is_reported_not_swallowed(fresh):
    assert maritime.handle_message({"error": "Api Key Is Not Valid"}, T0) == "error:Api Key Is Not Valid"


def test_upstream_time_is_parsed_and_garbage_is_none_not_receipt_time():
    whole = datetime(2027, 1, 15, 8, 0, 0, tzinfo=timezone.utc).timestamp()
    assert maritime._parse_time_utc("2027-01-15 08:00:00.123456 +0000 UTC") == pytest.approx(whole + 0.123456)
    assert maritime._parse_time_utc("2027-01-15 08:00:00 +0000 UTC") == whole
    assert maritime._parse_time_utc("yesterday") is None
    assert maritime._parse_time_utc(None) is None


@pytest.mark.parametrize("text, kind", [
    ("Api Key Is Not Valid", ErrorKind.AUTH_FAILED),
    ("API key required", ErrorKind.AUTH_FAILED),
    ("Unauthorized", ErrorKind.AUTH_FAILED),
    ("Error reading from socket", ErrorKind.FETCH_ERROR),
])
def test_key_errors_are_auth_and_everything_else_is_transport(text, kind):
    assert maritime.classify_error_frame(text) is kind


def test_the_backoff_ladder_and_the_hourly_auth_probe():
    delays = [maritime.next_delay(ErrorKind.FETCH_ERROR, n) for n in range(1, 8)]
    assert delays == [5, 15, 60, 300, 900, 900, 900]
    assert maritime.next_delay(ErrorKind.AUTH_FAILED, 1) == 3600


def test_transport_failures_stay_under_ten_attempts_an_hour():
    """The old loop made 360 attempts an hour, 6 a minute, forever."""
    elapsed, attempts = 0, 0
    while elapsed < 3600:
        attempts += 1
        elapsed += maritime.next_delay(ErrorKind.FETCH_ERROR, attempts)
    assert attempts < 10


# ── The loop, with a fake socket ─────────────────────────────────────────────

class _Stop(Exception):
    pass


class _FakeWS:
    def __init__(self, frames, then_silent=True):
        self.frames = list(frames)
        self.then_silent = then_silent
        self.sent = []

    async def send(self, msg):
        self.sent.append(msg)

    async def recv(self):
        if self.frames:
            return json.dumps(self.frames.pop(0))
        await asyncio.Event().wait()  # open, answering pings, sending nothing


class _Connect:
    """Stands in for websockets.connect: one scripted socket (or exception) per call."""

    def __init__(self, *sockets):
        self.sockets = list(sockets)
        self.calls = 0

    def __call__(self, url, **kw):
        self.calls += 1
        item = self.sockets.pop(0) if self.sockets else OSError("connection refused")

        class _Ctx:
            async def __aenter__(self_inner):
                if isinstance(item, Exception):
                    raise item
                return item

            async def __aexit__(self_inner, *exc):
                return False

        return _Ctx()


async def _run(monkeypatch, connect, cycles, clock=lambda: T0):
    """Run the real poller until `cycles` reconnect waits have happened."""
    slept = []

    async def sleep(seconds):
        slept.append(seconds)
        if len(slept) >= cycles:
            raise _Stop

    with pytest.raises(_Stop):
        await maritime.start_maritime_poller(connect=connect, sleep=sleep, clock=clock)
    return slept


async def test_no_key_is_unconfigured_and_never_an_empty_sea(fresh, monkeypatch):
    monkeypatch.setattr(settings, "aisstream_api_key", "")
    await maritime.start_maritime_poller(connect=_Connect())
    env = maritime.get_vessels_envelope()
    assert env["state"] == "unconfigured"
    assert env["count"] is None
    assert env["verdict"] == "unproven"
    assert "AISSTREAM_API_KEY" in env["reason"]
    assert feeds.health(T0, T0)["not_collected"][0]["missing_env"] == ["AISSTREAM_API_KEY"]


async def test_an_open_but_silent_socket_is_recycled(fresh, monkeypatch):
    """PHASE2-8: ping keeps a silent socket open forever, so every read has a deadline."""
    monkeypatch.setattr(maritime, "RECV_DEADLINE_S", 0.01)
    # Bounded, so a missing read deadline FAILS this test instead of hanging the
    # suite: without one, the reader waits on the silent socket forever.
    slept = await asyncio.wait_for(_run(monkeypatch, _Connect(_FakeWS([])), cycles=1), 5)
    assert fresh.error_kind is ErrorKind.TIMEOUT
    assert "recycled" in fresh.error_detail
    assert slept == [5]


async def test_a_key_error_frame_is_auth_failed_and_waits_an_hour(fresh, monkeypatch):
    slept = await _run(monkeypatch, _Connect(_FakeWS([{"error": "Api Key Is Not Valid"}])), cycles=1)
    assert fresh.state(T0) is FeedState.AUTH_FAILED
    assert slept == [3600]


async def test_repeated_connect_failures_climb_the_ladder(fresh, monkeypatch):
    slept = await _run(monkeypatch, _Connect(), cycles=6)
    assert slept == [5, 15, 60, 300, 900, 900]
    assert fresh.consecutive_failures == 6


async def test_positions_make_the_feed_live_and_reset_the_ladder(fresh, monkeypatch):
    monkeypatch.setattr(maritime, "RECV_DEADLINE_S", 0.01)
    t_fix = maritime._parse_time_utc("2027-01-15 08:00:00 +0000 UTC")
    connect = _Connect(OSError("down"), _FakeWS([_position()]))
    slept = await asyncio.wait_for(_run(monkeypatch, connect, cycles=2, clock=lambda: t_fix + 5), 5)
    # The first attempt failed (5 s); the second delivered a position, then went
    # silent and was recycled: one failure after a success is the FIRST rung again.
    assert slept == [5, 5]
    assert fresh.last_success_at == t_fix + 5
    assert fresh.source_epoch == t_fix
    assert fresh.count == 1


async def test_an_outage_keeps_the_rows_as_last_good_but_never_live(fresh, monkeypatch):
    """A 700 s outage: rows stay (aged against the last success, not the wall
    clock), the state is not live, and nothing prints a count."""
    maritime.handle_message(_position(), T0 - 700)
    fresh.succeeded(T0 - 700, count=1, source="aisstream", source_epoch=T0 - 700)
    fresh.failed(T0 - 690, ErrorKind.FETCH_ERROR, "socket closed")
    monkeypatch.setattr(maritime.time, "time", lambda: T0)
    env = maritime.get_vessels_envelope()
    assert env["state"] == "unavailable"
    assert env["count"] is None
    assert len(env["items"]) == 1
