"""Satellites: element-set epochs, checksums, and what a fetch outcome means.

Closes the untriaged "TLE epoch age is never checked" item (docs/FINDINGS.md,
Collection): whatever line1/line2 arrived was propagated, and a month-old
element set would drift kilometres with no currency indicator. Keeps the C20
guarantees (0de11f2): a failed fetch never empties a good cache, and a block
backs off instead of retrying on the normal cadence.
"""

import json
from datetime import datetime, timezone

import pytest

from app import feeds
from app.feeds import ErrorKind, FeedState
from app.services import satellites as sat

# The ISS element set used as the worked example on Wikipedia's TLE page; both
# checksums are valid. Epoch 08264.51782528 = 2008, day 264.51782528.
ISS_1 = "1 25544U 98067A   08264.51782528 -.00002182  00000-0 -11606-4 0  2927"
ISS_2 = "2 25544  51.6416 247.4627 0006703 130.5360 325.0288 15.72125391563537"


def _checksum(body68: str) -> str:
    total = sum(int(c) if c.isdigit() else (1 if c == "-" else 0) for c in body68)
    return body68 + str(total % 10)


def _line1_with_epoch(ts: float) -> str:
    """ISS line 1 re-dated to `ts`, with a recomputed checksum."""
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    day = dt.timetuple().tm_yday + (dt.hour * 3600 + dt.minute * 60 + dt.second) / 86400
    field = f"{dt.year % 100:02d}{day:012.8f}"
    return _checksum(ISS_1[:18] + field + ISS_1[32:68])


@pytest.fixture
def fresh(monkeypatch, tmp_path):
    tracker = feeds.FeedTracker(feeds.REGISTRY["satellites"])
    monkeypatch.setitem(feeds.TRACKERS, "satellites", tracker)
    monkeypatch.setattr(sat, "_tle_cache", [])
    monkeypatch.setattr(sat, "CACHE_PATH", str(tmp_path / "tles.json"))
    return tracker


class _Stop(Exception):
    pass


class _Resp:
    def __init__(self, status, text=""):
        self.status_code = status
        self.text = text


class _Client:
    def __init__(self, resp):
        self.resp = resp

    async def get(self, url, params=None):
        if isinstance(self.resp, Exception):
            raise self.resp
        return self.resp


# ── Parsing ──────────────────────────────────────────────────────────────────

def test_the_epoch_is_read_from_line_one_with_the_two_digit_year_pivot():
    expected = datetime(2008, 1, 1, tzinfo=timezone.utc).timestamp() + (264.51782528 - 1) * 86400
    assert sat.epoch_utc(ISS_1) == pytest.approx(expected, abs=1e-3)
    # Below 57 is 20YY, 57 and above is 19YY.
    pre2000 = ISS_1[:18] + "98001.00000000" + ISS_1[32:]
    assert sat.epoch_utc(pre2000) == datetime(1998, 1, 1, tzinfo=timezone.utc).timestamp()
    assert sat.epoch_utc("1 25544U garbage") is None


def test_valid_records_are_kept_with_their_epoch():
    records, rejected = sat.parse_tle_text(f"ISS (ZARYA)\n{ISS_1}\n{ISS_2}\n")
    assert rejected == 0
    assert records[0]["name"] == "ISS (ZARYA)"
    assert records[0]["epoch_utc"] == sat.epoch_utc(ISS_1)


def test_a_bad_checksum_is_rejected_and_counted_not_guessed():
    corrupted = ISS_2[:10] + ("7" if ISS_2[10] != "7" else "8") + ISS_2[11:]
    records, rejected = sat.parse_tle_text(f"ISS\n{ISS_1}\n{corrupted}\n")
    assert (records, rejected) == ([], 1)


# ── fetch_once: outcome and tracker ──────────────────────────────────────────

async def test_a_fresh_fetch_of_current_orbits_is_live(fresh):
    now = datetime(2026, 9, 24, tzinfo=timezone.utc).timestamp()
    line1 = _line1_with_epoch(now - 86400)
    kind = await sat.fetch_once(_Client(_Resp(200, f"SAT A\n{line1}\n{ISS_2}\n")), now)
    assert kind is ErrorKind.OK
    assert fresh.state(now) is FeedState.LIVE
    assert fresh.source_epoch == pytest.approx(now - 86400, abs=1)


async def test_a_fresh_fetch_of_old_orbits_is_stale_not_live(fresh):
    """The point of the epoch: a fetch that succeeded today can carry orbits
    that are years old, and those are not a current picture of the sky."""
    now = datetime(2026, 9, 24, tzinfo=timezone.utc).timestamp()
    kind = await sat.fetch_once(_Client(_Resp(200, f"ISS\n{ISS_1}\n{ISS_2}\n")), now)
    assert kind is ErrorKind.OK
    assert fresh.state(now) is FeedState.STALE
    assert fresh.reason(now) == "upstream data older than its stale bound"


async def test_a_block_on_a_warm_cache_keeps_the_cache_and_is_rate_limited(fresh):
    """C20 (0de11f2): one 403 used to turn a good cache into zero satellites."""
    now = datetime(2026, 9, 24, tzinfo=timezone.utc).timestamp()
    line1 = _line1_with_epoch(now - 3600)
    await sat.fetch_once(_Client(_Resp(200, f"SAT A\n{line1}\n{ISS_2}\n")), now)
    kind = await sat.fetch_once(_Client(_Resp(403)), now + 60)
    assert kind is ErrorKind.RATE_LIMITED
    assert len(sat.get_tles()) == 1
    assert fresh.state(now + 60) is FeedState.RETRYING
    assert fresh.error_kind is ErrorKind.RATE_LIMITED


async def test_a_200_with_no_valid_record_is_a_parse_error_not_ok(fresh):
    now = 1_800_000_000.0
    kind = await sat.fetch_once(_Client(_Resp(200, "<html>maintenance</html>")), now)
    assert kind is ErrorKind.PARSE_ERROR
    assert fresh.last_success_at is None


async def test_a_blocked_fetch_backs_off_an_hour(fresh, monkeypatch):
    slept = []

    async def fetch(client, now):
        return ErrorKind.RATE_LIMITED

    async def sleep(seconds):
        slept.append(seconds)
        raise _Stop

    monkeypatch.setattr(sat, "fetch_once", fetch)
    with pytest.raises(_Stop):
        await sat.start_tle_fetcher(sleep=sleep, clock=lambda: 1_800_000_000.0)
    assert slept == [sat.BACKOFF_ON_BLOCK]




# ── The disk cache carries its real age ──────────────────────────────────────

def test_a_thirty_day_old_disk_cache_reports_its_age_and_is_not_live(fresh):
    now = datetime(2026, 9, 24, tzinfo=timezone.utc).timestamp()
    fetched = now - 30 * 86400
    line1 = _line1_with_epoch(fetched - 86400)
    with open(sat.CACHE_PATH, "w", encoding="utf-8") as fh:
        json.dump({"fetched_at": fetched, "tles": [{"name": "A", "line1": line1, "line2": ISS_2}]}, fh)

    age = sat._load_cache(now)
    assert age == pytest.approx(30 * 86400)
    assert fresh.last_success_at == fetched  # when it was fetched, not when it was loaded
    assert fresh.state(now) is FeedState.UNAVAILABLE
    env = feeds.envelope("satellites", sat.get_tles(), now)
    assert env["count"] is None
    assert env["age_s"] == pytest.approx(30 * 86400, abs=1)
    assert env["items"][0]["epoch_utc"] == sat.epoch_utc(line1)  # re-derived for old caches
