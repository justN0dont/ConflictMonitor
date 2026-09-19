"""Internet-disruption service.

Polls IODA (Georgia Tech, free, no auth) for four INDEPENDENT connectivity
sensors per country and reports them SEPARATELY.  The four are never averaged:
agreement across sensors is itself the confidence signal, because one sensor
moving alone is almost always a measurement artifact rather than a country
losing the internet.

Governments cut connectivity before and during operations, so this is an
ABSENCE signal.  That cuts both ways: a sensor that returns nothing is reported
as UNAVAILABLE, never as normal, and a country we cannot corroborate is
"degraded" rather than "nominal".

THE BASELINE IS THE SAME CLOCK HOURS YESTERDAY, NOT THE DAILY MEDIAN.
Two of the four sensors (Google traffic, telescope backscatter) follow human
activity, so in the small hours they sit far below the day's median TOGETHER —
and a flat-median baseline reads that agreement as a corroborated outage.
Replayed hour by hour over 24h of live IODA, on a day when no watched country
lost the internet:

    flat 24h median baseline : 7 "partial disruption" country-hours out of 216
                               (gtr voted depressed 3-7h of every 24, for 8 of 9
                               countries; merit-nt 8h for Iraq)
    same hours yesterday     : 0 out of 216

Comparing 03:00 with 03:00 also keeps a blackout visible once it has been
running for hours, which a same-day median cannot: the outage eventually
becomes the baseline and disarms the sensor that should be reporting it.
"""

import asyncio
import logging
import math
import time
from statistics import median

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

IODA_BASE = "https://api.ioda.inetintel.cc.gatech.edu"

# The conflict theatres this project covers.  A plain constant on purpose:
# nothing about a deployment should change which countries get watched.
WATCHED: list[tuple[str, str]] = [
    ("IR", "Iran"),
    ("IL", "Israel"),
    ("LB", "Lebanon"),
    ("SY", "Syria"),
    ("IQ", "Iraq"),
    ("YE", "Yemen"),
    ("PS", "Palestine"),
    ("UA", "Ukraine"),
    ("RU", "Russia"),
]

# Four genuinely independent sensors at different resolutions: routed /24s
# (control plane), active probing, background radiation at a network telescope,
# and Google's own traffic.  Measured steps are 300s, 600s, 300s and 1800s.
DATASOURCES = ["bgp", "ping-slash24", "merit-nt", "gtr"]

DAY_SECONDS = 24 * 3600
# 48h: today, plus the same clock hours yesterday to judge it against.  Measured:
# IODA serves 48h at full native resolution (577/289/577/97 samples) in under
# 4 KB per sensor, so the second day costs bytes, not requests.
WINDOW_SECONDS = 48 * 3600
POLL_INTERVAL = 300  # IODA's fastest sensor steps at 300s; polling faster gains
                     # nothing and is rude to a free academic service.

# adsb.lol 403s generic clients and CelesTrak IP-banned this host for excessive
# downloads.  IODA gets the same courtesy: a contact User-Agent.
IODA_USER_AGENT = "conflict-monitor/1.0 (+https://github.com/troofevades-rgb/conflict-monitor)"

# "Now" is the last 90 minutes of MEASURED time, anchored to the most recent
# sample rather than to the clock: IODA's ingest runs behind (measured lag
# bgp 19m, ping 19m, merit-nt 14m, gtr 99m), so a window pinned to the wall
# clock would be empty for gtr and would silently judge merit-nt on old data.
CURRENT_WINDOW = 5400
SEASONAL_HALF = 5400    # baseline = the same 3h of yesterday, centred on the
                        # clock time of the current window.
MIN_CURRENT_POINTS = 2  # gtr contributes 3 samples at 1800s; one is a spike.
MIN_BASELINE_POINTS = 4
DEPRESSED_AT = -0.30    # 30% below this sensor's own level at this hour yesterday
SPARK_POINTS = 48       # never ship 577 raw points per sensor per country

# A sensor that has not reported for this long is UNAVAILABLE, not calm.  Without
# it, a series that stopped hours ago still scores: the nulls are dropped, the
# last live samples become "current", and a country nothing is measuring renders
# nominal beside a two-minute-old timestamp.  The bound has to clear IODA's
# normal ingest lag (above), so it is six of the sensor's own steps, and never
# under an hour: bgp/ping/merit 1h, gtr 3h.
STALE_STEPS = 6
STALE_FLOOR = 3600

