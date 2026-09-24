"""The connectivity layer's "I don't know" states. Protects 51bdce9 and bb1c80c.

connectivity.py is the one module in this repo that already keeps "not
measured", "not configured" and "could not tell" apart from "nominal", and the
roadmap's feed_health work will generalise it. These tests pin that vocabulary
and the agreement rule before anything is refactored toward a shared helper,
so a refactor cannot quietly turn degraded into nominal.

Deliberately NOT pinned: robust-z values, Z_DEPRESSED, the swing ruler and the
baseline windows. The trailing-baseline and regime_id work (Phase 2) is meant
to change those; pinning them would make that work fight the suite. Series are
built here as dicts, not captured IODA payloads.
"""

import math

import pytest

from app.config import settings
from app.services import connectivity as c

_NOW = 1_800_000_000


def _sensor(available, depressed=False, deviation=None):
    return c._sensor(available, None if available else "fetch_failed",
                     None, None, deviation, depressed, 0, [])


def _four(*flags):
    """Four sensors from (available, depressed) pairs, padded with unavailable."""
    flags = list(flags) + [(False, False)] * (4 - len(flags))
    return {ds: _sensor(a, d, deviation=-0.5 if d else 0.01 if a else None)
            for ds, (a, d) in zip(c.DATASOURCES, flags)}


# ── _clean: anything not a finite number is "not measured" (51bdce9) ─────────

def test_non_finite_and_non_numbers_are_not_measured():
    """51bdce9: one NaN reaching the response body 500'd the endpoint, and
    `nan <= -0.30` is False, so the sensor read available and calm on the way.
    bool is excluded explicitly because isinstance(True, int) holds."""
    values, bad = c._clean([None, math.nan, math.inf, True, "1", 2])
    assert values == [None, None, None, None, None, 2.0]
    # None is "IODA sent a gap", not a bad value; the other four are bad.
    assert bad == 4


# ── _agree: a majority of the available sensors, never fewer than two (bb1c80c)

@pytest.mark.parametrize(
    "depressed, available, agrees",
    [
        (2, 3, True),    # Cuba: two of the three that can see agree
        (3, 4, True),
        (2, 4, False),   # half is not a majority
        (1, 1, False),   # one sensor alone is never agreement
        (1, 2, False),
    ],
)
def test_agreement_is_a_majority_of_available_sensors_with_a_floor_of_two(
    depressed, available, agrees
):
    """bb1c80c replaced a flat "3 sensors" rule that became unreachable when a
    sensor was unavailable. The floor of two is what stops one sensor from
    declaring an outage on its own."""
    assert c._agree(depressed, available) is agrees


# ── _build_country: nominal is a claim that needs coverage (bb1c80c) ─────────

def test_one_measurable_short_sensor_is_degraded_even_with_a_calm_week():
    """bb1c80c, "NOMINAL IS A CLAIM": a country whose 24h fetch mostly failed
    must not read nominal off a calm 7-day cache. Nothing is measuring it today."""
    long_calm = _four((True, False), (True, False), (True, False), (True, False))
    country = c._build_country("XX", "Test", _four((True, False)), long_calm)
    assert country["state"] == "degraded"
    assert country["basis"] is None


def test_two_calm_measurable_short_sensors_are_nominal():
    """The control for the test above: with the coverage, nominal is allowed."""
    country = c._build_country("XX", "Test", _four((True, False), (True, False)))
    assert country["state"] == "nominal"


def test_nothing_measurable_has_no_deviation_rather_than_zero():
    """0.0 would read as "measured, no change" — the false-nominal the module
    refuses. With no available sensor the worst deviation is unknown."""
    country = c._build_country("XX", "Test", _four())
    assert country["state"] == "degraded"
    assert country["worst_deviation"] is None
    assert country["worst_deviation_long"] is None


def test_long_sensors_that_were_never_fetched_are_not_measurements():
    """sensors_long defaults to _pending("not_polled"): unavailable, not calm."""
    country = c._build_country("XX", "Test", _four((True, False), (True, False)))
    assert country["sensors_long_available"] == 0
    assert {s["reason"] for s in country["sensors_long"].values()} == {"not_polled"}


# ── _score_series: four different ways of not knowing (51bdce9) ──────────────

def test_a_failed_fetch_is_fetch_failed():
    assert c._score_series(None, _NOW, "bgp")["reason"] == "fetch_failed"
    assert c._score_series(None, _NOW, "bgp")["available"] is False


def test_an_answer_with_no_series_is_its_own_fact():
    """IODA answered and holds nothing: distinct from "could not reach it"."""
    s = c._score_series({"from": _NOW - 600, "step": 300, "values": []}, _NOW, "bgp")
    assert s["reason"] == "no_series"
    assert s["available"] is False


def test_all_non_finite_values_are_bad_values_and_all_gaps_are_all_null():
    series = {"from": _NOW - 900, "step": 300}
    bad = c._score_series({**series, "values": [math.nan, math.nan, math.inf]}, _NOW, "bgp")
    gaps = c._score_series({**series, "values": [None, None, None]}, _NOW, "bgp")
    assert (bad["reason"], bad["available"]) == ("bad_values", False)
    assert (gaps["reason"], gaps["available"]) == ("all_null", False)


def test_a_series_older_than_its_stale_bound_is_stale_not_calm():
    """The bound is max(STALE_STEPS * step, STALE_FLOOR), built from both
    constants here so the test does not restate either number."""
    step = 300
    bound = max(c.STALE_STEPS * step, c.STALE_FLOOR)
    stale = c._score_series({"from": _NOW - bound - 1, "step": step, "values": [5.0]},
                            _NOW, "bgp")
    fresh = c._score_series({"from": _NOW - bound + 1, "step": step, "values": [5.0]},
                            _NOW, "bgp")
    assert stale["reason"] == "stale_series"
    assert stale["available"] is False
    assert stale["data_age"] == bound + 1
    # Inside the bound it fails for a different reason (one point), not staleness.
    assert fresh["reason"] != "stale_series"


# ── Cloudflare Radar: a missing token is unconfigured, not an error (bb1c80c) ─

class _NoClient:
    """Any attribute access means _refresh_radar tried to use the network."""

    def __getattr__(self, name):
        raise AssertionError(f"_refresh_radar touched the client ({name}) with no token")


async def test_no_radar_token_is_unconfigured_and_does_no_io(monkeypatch):
    monkeypatch.setattr(settings, "cloudflare_radar_token", "")
    cache = dict(c._radar_cache)
    monkeypatch.setattr(c, "_radar_cache", cache)

    await c._refresh_radar(_NoClient())

    assert cache["status"] == "unconfigured"
    assert cache["outages_status"] == "unconfigured"
    assert cache["attacks_status"] == "unconfigured"
    assert cache["error"] is None
