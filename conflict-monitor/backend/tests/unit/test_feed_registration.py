"""Connectivity, RSS and Telegram in feed_health.

Closes the Phase 2 feed_health line (docs/FINDINGS.md, Roadmap > Phase 2):
after aircraft, vessels and satellites, these three collectors now report
every attempt to a tracker, so /health covers all six and none of them can go
quiet while reading as calm.
"""

import asyncio
from datetime import datetime, timezone

import httpx
import pytest

from app import feeds
from app.feeds import ErrorKind, FeedState, FeedTracker
from app.services import connectivity as conn
from app.services import news_feeds as rss
from app.services import telegram as tg

NOW = 1_800_000_000.0


class _Stop(Exception):
    pass


@pytest.fixture
def fresh(monkeypatch):
    """A clean tracker per feed under test."""
    out = {}
    for fid in ("connectivity", "rss", "telegram"):
        out[fid] = FeedTracker(feeds.REGISTRY[fid])
        monkeypatch.setitem(feeds.TRACKERS, fid, out[fid])
    return out


def test_all_six_collectors_are_registered():
    assert set(feeds.REGISTRY) == {"aircraft", "vessels", "satellites", "connectivity", "rss", "telegram"}
    assert feeds.REGISTRY["telegram"].requires_env == ("TELEGRAM_API_ID", "TELEGRAM_API_HASH")


# ── Connectivity ─────────────────────────────────────────────────────────────

def _country(*ages):
    return {"sensors": {f"s{i}": {"data_age": a} for i, a in enumerate(ages)}}


def test_the_connectivity_clock_is_the_newest_real_measurement_not_the_poll():
    until = int(NOW)
    countries = [_country(1200, None), _country(None, 900)]
    assert conn.newest_measurement(countries, until) == until - 900
    # No sensor with data: no upstream time, never the poll time.
    assert conn.newest_measurement([_country(None, None)], until) is None


def test_a_cycle_with_answers_is_live_by_iodas_clock(fresh):
    conn.report_cycle(NOW, [_country(600)], answers=4, until=int(NOW))
    t = fresh["connectivity"]
    assert t.state(NOW) is FeedState.LIVE
    assert t.source_epoch == int(NOW) - 600


def test_answers_with_only_hours_old_data_are_stale_not_live(fresh):
    conn.report_cycle(NOW, [_country(3 * 3600)], answers=4, until=int(NOW))
    assert fresh["connectivity"].state(NOW) is FeedState.STALE


def test_zero_answers_is_a_failure_not_a_quiet_world(fresh):
    """Before this, zero answers set a status string and nothing else: a
    country list of "no disruption" was indistinguishable from IODA gone."""
    conn.report_cycle(NOW, [_country(None)], answers=0, until=int(NOW))
    t = fresh["connectivity"]
    assert t.last_success_at is None
    assert t.error_kind is ErrorKind.FETCH_ERROR
    assert "0 of" in t.error_detail


async def test_the_connectivity_loop_reports_every_cycle(fresh, monkeypatch):
    async def measure(client, code, name, frm, until):
        return {"bgp": conn._sensor(True, None, 1, 1, 0.0, False, 10, [], data_age=300)}, 1

    async def nothing(client):
        return None

    monkeypatch.setattr(conn, "_measure_country", measure)
    monkeypatch.setattr(conn, "_refresh_long", nothing)
    monkeypatch.setattr(conn, "_refresh_radar", nothing)

    async def sleep(s):
        raise _Stop

    with pytest.raises(_Stop):
        await conn.start_connectivity_poller(sleep=sleep, clock=lambda: NOW)
    t = fresh["connectivity"]
    assert t.state(NOW) is FeedState.LIVE
    assert t.count == len(conn.WATCHED)
    assert t.source_epoch == int(NOW) - 300


