"""The aircraft poller: interference outcomes, and what a failed poll leaves behind.

Two halves.

1. `_detect_jamming` is pure, so its three outcomes are pinned directly.
   Protects 64b690a (a real denominator and three honest states) and 3c92466
   (the FL200 altitude floor).

2. `start_opensky_poller` is driven for real: both fetchers are scripted,
   `asyncio.sleep` is replaced so the loop stops after the scripted cycles,
   and the module cache is swapped for a fresh one. No poll_once seam exists
   yet (docs/GODS-EYE-VIEW.md, PHASE2-2); driving the loop tests the code that
   runs rather than a copy of it. The C31 test was a strict xfail from
   2b60071 until the aircraft slice of feed_health fixed it; the marker came
   off in the fixing commit, as the ratchet required.
"""

import types

import pytest

from app import feeds
from app.feeds import ErrorKind
from app.services import opensky

_FL = opensky.MIN_ALTITUDE_FT


def _aircraft(i, lat=29.5, lon=45.5, alt=_FL + 5000, nic=8, nac_p=10):
    return {"icao24": f"a{i:05x}", "lat": lat, "lon": lon, "altitude": alt,
            "on_ground": False, "nic": nic, "nac_p": nac_p}


def _cell(n, degraded=0, **kw):
    """n aircraft in one grid cell, the first `degraded` of them failing NIC."""
    return [_aircraft(i, nic=5 if i < degraded else 8, **kw) for i in range(n)]


# ── _detect_jamming: three honest states (64b690a, 3c92466) ──────────────────

def test_aircraft_without_integrity_fields_cannot_be_evaluated():
    """OpenSky state vectors carry no nic/nac_p. That is "I wasn't looking",
    never "no interference"."""
    states = [{**a, "nic": None, "nac_p": None} for a in _cell(20)]
    out = opensky._detect_jamming(states)
    assert out["status"] == "no_integrity_data"
    assert out["aircraft_evaluable"] == 0
    assert out["zones"] == []


def test_below_the_altitude_floor_nothing_is_evaluable():
    """3c92466: low-level GA reports low NIC because of equipage. Ten degraded
    aircraft below FL200 are not evidence of anything."""
    out = opensky._detect_jamming(_cell(10, degraded=10, alt=_FL - 1000))
    assert out["status"] == "no_integrity_data"
    assert out["aircraft_evaluable"] == 0


def test_too_few_aircraft_per_cell_is_insufficient_coverage():
    """64b690a: a cell with fewer than MIN_CELL_AIRCRAFT cannot carry a ratio,
    so the AO was not measured, even though aircraft were evaluable."""
    out = opensky._detect_jamming(_cell(opensky.MIN_CELL_AIRCRAFT - 1, degraded=5))
    assert out["status"] == "insufficient_coverage"
    assert out["cells_evaluated"] == 0
    assert out["aircraft_evaluable"] == opensky.MIN_CELL_AIRCRAFT - 1


def test_a_measured_cell_reports_ok_and_a_zone_only_above_the_ratio():
    n = opensky.MIN_CELL_AIRCRAFT
    quiet = opensky._detect_jamming(_cell(n, degraded=0))
    jammed = opensky._detect_jamming(_cell(n, degraded=n))
    assert (quiet["status"], quiet["zones"]) == ("ok", [])
    assert jammed["status"] == "ok"
    assert len(jammed["zones"]) == 1
    assert jammed["zones"][0]["ratio"] == 1.0


# ── The poll loop, driven for real ───────────────────────────────────────────

class _StopLoop(Exception):
    pass


