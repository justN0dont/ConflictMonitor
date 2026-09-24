"""The aircraft poller: interference outcomes, and what a failed poll leaves behind.

Two halves.

1. `_detect_jamming` is pure, so its three outcomes are pinned directly.
   Protects 64b690a (a real denominator and three honest states) and 3c92466
   (the FL200 altitude floor).

2. `start_opensky_poller` is driven for real: both fetchers are scripted,
   `asyncio.sleep` is replaced so the loop stops after the scripted cycles,
   and the module cache is swapped for a fresh one. No poll_once seam exists
   yet (docs/GODS-EYE-VIEW.md, PHASE2-2); driving the loop tests the code that
   runs rather than a copy of it. The C31 test is a strict xfail: it describes
   the defect today and turns into an XPASS failure the day feed_health fixes
   it, which forces the marker off.
"""

import types

import pytest

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

    Returns the cache the loop wrote to. A result of None is a failed fetch,
    exactly what _poll_adsb_lol / _poll_opensky return on any error.
    """
    cache = {**opensky._cache, "states": [], "jamming": []}
    monkeypatch.setattr(opensky, "_cache", cache)
    monkeypatch.setattr(opensky, "record_aircraft_position", lambda *a: None)

    script = list(cycles)
    turn = {"i": 0}

    async def adsb(_client):
        return script[turn["i"]][0]

    async def opensky_fetch(_client, _auth):
        return script[turn["i"]][1]

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
    """The switch is at least recorded in the cache. It reaches only
    /tracking/jamming today; the aircraft route is a bare array (untriaged:
    bare-array tracking routes, FINDINGS.md)."""
    cache = await _run_poller(monkeypatch, [(None, _cell(3))])
    assert cache["source"] == "opensky"
    assert len(cache["states"]) == 3


async def test_a_failed_poll_does_not_advance_the_success_time(monkeypatch):
    """as_of is the time of the last SUCCESS. Freezing it on failure is right;
    what is wrong is that nothing else says the feed stopped (the C31 test)."""
    cache = await _run_poller(monkeypatch, [(_cell(10), None), (None, None)])
    first_success = cache["timestamp"]
    assert first_success > 0
    assert opensky.get_jamming_zones()["as_of"] == first_success


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="C31: a failed poll leaves the last verdict in place with status still ok",
)
async def test_after_both_sources_fail_the_verdict_is_not_still_ok(monkeypatch):
    """C31 (FINDINGS.md): with both adsb.lol and OpenSky down, the last
    measured verdict keeps reading "ok" indefinitely, and the UI shows a dead
    feed as a calm sky. Reproduced live on 2026-09-24 (docs/GODS-EYE-VIEW.md,
    section 2). The fix is feed_health's; this test only states the contract:
    after a poll in which nothing was measured, the status must not claim a
    measurement."""
    await _run_poller(monkeypatch, [(_cell(10), None), (None, None)])
    assert opensky.get_jamming_zones()["status"] != "ok"