async def test_a_crashed_connectivity_cycle_is_recorded(fresh, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("scoring exploded")

    async def nothing(client):
        return None

    async def measure(client, code, name, frm, until):
        return {}, 1

    monkeypatch.setattr(conn, "_measure_country", measure)
    monkeypatch.setattr(conn, "_refresh_long", nothing)
    monkeypatch.setattr(conn, "_refresh_radar", nothing)
    monkeypatch.setattr(conn, "_build_country", boom)

    async def sleep(s):
        raise _Stop

    with pytest.raises(_Stop):
        await conn.start_connectivity_poller(sleep=sleep, clock=lambda: NOW)
    t = fresh["connectivity"]
    assert t.consecutive_failures == 1
    assert "scoring exploded" in t.error_detail


# ── RSS ──────────────────────────────────────────────────────────────────────

RSS_OK = """<?xml version="1.0"?><rss><channel>
<item><title>A</title><link>https://x/a</link><pubDate>Mon, 14 Jan 2027 08:00:00 GMT</pubDate></item>
<item><title>B</title><link>https://x/b</link></item>
</channel></rss>"""
RSS_EMPTY = '<?xml version="1.0"?><rss><channel></channel></rss>'
DATED = datetime(2027, 1, 14, 8, tzinfo=timezone.utc).timestamp()


def test_a_missing_pubdate_is_marked_undated_not_passed_off_as_the_feeds_time():
    arts = rss._parse_rss(RSS_OK)
    assert [a["dated"] for a in arts] == [True, False]
    assert arts[0]["published"].timestamp() == DATED


def test_an_html_error_page_is_not_a_feed_and_an_empty_feed_is():
    assert rss._parse_rss("<html><body>502</body></html>") is None
    assert rss._parse_rss("not xml at all <") is None
    assert rss._parse_rss(RSS_EMPTY) == []


class _Resp:
    def __init__(self, status, text=""):
        self.status_code = status
        self.text = text


class _Client:
    """Answers by URL; an Exception value is raised."""

    def __init__(self, by_url):
        self.by_url = by_url

    async def get(self, url, **kw):
        r = self.by_url[url]
        if isinstance(r, BaseException):
            raise r
        return r

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


@pytest.mark.parametrize("reply, kind", [
    (_Resp(200, RSS_OK), ErrorKind.OK),
    (_Resp(200, RSS_EMPTY), ErrorKind.EMPTY),
    (_Resp(200, "<html>maintenance</html>"), ErrorKind.PARSE_ERROR),
    (_Resp(404), ErrorKind.HTTP_ERROR),
    (_Resp(429), ErrorKind.RATE_LIMITED),
    (httpx.ReadTimeout("slow"), ErrorKind.TIMEOUT),
    (httpx.ConnectError("refused"), ErrorKind.FETCH_ERROR),
])
async def test_each_feed_fetch_has_one_outcome(reply, kind):
    feed = {"url": "u", "source": "s", "reliability": 3}
    _, got, _ = await rss._fetch_feed(feed, _Client({"u": reply}))
    assert got is kind


def test_one_feed_answering_is_live_and_the_dead_ones_are_named(fresh):
    """No partial state: the roll-up is live, and `parts` keeps a dead feed
    URL visible instead of letting it hide behind the others."""
    arts = rss._parse_rss(RSS_OK)
    rss.report_cycle(DATED + 60, {
        "bbc": (arts, ErrorKind.OK, None),
        "reuters": (None, ErrorKind.HTTP_ERROR, "HTTP 404"),
    })
    t = fresh["rss"]
    assert t.state(DATED + 60) is FeedState.LIVE
    assert t.source_epoch == DATED  # the dated article, not receipt time
    assert t.count == 1
    assert t.parts == {"bbc": "ok", "reuters": "http_error: HTTP 404"}
    assert t.snapshot(DATED + 60)["parts"]["reuters"] == "http_error: HTTP 404"


def test_feeds_answering_with_only_undated_items_are_stale(fresh):
    undated = [a for a in rss._parse_rss(RSS_OK) if not a["dated"]]
    rss.report_cycle(NOW, {"bbc": (undated, ErrorKind.OK, None)})
    t = fresh["rss"]
    assert t.source_epoch is None
    assert t.state(NOW) is FeedState.STALE


def test_every_feed_failing_is_a_failure_naming_each(fresh):
    rss.report_cycle(NOW, {
        "bbc": (None, ErrorKind.TIMEOUT, "ReadTimeout"),
        "ap": (None, ErrorKind.TIMEOUT, "ReadTimeout"),
        "reuters": (None, ErrorKind.HTTP_ERROR, "HTTP 404"),
    })
    t = fresh["rss"]
    assert t.last_success_at is None
    assert t.error_kind is ErrorKind.TIMEOUT  # the most common
    assert t.error_detail.startswith("all 3 feeds failed:")
    assert "reuters http_error: HTTP 404" in t.error_detail


async def test_the_rss_cycle_reports_before_it_classifies(fresh, monkeypatch):
    """Health is whether the outlets answered, not how long the classifier
    took: the tracker is updated before any article is processed."""
    monkeypatch.setattr(rss, "FEEDS", [
        {"url": "a", "source": "bbc", "reliability": 4},
        {"url": "b", "source": "reuters", "reliability": 4},
    ])
    seen_state = []

    async def process(feed, articles):
        seen_state.append(fresh["rss"].last_success_at)

    monkeypatch.setattr(rss, "_process_articles", process)

    async def sleep(s):
        if s == rss.POLL_INTERVAL:
            raise _Stop

    client = _Client({"a": _Resp(200, RSS_OK), "b": _Resp(404)})
    with pytest.raises(_Stop):
        await rss.start_news_feed_poller(sleep=sleep, clock=lambda: DATED + 60, client=client)
    assert seen_state == [DATED + 60]  # reported first; only bbc had articles
    assert fresh["rss"].parts["reuters"] == "http_error: HTTP 404"


# ── Telegram ─────────────────────────────────────────────────────────────────

@pytest.fixture
def no_messages(monkeypatch):
    monkeypatch.setattr(tg, "_newest_message_at", None)


def test_connected_and_recent_messages_is_live(fresh, no_messages):
    tg._note_message(datetime.fromtimestamp(NOW - 600, tz=timezone.utc))
    tg.report_connection(NOW, True, channels=12)
    t = fresh["telegram"]
    assert t.state(NOW) is FeedState.LIVE
    assert t.count == 12


def test_connected_but_quiet_channels_are_stale_not_live(fresh, no_messages):
    """Connected is not the same as hearing anything."""
    tg._note_message(datetime.fromtimestamp(NOW - 5 * 3600, tz=timezone.utc))
    tg.report_connection(NOW, True, channels=12)
    assert fresh["telegram"].state(NOW) is FeedState.STALE
    tg._note_message(datetime.fromtimestamp(NOW - 9 * 3600, tz=timezone.utc))  # older: ignored
    assert tg._newest_message_at == NOW - 5 * 3600


def test_a_dropped_connection_goes_retrying_then_unavailable(fresh, no_messages):
    tg._note_message(datetime.fromtimestamp(NOW, tz=timezone.utc))
    tg.report_connection(NOW, True, channels=12)
    tg.report_connection(NOW + 60, False, channels=12)
    t = fresh["telegram"]
    assert t.state(NOW + 60) is FeedState.RETRYING
    assert t.state(NOW + feeds.REGISTRY["telegram"].max_stale_s + 1) is FeedState.UNAVAILABLE


def test_a_session_that_wants_a_login_code_is_an_auth_failure():
    assert tg.classify_start_error(EOFError("EOF when reading a line")) is ErrorKind.AUTH_FAILED
    assert tg.classify_start_error(OSError("network down")) is ErrorKind.FETCH_ERROR


async def test_an_exited_listener_is_dead_and_says_why(fresh):
    t = fresh["telegram"]
    t.failed(NOW, ErrorKind.AUTH_FAILED, "login failed: EOFError")

    async def gone():
        return None

    t.task = asyncio.create_task(gone())
    await t.task
    assert t.state(NOW) is FeedState.DEAD
    assert t.reason(NOW) == "collector task exited · auth_failed: login failed: EOFError"


async def test_the_watchdog_reports_the_connection_each_minute(fresh, no_messages):
    class _C:
        def __init__(self):
            self.up = iter([True, False])

        def is_connected(self):
            return next(self.up)

    ticks = iter([NOW, NOW + 60])
    slept = []

    async def sleep(s):
        slept.append(s)
        if len(slept) == 2:
            raise _Stop

    with pytest.raises(_Stop):
        await tg._watchdog(_C(), 5, sleep=sleep, clock=lambda: next(ticks))
    t = fresh["telegram"]
    assert slept == [tg.WATCHDOG_INTERVAL_S] * 2
    assert (t.last_success_at, t.consecutive_failures) == (NOW, 1)


def test_missing_credentials_read_unconfigured_and_demo_says_why(fresh):
    t = fresh["telegram"]
    t.configured = False
    assert t.state(NOW) is FeedState.UNCONFIGURED
    assert t.reason(NOW) == "set TELEGRAM_API_ID, TELEGRAM_API_HASH"
    r = fresh["rss"]
    r.configured, r.off_reason = False, "not run in demo mode"
    assert r.reason(NOW) == "not run in demo mode"
    h = feeds.health(NOW, NOW)
    off = {x["feed"]: x for x in h["not_collected"]}
    assert off["rss"]["missing_env"] == [] and off["rss"]["reason"] == "not run in demo mode"