async def _run_poller(monkeypatch, cycles):
    """Run the real loop over scripted (adsb_result, opensky_result) cycles.

    Returns the cache the loop wrote to. A list is a successful fetch (empty
    list: a successful empty answer); None is a failed fetch, which the
    scripted fetcher reports as a fetch_error.
    """
    cache = {**opensky._cache, "states": [], "jamming": []}
    monkeypatch.setattr(opensky, "_cache", cache)
    fresh = feeds.FeedTracker(feeds.REGISTRY["aircraft"])
    monkeypatch.setitem(feeds.TRACKERS, "aircraft", fresh)
    monkeypatch.setattr(opensky, "record_aircraft_position", lambda *a: None)

    script = list(cycles)
    turn = {"i": 0}

    def result(states, name):
        if states is None:
            return opensky.FetchResult(None, ErrorKind.FETCH_ERROR, detail=f"{name} down")
        kind = ErrorKind.OK if states else ErrorKind.EMPTY
        # A real upstream clock, so a success reads live rather than stale.
        return opensky.FetchResult(states, kind, source_epoch=opensky.time.time())

    async def adsb(_client):
        return result(script[turn["i"]][0], "adsb.lol")

    async def opensky_fetch(_client, _auth):
        return result(script[turn["i"]][1], "opensky")

    async def sleep(_seconds):
        turn["i"] += 1
        if turn["i"] >= len(script):
            raise _StopLoop

    monkeypatch.setattr(opensky, "_poll_adsb_lol", adsb)
    monkeypatch.setattr(opensky, "_poll_opensky", opensky_fetch)
    # Only this module's view of asyncio: patching asyncio.sleep itself would
    # change it for the event loop running the test too.
    monkeypatch.setattr(opensky, "asyncio", types.SimpleNamespace(sleep=sleep))

    with pytest.raises(_StopLoop):
        await opensky.start_opensky_poller()
    return cache


async def test_a_fallback_poll_records_opensky_as_the_source(monkeypatch):
    """The adsb.lol -> OpenSky switch used to reach only /tracking/jamming, as a
    source string. It is now `fallback_from` on the aircraft envelope."""
    cache = await _run_poller(monkeypatch, [(None, _cell(3))])
    assert cache["source"] == "opensky"
    assert len(cache["states"]) == 3
    # ...and now as a field on the envelope, not a string to infer it from.
    env = opensky.get_aircraft_envelope()
    assert (env["source"], env["fallback_from"]) == ("opensky", "adsb.lol")


async def test_a_failed_poll_does_not_advance_the_success_time(monkeypatch):
    """as_of is the time of the last SUCCESS. Freezing it on failure is right;
    what is wrong is that nothing else says the feed stopped (the C31 test)."""
    cache = await _run_poller(monkeypatch, [(_cell(10), None), (None, None)])
    first_success = cache["timestamp"]
    assert first_success > 0
    assert opensky.get_jamming_zones()["as_of"] == first_success


async def test_after_both_sources_fail_the_verdict_is_not_still_ok(monkeypatch):
    """C31 (FINDINGS.md): with both adsb.lol and OpenSky down, the last
    measured verdict kept reading "ok" indefinitely, and the UI showed a dead
    feed as a calm sky. Reproduced live on 2026-09-24 (docs/GODS-EYE-VIEW.md,
    section 2). Contract: after a poll in which nothing was measured, the
    status must not claim a measurement."""
    await _run_poller(monkeypatch, [(_cell(10), None), (None, None)])
    js = opensky.get_jamming_zones()
    assert js["status"] != "ok"
    assert js["status"] == "feed_not_live"
    assert js["feed_state"] == "retrying"
    assert js["zones"] == []


async def test_after_both_sources_fail_the_fleet_is_kept_but_not_counted(monkeypatch):
    """C31, the aircraft half. The last fleet stays available as last-good, but
    the envelope says the feed is retrying and prints no count, so neither the
    header nor the rail can show it as a live reading."""
    await _run_poller(monkeypatch, [(_cell(10), None), (None, None)])
    env = opensky.get_aircraft_envelope()
    assert env["state"] == "retrying"
    assert env["count"] is None
    assert env["verdict"] == "unproven"
    assert len(env["items"]) == 10
    assert env["reason"].startswith("fetch_error")


async def test_a_successful_empty_answer_is_live_with_an_unproven_zero(monkeypatch):
    """An empty sky from a live feed is a measured zero, printed as 0 — but
    still `unproven`: `absent` waits for the control ring."""
    await _run_poller(monkeypatch, [([], None)])
    env = opensky.get_aircraft_envelope()
    assert (env["state"], env["count"], env["verdict"]) == ("live", 0, "unproven")