# A ratio needs a denominator big enough to mean something.  Same defect as the
# altitude floor on the interference denominator: a -30% move on a telescope
# whose normal reading is 1 packet is 0.36 packets, which is noise wearing a
# percentage.  Measured over the 24h replay above, the only two merit-nt series
# that ever voted depressed had baselines of 0.8 (Yemen) and 10.8 (Lebanon);
# every series that never did was >= 20.8.  Below the floor the sensor is
# unavailable — it cannot corroborate, and it cannot object either.
MIN_BASELINE = {
    "bgp": 10.0,           # routed /24s; the smallest watched country has ~960
    "ping-slash24": 10.0,  # responding /24s; Yemen, the smallest, sits at ~21
    "merit-nt": 15.0,      # between the two noisy series and the seven quiet ones
    "gtr": 1e6,            # Google traffic runs ~1e9; 1e6 is a broken series
}

_cache: dict = {
    "countries": [],
    "timestamp": 0,
    "status": "no_data",
}


def _downsample(values: list) -> list:
    """Collapse a series to at most SPARK_POINTS buckets for a sparkline.

    A bucket holding no measurement stays None so the sparkline draws a gap.
    Rendering a gap as zero would invent an outage.
    """
    if not values:
        return []
    size = max(1, -(-len(values) // SPARK_POINTS))  # ceil division
    spark = []
    for i in range(0, len(values), size):
        chunk = [v for v in values[i:i + size] if v is not None]
        spark.append(round(median(chunk), 2) if chunk else None)
    return spark


def _sensor(available, reason, baseline, current, deviation, depressed,
            points, spark, data_age=None) -> dict:
    return {
        "available": available,
        "reason": reason,
        "baseline": baseline,
        "current": current,
        "deviation": deviation,
        "depressed": depressed,
        "points": points,
        "spark": spark,
        # Seconds between the last real measurement and the poll.  The row's age
        # is poll age PLUS this: IODA's lag is part of how old the number is.
        "data_age": data_age,
    }


def _score_series(series: dict | None, until: int, datasource: str) -> dict:
    """Score one sensor's current reading against its own level 24h earlier.

    Median everywhere, never mean: these series are spiky, and a single 10x
    spike would otherwise hide a real drop.  `points` is the number of usable
    samples — nulls mean "not measured" and are dropped, never read as zero.
    """
    if series is None:
        return _sensor(False, "fetch_failed", None, None, None, False, 0, [])

    raw = series.get("values") or []
    step = series.get("step") or 0
    frm = series.get("from")
    if not raw or not step or frm is None:
        # IODA answered and holds no series for this entity.  That is a third
        # fact, distinct from "I could not reach it" and from "too few points".
        return _sensor(False, "no_series", None, None, None, False, 0, [])

    # NaN and Infinity are legal to json.loads, and one NaN reaching the response
    # body 500s this endpoint for all nine countries — while `nan <= -0.30` is
    # False, so the sensor would count as available and calm on the way there.
    # Anything that is not a finite number is "not measured".
    values: list = []
    bad = 0
    for v in raw:
        if v is None:
            values.append(None)
        elif isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            bad += 1
            values.append(None)
        else:
            values.append(float(v))

    # The trace shows the last 24h; the day before it exists to be a baseline.
    keep = int(DAY_SECONDS // step) + 1
    spark = _downsample(values[-keep:])

    live = [i for i, v in enumerate(values) if v is not None]
    points = len(live)
    if not live:
        return _sensor(False, "bad_values" if bad else "all_null",
                       None, None, None, False, 0, spark)

    last_t = frm + live[-1] * step
    data_age = max(0, until - last_t)
    if data_age > max(STALE_STEPS * step, STALE_FLOOR):
        return _sensor(False, "stale_series", None, None, None, False, points, spark, data_age)

    current_pts = [values[i] for i in live if frm + i * step > last_t - CURRENT_WINDOW]
    centre = last_t - DAY_SECONDS
    baseline_pts = [values[i] for i in live if abs(frm + i * step - centre) <= SEASONAL_HALF]
    if len(current_pts) < MIN_CURRENT_POINTS or len(baseline_pts) < MIN_BASELINE_POINTS:
        return _sensor(False, "insufficient_points", None, None, None, False, points, spark, data_age)

    baseline = median(baseline_pts)
    current = median(current_pts)

    if baseline < MIN_BASELINE.get(datasource, 0.0):
        # Too small a denominator to carry a ratio, so this sensor cannot
        # corroborate anything.  Returning "not depressed" here would be a claim.
        return _sensor(False, "low_baseline", round(baseline, 2), round(current, 2),
                       None, False, points, spark, data_age)

    deviation = (current - baseline) / baseline
    return _sensor(
        True, None, round(baseline, 2), round(current, 2),
        round(deviation, 4), deviation <= DEPRESSED_AT, points, spark, data_age,
    )


def _build_country(code: str, name: str, sensors: dict) -> dict:
    """Apply the corroboration model across the four sensors."""
    available = [s for s in sensors.values() if s["available"]]
    depressed = [s for s in available if s["depressed"]]

    if len(available) < 2:
        # Nothing can corroborate anything, so we do not claim either way.
        state = "degraded"
    elif len(depressed) >= 3:
        state = "disruption"
    elif len(depressed) == 2:
        state = "partial"
    else:
        # One sensor depressed alone is a measurement artifact, not an outage.
        state = "nominal"

    return {
        "code": code,
        "name": name,
        "sensors": sensors,
        "sensors_available": len(available),
        "sensors_depressed": len(depressed),
        "state": state,
        # None, not 0.0, when nothing is measurable: 0.0 would read as
        # "measured, no change" — the false-nominal this design refuses.
        "worst_deviation": min((s["deviation"] for s in available), default=None),
        "as_of": int(time.time()),
    }


async def _fetch_series(client: httpx.AsyncClient, code: str, datasource: str,
                        frm: int, until: int) -> dict | None:
    """One country, one datasource.  None means IODA did not answer.

    An empty dict means it answered but holds no series for this entity, which
    is a different fact and gets a different reason.
    """
    try:
        resp = await client.get(
            f"{IODA_BASE}/v2/signals/raw/country/{code}",
            params={"from": frm, "until": until, "datasource": datasource},
        )
        if resp.status_code != 200:
            logger.warning("IODA %s/%s returned %d", code, datasource, resp.status_code)
            return None
        data = resp.json().get("data") or []
        if not data or not data[0]:
            return {}
        entry = data[0][0]
        return entry if isinstance(entry, dict) else {}
    except Exception as e:
        logger.error("IODA %s/%s fetch error: %s", code, datasource, e)
        return None


async def _measure_country(client: httpx.AsyncClient, code: str, name: str,
                           frm: int, until: int) -> tuple[dict, int]:
    """Returns the country entry plus how many datasources IODA answered for."""
    sensors = {}
    answered = 0
    for datasource in DATASOURCES:
        series = await _fetch_series(client, code, datasource, frm, until)
        if series is not None:
            answered += 1
        try:
            sensors[datasource] = _score_series(series, until, datasource)
        except Exception as e:
            # One malformed sensor must not be reported as the COUNTRY failing:
            # IODA answered, and the other three sensors still have something
            # true to say.  "fetch_failed" here would be a false statement.
            logger.error("Connectivity: %s/%s scoring failed: %s", code, datasource, e)
            sensors[datasource] = _sensor(False, "score_failed", None, None, None, False, 0, [])
    return _build_country(code, name, sensors), answered


async def start_connectivity_poller():
    """Background task that polls IODA every POLL_INTERVAL seconds."""
    logger.info(
        "Connectivity: polling IODA for %d countries x %d sensors every %ds",
        len(WATCHED), len(DATASOURCES), POLL_INTERVAL,
    )
    if not settings.cloudflare_radar_token:
        logger.info("Connectivity: Cloudflare Radar unconfigured — skipped, not failed")

    async with httpx.AsyncClient(
        timeout=30,
        headers={"User-Agent": IODA_USER_AGENT},
    ) as client:
        while True:
            try:
                until = int(time.time())
                frm = until - WINDOW_SECONDS
                countries = []
                answers = 0

                for code, name in WATCHED:
                    try:
                        entry, answered = await _measure_country(client, code, name, frm, until)
                    except Exception as e:
                        # One country must never take down the cycle or the task.
                        logger.error("Connectivity: %s measurement failed: %s", code, e)
                        entry = _build_country(
                            code, name, {ds: _score_series(None, until, ds) for ds in DATASOURCES}
                        )
                        answered = 0
                    countries.append(entry)
                    answers += answered

                _cache["countries"] = countries
                _cache["timestamp"] = int(time.time())
                # Not one answer out of 36 requests means IODA itself is gone.
                _cache["status"] = "ok" if answers else "unavailable"

                states = [c["state"] for c in countries]
                logger.info(
                    "Connectivity: %d countries — %d disruption, %d partial, %d degraded (status=%s)",
                    len(countries),
                    states.count("disruption"),
                    states.count("partial"),
                    states.count("degraded"),
                    _cache["status"],
                )
            except Exception as e:
                logger.error("Connectivity poll error: %s", e)

            await asyncio.sleep(POLL_INTERVAL)


def get_connectivity() -> dict:
    """Return the current per-country disruption measurement plus provenance."""
    return {
        "status": _cache["status"],
        "as_of": _cache["timestamp"],
        "poll_interval": POLL_INTERVAL,
        "countries": _cache["countries"],
        # There is no token, so Radar is never called.  Unconfigured is reported
        # as unconfigured — not faked, not errored, not retried.
        "cloudflare": {"configured": bool(settings.cloudflare_radar_token)},
    }
