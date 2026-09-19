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

A FLAT PERCENTAGE THRESHOLD IS THE WRONG RULER, because the four sensors have
wildly different natural variance.  Measured typical swing (MAD/median over 24h)
against the reading at the time, 2026-09-19:

    country  worst sensor    deviation   typical swing   robust-z
    LB       gtr               -4.5%        12.4%          -0.4     noise
    UA       gtr               -3.6%        19.0%          -0.2     noise
    RU       ping-slash24      -0.2%         0.1%          -2.6     MOST unusual
    IR       bgp                0.0%         0.0%          +1.8

The reading with the LARGEST percentage is the calmest; the one with the
SMALLEST is the most unusual.  The displayed percentage is anti-correlated with
what deserves attention.  A -30% drop in bgp is a catastrophe; -30% in gtr is
about 1.5 normal evenings.  So every sensor is judged against ITS OWN swing, and
the percentage is kept only because it is the human-readable magnitude.

AND EVERY RULER IS MEASURED ON THE COMPARISON IT JUDGES.  The short baseline's
deviation is a same-clock-hour comparison, which has already had the diurnal
cycle taken out of it, so its swing is measured the same way: that same
comparison replayed hourly across the day, and the spread of those.  Measuring
it over 24h of RAW samples instead measured the day/night amplitude and inflated
the bar by exactly that much — far enough that two sensors needed a drop past
-100% to vote and therefore could not vote at all, on a scale where -100% is a
country gone completely dark (see RULER_SPAN).

A TRAILING 24h BASELINE CANNOT SEE A SUSTAINED DECLINE, because the baseline
sits inside the outage and normalises to it.  Cuba, gtr daily medians:

    09-12  1,999,130,989    +0.0%
    09-14  1,571,972,916   -21.4%
    09-16  1,360,037,448   -32.0%
    09-18    967,628,228   -51.6%
    09-19    721,088,838   -63.9%    and still falling

24h scoring reports that Cuba as nominal.  State-ordered shutdowns last DAYS, so
a detector that only sees sudden drops misses essentially every deliberate one.
Hence TWO baselines, short and long, scored separately and never blended: a
country can be quiet on 24h and collapsing over the week, and the row has to be
able to say exactly that.

WHAT THE LONG BASELINE CANNOT SEE, stated plainly: a PERFECTLY STEADY decline.
One day against the median of the six before it, divided by the spread of those
six, is pinned at z = -2.33 for a straight line of ANY slope — a trend inflates
its own denominator exactly as fast as it moves the numerator, so -10%/week and
-80%/week score identically and neither reaches -4.0.  What fires is a STEP
inside the week, which is what Cuba is (a flat-ish week and then a cliff), not a
smooth drain.  Detrending the six days before measuring their spread was tried
and MEASURED: over 259 replayed country-days it doubled the fires (11 -> 23) and
lit Iranian and Israeli weekend cycles at z = -10 to -12, because a Theil-Sen
line through six points absorbs almost anything.  Six daily medians cannot
support both a trend estimate and a ruler built from the residue.  So the week's
slope is REPORTED instead (trend_per_day) and never voted with, and the
seven-point trace draws the shape the z cannot judge.

THE SENSORS DISAGREE INFORMATIVELY, and that is the point.  Cuba right now: bgp
flat at 791 all week (routing intact, ASes still announced), ping-slash24 -9%,
gtr -64%.  Routing untouched while traffic drains is NOT a government pulling
the plug — a shutdown withdraws routes and bgp drops with them.  It is
demand-side collapse, and Cloudflare Radar independently calls that same outage
POWER_OUTAGE / NATIONWIDE.  Different event, different meaning.  Collapsing the
four into one score would erase the only evidence that distinguishes them.
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
    # Cuba is the VALIDATION CASE, not a theatre.  It is the one watched country
    # with an independently corroborated ongoing disruption (Cloudflare Radar has
    # carried a nationwide outage annotation for it since 2026-09-18), so it is
    # the only row that can prove the detector fires at all.  Nine nominal rows
    # prove the thing does not invent drama; they do not prove it can see.
    ("CU", "Cuba"),
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
SPARK_POINTS = 48       # never ship 577 raw points per sensor per country

# --- the ruler -------------------------------------------------------------
#
# A sensor is depressed when it is far outside ITS OWN normal swing, not when it
# crosses a shared percentage.  typical = MAD/median (scale-free, and MAD not
# stdev because one 10x spike would otherwise inflate the ruler it is measured
# against); robust_z = deviation / typical.
#
# -4.0 because the measured quiet-day z values across nine countries and four
# sensors sit between -2.6 and +1.8 (table above), and the largest single
# quiet-day excursion found in the long baseline was -3.05 (RU merit-nt).  -4.0
# clears the observed noise floor without needing the drop to be total.
Z_DEPRESSED = -4.0

# A sensor with no swing has no z: deviation/0 is undefined, and 0.0 or infinity
# would both be inventions.  Its z is reported as null and it is judged by an
# ABSOLUTE rule instead, because a flat sensor moving AT ALL is meaningful — bgp
# sits on the same integer for a week, so 2% off it is hundreds of prefixes
# withdrawn, which is the shape of a state-ordered shutdown.
FLAT_ABSOLUTE = -0.02

# ...and "flat" has to be a floor, not equality, because near-flat is the same
# pathology wearing a rounding error.  MEASURED 2026-09-19 on the long baseline:
# Israel's bgp moved -0.16% with a typical swing of 0.02% (z = -7.2) and
# Ukraine's ping-slash24 moved -0.54% with a swing of 0.06% (z = -9.2).  Both
# would have voted depressed on a ratio whose denominator is rounding error, and
# both countries would have read "partial sustained disruption" on a quiet day.
# The floor is 0.005 because that is where the two rules AGREE: at typical=0.005,
# z <= -4.0 and deviation <= -0.02 are the same test, so there is no step at the
# boundary and nothing to game.  Below it the sensor is flat and judged
# absolutely; above it, by z.
TYPICAL_FLOOR = FLAT_ABSOLUTE / Z_DEPRESSED  # 0.005
MIN_VARIANCE_POINTS = 4  # fewer samples than this cannot estimate a swing

# The short baseline's ruler is ITS OWN COMPARISON, replayed hourly across the
# day: one probe an hour, each one the same current-3h-against-3h-yesterday test
# the live reading makes, and the swing is the spread of those probes.
#
# It used to be the MAD/median of 24h of raw samples — the DIURNAL AMPLITUDE —
# while the numerator it divided was a same-clock-hour comparison with the
# diurnal cycle already taken out.  MEASURED over 888 replayed country-hours,
# what each ruler implies as a trigger:
#
#     sensor         raw-24h ruler   same-hour ruler   threshold then / now
#     RU gtr             9.8%             3.7%            -39%  /  -15%
#     YE gtr            27.8%             4.4%           -111%  /  -18%
#     IQ merit-nt       26.2%            12.3%           -105%  /  -49%
#
# A deviation floors at -100% when a country goes completely dark, so YE gtr and
# IQ merit-nt were wired in, counted as available, and could not vote under any
# physically possible reading.  On the same replay the corrected ruler fires 39
# sensor-hours against 2, and the corroboration rule absorbs all but Cuba: 236 of
# 240 country-hours stay nominal, and the four that do not are the one country
# with an independently corroborated ongoing outage.
#
# The span is 22h because a probe needs its own 24h-earlier baseline and the
# fetch window is 48h; the stride is an hour because the sensors step at 5-30
# minutes and neighbouring probes overlap anyway.
RULER_STRIDE = 3600
RULER_SPAN = 22 * 3600
MIN_RULER_PROBES = 8  # fewer probes than this cannot estimate a spread

# --- the long baseline -----------------------------------------------------
#
# recent 24h vs the median of days 2-7.  Seven days is the shortest window that
# holds a week-long decline AND leaves six baseline days to judge it against.
LONG_WINDOW_SECONDS = 7 * DAY_SECONDS
LONG_POLL_INTERVAL = 3600
# 7 days of bgp at its native 300s is ~2000 points per sensor per country; ten
# countries every 300s would be 40k+ points a cycle against a free academic
# service that has already IP-banned this host once (CelesTrak, for exactly
# this).  IODA's raw endpoint accepts maxPoints and downsamples server-side —
# VERIFIED 2026-09-19: 7d of CU/bgp returns 2017 points at step 300 without it
# and 169 points at step 3600 with maxPoints=200.  400 buys uniform 30-minute
# resolution (336 points) across all four sensors, which is finer than the daily
# medians this baseline is built on and still 6x less data than native.
LONG_MAX_POINTS = 400
MIN_LONG_DAY_POINTS = 3   # samples needed before a day counts as measured
MIN_LONG_BASELINE_POINTS = 10
MIN_LONG_DAYS = 4  # of the six baseline days, how many must be measurable

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
# A datasource not in that table has no measured floor, and 0.0 would be the
# wrong default for a guard whose entire job is to stop a division by a
# meaningless denominator: `0.0 < 0.0` is False, so the unknown sensor would sail
# past the guard and divide by zero.  DATASOURCES is a constant so this is
# unreachable today; it is the default that is wrong, not the caller.
MIN_BASELINE_FALLBACK = 1.0

# --- Cloudflare Radar ------------------------------------------------------
#
# A SECOND ORGANISATION measuring the same thing by a different method.  Radar
# sees Cloudflare's own request volume; IODA sees routing, probes and a
# telescope.  When they agree the reading is stronger than either alone — but
# the way to say that is to report both, not to average them into a number that
# claims a precision neither has.  Radar annotations therefore sit in their own
# field and NEVER enter sensors_depressed.
RADAR_BASE = "https://api.cloudflare.com/client/v4"
RADAR_POLL_INTERVAL = 900
RADAR_ANNOTATION_LIMIT = 50
RADAR_ATTACK_LIMIT = 10
RADAR_DISCOVERY_LIMIT = 10  # a short global list, not a world feed

_cache: dict = {
    "countries": [],
    "timestamp": 0,
    "status": "no_data",
}

# The long baseline and Radar live on their own slower clocks and are cached
# between refreshes; the 300s short poll reads whatever is here without
# re-fetching.  That is the whole rate-limit story.
_long_cache: dict = {"sensors": {}, "timestamp": 0, "status": "no_data"}
# Radar is TWO calls, and they fail independently.  One combined status word
# collapsed "stale outages, fresh attacks" into the same "degraded" as its
# mirror image, and the outage half of that is the dangerous one: it let an
# empty annotation list from a call that never happened read as "Cloudflare
# reports no outage" on the one country added to prove the detector can see.
# Each branch therefore carries its own status and its own timestamp.
_radar_cache: dict = {
    "timestamp": 0,
    "status": "no_data",
    "error": None,
    "by_country": {},
    "discovery": [],
    "attacks": None,
    "outages_status": "no_data",   # no_data | unconfigured | ok | stale | error
    "outages_as_of": 0,
    "attacks_status": "no_data",
    "attacks_as_of": 0,
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


def _clean(raw: list) -> tuple[list, int]:
    """Coerce IODA's values to floats-or-None.  Returns (values, bad_count).

    NaN and Infinity are legal to json.loads, and one NaN reaching the response
    body 500s this endpoint for every country — while `nan <= -0.30` is False, so
    the sensor would count as available and calm on the way there.  Anything that
    is not a finite number is "not measured".
    """
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
    return values, bad


def _typical(values: list) -> float | None:
    """This sensor's own normal swing: MAD / median, scale-free.

    None when it cannot be estimated at all (too few samples, or a level of
    zero).  None is not "flat" — flat is a measured zero — and a sensor whose
    ruler cannot be built does not get to vote either way.
    """
    live = [v for v in values if v is not None]
    if len(live) < MIN_VARIANCE_POINTS:
        return None
    med = median(live)
    if med == 0:
        return None
    return median([abs(x - med) for x in live]) / med


def _compare(live: list, t_end: int, floor: float) -> tuple:
    """The ONE comparison the short baseline makes, at any point in the window.

    Returns (reason, baseline, current, deviation); reason is None on success and
    names the defect otherwise.  The live reading and every probe in its ruler
    both come through here, so the ruler can never end up measuring a different
    quantity from the thing it judges — which is precisely how a deseasonalised
    deviation came to be divided by a diurnal swing.
    """
    current_pts = [v for t, v in live if t_end - CURRENT_WINDOW < t <= t_end]
    centre = t_end - DAY_SECONDS
    baseline_pts = [v for t, v in live if abs(t - centre) <= SEASONAL_HALF]
    if len(current_pts) < MIN_CURRENT_POINTS or len(baseline_pts) < MIN_BASELINE_POINTS:
        return "insufficient_points", None, None, None
    baseline = median(baseline_pts)
    current = median(current_pts)
    if baseline < floor:
        # Too small a denominator to carry a ratio, so this sensor cannot
        # corroborate anything.  Returning "not depressed" here would be a claim.
        return "low_baseline", baseline, current, None
    return None, baseline, current, (current - baseline) / baseline


def _swing(live: list, last_t: int, floor: float) -> float | None:
    """This sensor's own normal swing ON THE SHORT COMPARISON: the spread of the
    same test, run once an hour across the day.

    MAD about the median of those probes, not about zero: the question is how
    unusual THIS hour's day-over-day move is among the day's other hours.  A day
    that is uniformly down therefore has a tight ruler and a large z, which is
    the detector agreeing with itself — and is why two sensors are still
    required before any of it is called a disruption.
    """
    devs = []
    for offset in range(RULER_STRIDE, RULER_SPAN + 1, RULER_STRIDE):
        reason, _b, _c, dev = _compare(live, last_t - offset, floor)
        if reason is None:
            devs.append(dev)
    if len(devs) < MIN_RULER_PROBES:
        return None
    mid = median(devs)
    return median([abs(d - mid) for d in devs])


def _slope(values: list) -> float | None:
    """Theil-Sen: the median of every pairwise slope, over evenly spaced days.

    REPORTED, NEVER VOTED WITH.  Six daily medians cannot support both a trend
    estimate and a ruler built from what is left over (module docstring), so this
    is the shape of the week made visible, not a second detector.
    """
    pts = [(i, v) for i, v in enumerate(values) if v is not None]
    if len(pts) < 2:
        return None
    slopes = [(b - a) / (j - i) for i, a in pts for j, b in pts if j > i]
    return median(slopes)


def _verdict(deviation: float, typical: float | None) -> tuple[bool, float | None, str]:
    """(depressed, robust_z, z_basis) for one reading against its own swing."""
    if typical is None:
        # No ruler, so no claim: it cannot corroborate and it cannot object.
        return False, None, "unavailable"
    if typical < TYPICAL_FLOOR:
        # Flat sensor: z would be a ratio over rounding error.  Report no z at
        # all and judge the move absolutely (see TYPICAL_FLOOR).
        return deviation <= FLAT_ABSOLUTE, None, "flat-sensor-absolute"
    z = deviation / typical
    return z <= Z_DEPRESSED, round(z, 2), "relative"


def _sensor(available, reason, baseline, current, deviation, depressed,
            points, spark, data_age=None, typical=None, robust_z=None,
            z_basis="unavailable", trend=None) -> dict:
    return {
        "available": available,
        "reason": reason,
        "baseline": baseline,
        "current": current,
        # Kept because it is the human-readable magnitude — but it no longer
        # decides anything.  robust_z does.
        "deviation": deviation,
        # This sensor's own normal swing (MAD/median), the denominator of z.
        "typical": typical,
        "robust_z": robust_z,
        # "relative" (judged by z), "flat-sensor-absolute" (no swing to divide
        # by, judged by FLAT_ABSOLUTE) or "unavailable".  The UI never has to
        # guess why a null z is null.
        "z_basis": z_basis,
        # Long baseline only: the week's slope as a fraction of the baseline per
        # day, from a Theil-Sen fit over the daily medians.  A steady decline is
        # the one thing robust_z cannot see, so the slope is MEASURED AND SHOWN
        # rather than folded into a vote it cannot support (module docstring).
        "trend_per_day": trend,
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

    values, bad = _clean(raw)

    # The trace shows the last 24h; the day before it exists to be a baseline.
    keep = int(DAY_SECONDS // step) + 1
    trace = values[-keep:]
    spark = _downsample(trace)

    live = [i for i, v in enumerate(values) if v is not None]
    points = len(live)
    if not live:
        return _sensor(False, "bad_values" if bad else "all_null",
                       None, None, None, False, 0, spark)

    last_t = frm + live[-1] * step
    data_age = max(0, until - last_t)
    if data_age > max(STALE_STEPS * step, STALE_FLOOR):
        return _sensor(False, "stale_series", None, None, None, False, points, spark, data_age)

    live_pts = [(frm + i * step, values[i]) for i in live]
    floor = MIN_BASELINE.get(datasource, MIN_BASELINE_FALLBACK)
    reason, baseline, current, deviation = _compare(live_pts, last_t, floor)
    if reason == "insufficient_points":
        return _sensor(False, reason, None, None, None, False, points, spark, data_age)
    if reason:
        return _sensor(False, reason, round(baseline, 2), round(current, 2),
                       None, False, points, spark, data_age)

    # The swing is that same comparison replayed across the day, because the
    # deviation above is a same-clock-hour reading and its ruler has to be the
    # spread of same-clock-hour readings.  The 24h trace is drawn, not measured
    # with: its MAD is the diurnal amplitude and using it put two sensors' firing
    # thresholds past -100% (see RULER_SPAN).
    typical = _swing(live_pts, last_t, floor)
    if typical is None:
        return _sensor(False, "no_variance_estimate", round(baseline, 2),
                       round(current, 2), None, False, points, spark, data_age)

    depressed, robust_z, z_basis = _verdict(deviation, typical)
    return _sensor(
        True, None, round(baseline, 2), round(current, 2),
        round(deviation, 4), depressed, points, spark, data_age,
        round(typical, 5), robust_z, z_basis,
    )


def _score_long(series: dict | None, until: int, datasource: str) -> dict:
    """Score the last 24h against the median of days 2-7.

    This is the detector for a decline that a trailing 24h baseline cannot see,
    because after a day of outage the 24h baseline IS the outage.

    The swing here is measured over DAILY MEDIANS, not raw samples, because the
    comparison itself is day-against-days: gtr's raw MAD over a week is ~36%,
    which is its day/night cycle, and dividing a sustained decline by the diurnal
    swing hides exactly the event this function exists to find.  Measured on
    Cuba: raw-sample swing puts the gtr collapse at z = -1.0 (invisible), daily
    medians put it at z = -4.9 (fires).

    It detects a STEP INSIDE THE WEEK, not a straight line: one day against the
    median of the six before it is pinned at z = -2.33 for a steady decline of
    any slope, because the trend inflates its own denominator (module docstring,
    which also records the detrending that was tried and measured).  The slope is
    reported as trend_per_day for exactly that reason.
    """
    if series is None:
        return _sensor(False, "fetch_failed", None, None, None, False, 0, [])

    raw = series.get("values") or []
    step = series.get("step") or 0
    frm = series.get("from")
    if not raw or not step or frm is None:
        return _sensor(False, "no_series", None, None, None, False, 0, [])

    values, bad = _clean(raw)
    live = [(frm + i * step, v) for i, v in enumerate(values) if v is not None]
    points = len(live)
    if not live:
        return _sensor(False, "bad_values" if bad else "all_null",
                       None, None, None, False, 0, [])

    last_t = live[-1][0]
    data_age = max(0, until - last_t)
    if data_age > max(STALE_STEPS * step, STALE_FLOOR):
        return _sensor(False, "stale_series", None, None, None, False, points, [], data_age)

    recent_pts = [v for t, v in live if t > last_t - DAY_SECONDS]
    baseline_pts = [v for t, v in live
                    if last_t - LONG_WINDOW_SECONDS < t <= last_t - DAY_SECONDS]

    # One median per day, oldest first, with today's on the end.  This doubles as
    # the sparkline: seven numbers ARE the week, and downsampling 336 raw samples
    # would redraw the diurnal cycle instead of the trend.  A day held to the
    # same MIN_LONG_DAY_POINTS bar as every other day, today included: a final
    # bucket drawn from one sample would sit in the trace with the same weight as
    # six daily medians and read as the week's last word.
    dailies = []
    for d in range(6, 0, -1):
        chunk = [v for t, v in live
                 if last_t - (d + 1) * DAY_SECONDS < t <= last_t - d * DAY_SECONDS]
        dailies.append(median(chunk) if len(chunk) >= MIN_LONG_DAY_POINTS else None)
    today = median(recent_pts) if len(recent_pts) >= MIN_LONG_DAY_POINTS else None
    week = dailies + [today]
    spark = [None if v is None else round(v, 2) for v in week]

    if len(recent_pts) < MIN_LONG_DAY_POINTS or len(baseline_pts) < MIN_LONG_BASELINE_POINTS:
        return _sensor(False, "insufficient_points", None, None, None, False, points, spark, data_age)

    baseline = median(baseline_pts)
    current = median(recent_pts)
    if baseline < MIN_BASELINE.get(datasource, MIN_BASELINE_FALLBACK):
        return _sensor(False, "low_baseline", round(baseline, 2), round(current, 2),
                       None, False, points, spark, data_age)

    measured_days = [d for d in dailies if d is not None]
    if len(measured_days) < MIN_LONG_DAYS:
        return _sensor(False, "insufficient_days", round(baseline, 2), round(current, 2),
                       None, False, points, spark, data_age)

    # Over the SAME denominator the deviation uses.  Dividing the spread of the
    # daily medians by the median of the daily medians, while the ratio it rules
    # on divides by the median of the raw baseline points, is two different
    # quantities wearing one name — 0.4% apart on Cuba today, and further apart
    # the more uneven the sampling gets.
    spread = _typical(measured_days)
    typical = None if spread is None else spread * median(measured_days) / baseline
    if typical is None:
        return _sensor(False, "no_variance_estimate", round(baseline, 2),
                       round(current, 2), None, False, points, spark, data_age)

    deviation = (current - baseline) / baseline
    depressed, robust_z, z_basis = _verdict(deviation, typical)
    # The week's slope, reported and never voted with: a steady decline is pinned
    # at z = -2.33 whatever its slope, so this is the one number that can say
    # "still falling" when the z cannot (module docstring).
    slope = _slope(week)
    return _sensor(
        True, None, round(baseline, 2), round(current, 2),
        round(deviation, 4), depressed, points, spark, data_age,
        round(typical, 5), robust_z, z_basis,
        None if slope is None else round(slope / baseline, 4),
    )


def _agree(depressed: int, available: int) -> bool:
    """Do the sensors that can see AGREE that this country is down?

    A MAJORITY OF THE AVAILABLE SENSORS, never fewer than two.  It used to be a
    flat 3, which quietly meant "3 of 4" — and when a sensor is unavailable, an
    absolute 3 stops being a majority and becomes unreachable.  Cuba is the case:
    merit-nt reads 0.1 packets (below MIN_BASELINE, it cannot carry a ratio) and
    bgp is flat at 791 all week because the routes are still announced, which
    FACT 3 says is the informative part.  Three sensors available, two of them
    down over the week, and Cloudflare Radar independently carrying a nationwide
    outage annotation for the same country — and the absolute rule called that
    "partial", the same word it gives a country where half the sensors disagree.
    The evidence is AGREEMENT: how many of the instruments that can see it do,
    against how many could have and did not.  2 of 3 is agreement; 2 of 4 is not.
    Measured across 240 replayed country-hours and 70 country-days, this changes
    exactly one verdict, and it is the corroborated one.
    """
    return depressed >= 2 and depressed * 2 > available


def _pending(reason: str) -> dict:
    """A long sensor that has not been fetched yet is not a measurement."""
    return {ds: _sensor(False, reason, None, None, None, False, 0, []) for ds in DATASOURCES}


def _build_country(code: str, name: str, sensors: dict,
                   sensors_long: dict | None = None,
                   corroboration: dict | None = None) -> dict:
    """Apply the corroboration model across the four sensors, on both baselines.

    Short and long are counted SEPARATELY and never blended: a sudden cut and a
    week-long strangulation are different events, and a score that averaged them
    would name neither.
    """
    sensors_long = sensors_long or _pending("not_polled")

    available = [s for s in sensors.values() if s["available"]]
    depressed = [s for s in available if s["depressed"]]
    long_available = [s for s in sensors_long.values() if s["available"]]
    long_depressed = [s for s in long_available if s["depressed"]]

    if _agree(len(depressed), len(available)):
        state = "disruption_sudden"
        basis = "short"
    elif _agree(len(long_depressed), len(long_available)):
        state = "disruption_sustained"
        basis = "long"
    elif len(depressed) >= 2 or len(long_depressed) >= 2:
        # Two sensors agree, but so do the ones that stayed quiet.  Which
        # baseline fired is the difference between "something just happened" and
        # "this has been draining for a week", so the row gets to say it rather
        # than infer it from two counts.
        state = "partial"
        if len(depressed) >= 2 and len(long_depressed) >= 2:
            basis = "both"
        elif len(depressed) >= 2:
            basis = "short"
        else:
            basis = "long"
    elif len(available) >= 2:
        # One sensor depressed alone is a measurement artifact, not an outage.
        state = "nominal"
        basis = None
    else:
        # NOMINAL IS A CLAIM, and this is the coverage it needs.  Gating degraded
        # on "neither baseline has 2 sensors" let a country whose entire 24h
        # fetch failed read "nominal" off an hour-old 7-day cache — nothing is
        # measuring it today and the payload said all clear.  A verdict that
        # fired off the long baseline still stands above (a week of evidence is
        # evidence), but "nothing is wrong" has to come from the short one.
        state = "degraded"
        basis = None

    return {
        "code": code,
        "name": name,
        "sensors": sensors,
        "sensors_available": len(available),
        "sensors_depressed": len(depressed),
        "sensors_long": sensors_long,
        "sensors_long_available": len(long_available),
        "sensors_long_depressed": len(long_depressed),
        "state": state,
        # "short", "long", "both" or null — which baseline the state came off.
        "basis": basis,
        # None, not 0.0, when nothing is measurable: 0.0 would read as
        # "measured, no change" — the false-nominal this design refuses.
        "worst_deviation": min((s["deviation"] for s in available), default=None),
        "worst_deviation_long": min((s["deviation"] for s in long_available), default=None),
        # A different organisation, measuring by a different method.  Reported
        # beside the sensors, never counted among them.
        "corroboration": corroboration or {
            "source": "cloudflare_radar", "available": False, "stale": False,
            "as_of": 0, "ongoing_outage": False, "outages": [], "independent": True,
        },
        "as_of": int(time.time()),
    }


async def _fetch_series(client: httpx.AsyncClient, code: str, datasource: str,
                        frm: int, until: int, max_points: int | None = None) -> dict | None:
    """One country, one datasource.  None means IODA did not answer.

    An empty dict means it answered but holds no series for this entity, which
    is a different fact and gets a different reason.  max_points asks IODA to
    downsample server-side, which is how the 7-day window stays affordable.
    """
    params = {"from": frm, "until": until, "datasource": datasource}
    if max_points:
        params["maxPoints"] = max_points
    try:
        resp = await client.get(
            f"{IODA_BASE}/v2/signals/raw/country/{code}",
            params=params,
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
    """Returns the four short-baseline sensors plus how many IODA answered for."""
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
    return sensors, answered


async def _refresh_long(client: httpx.AsyncClient) -> None:
    """Fetch and score the 7-day baseline for every watched country.

    Runs on LONG_POLL_INTERVAL, not POLL_INTERVAL.  A week of data does not
    change meaningfully in five minutes, and re-fetching it at the short cadence
    would be 12x the load on a free service for no new information.
    """
    until = int(time.time())
    frm = until - LONG_WINDOW_SECONDS
    scored: dict = {}
    answered = 0
    for code, name in WATCHED:
        sensors = {}
        for datasource in DATASOURCES:
            series = await _fetch_series(client, code, datasource, frm, until,
                                         max_points=LONG_MAX_POINTS)
            if series is not None:
                answered += 1
            try:
                sensors[datasource] = _score_long(series, until, datasource)
            except Exception as e:
                logger.error("Connectivity long: %s/%s scoring failed: %s", code, datasource, e)
                sensors[datasource] = _sensor(False, "score_failed", None, None, None, False, 0, [])
        scored[code] = sensors

    _long_cache["sensors"] = scored
    _long_cache["timestamp"] = until
    _long_cache["status"] = "ok" if answered else "unavailable"
    fired = sum(1 for s in scored.values() for x in s.values() if x["depressed"])
    logger.info(
        "Connectivity long: 7d baseline refreshed for %d countries (%d/%d series answered, "
        "%d sensors depressed) — next in %ds",
        len(scored), answered, len(WATCHED) * len(DATASOURCES), fired, LONG_POLL_INTERVAL,
    )


def _annotation(a: dict) -> dict:
    """The parts of a Radar outage annotation worth carrying, and no more."""
    outage = a.get("outage") or {}
    return {
        "id": a.get("id"),
        "locations": a.get("locations") or [],
        "event_type": a.get("eventType"),
        "start": a.get("startDate"),
        "end": a.get("endDate"),
        # No end date means Cloudflare still considers it running.
        "ongoing": a.get("endDate") is None,
        # Radar states a CAUSE, which IODA never does.  POWER_OUTAGE against
        # GOVERNMENT_DIRECTED is the difference between a grid failing and a
        # state pulling the plug, and it is the field that makes this a
        # corroborator rather than a second opinion.
        "cause": outage.get("outageCause"),
        "scope": outage.get("outageType"),
        "asns": a.get("asns") or [],
    }


async def _refresh_radar(client: httpx.AsyncClient) -> None:
    """Poll Cloudflare Radar for outage annotations and layer-7 attack targets.

    Its own cadence (RADAR_POLL_INTERVAL), cached between refreshes.  A missing
    token is reported as unconfigured, a failure as an error — never retried in a
    loop, never filled in with a guess.
    """
    token = settings.cloudflare_radar_token
    if not token:
        _radar_cache.update(status="unconfigured", error=None, timestamp=int(time.time()),
                            outages_status="unconfigured", attacks_status="unconfigured")
        return

    headers = {"Authorization": f"Bearer {token}"}
    watched = {code for code, _ in WATCHED}
    by_country: dict = {}
    discovery: list = []
    attacks = None
    errors = []
    got_outages = False
    got_attacks = False

    try:
        resp = await client.get(
            f"{RADAR_BASE}/radar/annotations/outages",
            params={"limit": RADAR_ANNOTATION_LIMIT}, headers=headers,
        )
        if resp.status_code != 200:
            errors.append(f"outages HTTP {resp.status_code}")
        else:
            anns = (resp.json().get("result") or {}).get("annotations") or []
            for a in anns:
                entry = _annotation(a)
                hits = [c for c in entry["locations"] if c in watched]
                for code in hits:
                    by_country.setdefault(code, []).append(entry)
                # Everything NOT watched is the discovery channel: Cuba was found
                # this way, and a watchlist can only ever confirm what it already
                # suspects.  Ongoing first, then most recent.
                if not hits and entry["locations"]:
                    discovery.append(entry)
            # Ongoing first, then most recent.
            discovery.sort(key=lambda e: (e["ongoing"], e["start"] or ""), reverse=True)
            discovery = discovery[:RADAR_DISCOVERY_LIMIT]
            got_outages = True
    except Exception as e:
        errors.append(f"outages {e}")

    try:
        resp = await client.get(
            f"{RADAR_BASE}/radar/attacks/layer7/top/locations/target",
            params={"dateRange": "1d", "limit": RADAR_ATTACK_LIMIT}, headers=headers,
        )
        if resp.status_code != 200:
            errors.append(f"attacks HTTP {resp.status_code}")
        else:
            result = resp.json().get("result") or {}
            meta = result.get("meta") or {}
            attacks = {
                "date_range": "1d",
                # The denominator is the point.  This is a SHARE of observed
                # layer-7 attack traffic to Cloudflare, not a count of attacks
                # and not an arc drawn between two capitals.
                "units": meta.get("units"),
                "normalization": meta.get("normalization"),
                "last_updated": meta.get("lastUpdated"),
                "targets": [
                    {
                        "code": t.get("targetCountryAlpha2"),
                        "name": t.get("targetCountryName"),
                        "share": float(t["value"]) if t.get("value") is not None else None,
                        "rank": t.get("rank"),
                    }
                    for t in (result.get("top_0") or [])
                ],
            }
            got_attacks = True
    except Exception as e:
        errors.append(f"attacks {e}")

    if got_outages and got_attacks:
        status = "ok"
    elif got_outages or got_attacks:
        status = "degraded"
    else:
        status = "error"
    # A call that FAILED does not get to overwrite what a call that succeeded
    # said.  Writing the empty dict here would turn "Cloudflare did not answer"
    # into "Cloudflare reports no outage", which is the false-nominal this whole
    # service is built to refuse — and it would do it silently, one HTTP 500 at
    # a time, on the one country the detector was added to watch.
    #
    # ...and keeping the last-good annotations is only half of it: each branch
    # also has to SAY which of the two it is serving.  One combined status made
    # "stale outages, fresh attacks" indistinguishable from its mirror image, so
    # a reader of the payload could not tell a preserved annotation from a fresh
    # one, and the guard's whole benefit was thrown away one field later.
    now = int(time.time())
    _radar_cache.update(
        timestamp=now,
        status=status,
        error="; ".join(errors) or None,
    )
    if got_outages:
        _radar_cache.update(by_country=by_country, discovery=discovery,
                            outages_status="ok", outages_as_of=now)
    else:
        _radar_cache["outages_status"] = "stale" if _radar_cache["outages_as_of"] else "error"
    if got_attacks:
        _radar_cache.update(attacks=attacks, attacks_status="ok", attacks_as_of=now)
    else:
        _radar_cache["attacks_status"] = "stale" if _radar_cache["attacks_as_of"] else "error"
    cached_attacks = _radar_cache["attacks"]
    logger.info(
        "Connectivity radar: %d watched countries annotated, %d discovery annotations, "
        "%d attack targets (outages=%s attacks=%s%s) — next in %ds",
        len(_radar_cache["by_country"]), len(_radar_cache["discovery"]),
        len(cached_attacks["targets"]) if cached_attacks else 0,
        _radar_cache["outages_status"], _radar_cache["attacks_status"],
        "" if not errors else ": " + "; ".join(errors),
        RADAR_POLL_INTERVAL,
    )


def _corroboration(code: str) -> dict:
    """Radar's view of one watched country, kept beside the sensors."""
    # Keyed off the OUTAGES call specifically, not off a combined status: the
    # annotations branch can fail while the attacks branch succeeds, and the
    # combined word for that was "degraded", which this function used to read as
    # good enough.  An empty outage list from a call that never happened would
    # read as "measured, no outage" — measured-and-clean on the one country added
    # to prove the detector can see.  "stale" is served, and says so.
    if _radar_cache["outages_status"] not in ("ok", "stale"):
        return {"source": "cloudflare_radar", "available": False, "stale": False,
                "as_of": _radar_cache["outages_as_of"],
                "ongoing_outage": False, "outages": [], "independent": True}
    outages = _radar_cache["by_country"].get(code, [])
    return {
        "source": "cloudflare_radar",
        "available": True,
        # True means these are the last good annotations, not this poll's.
        "stale": _radar_cache["outages_status"] == "stale",
        "as_of": _radar_cache["outages_as_of"],
        "ongoing_outage": any(o["ongoing"] for o in outages),
        "outages": outages,
        # Two organisations with different methods agreeing is stronger than
        # either alone — which is why this is reported alongside the IODA count
        # and never folded into it.
        "independent": True,
    }


async def start_connectivity_poller():
    """Background task that polls IODA every POLL_INTERVAL seconds.

    The 7-day baseline and Cloudflare Radar run on their own slower clocks
    inside the same loop and are served from cache in between.
    """
    logger.info(
        "Connectivity: polling IODA for %d countries x %d sensors every %ds "
        "(7d baseline every %ds, Cloudflare Radar every %ds)",
        len(WATCHED), len(DATASOURCES), POLL_INTERVAL,
        LONG_POLL_INTERVAL, RADAR_POLL_INTERVAL,
    )
    if not settings.cloudflare_radar_token:
        logger.info("Connectivity: Cloudflare Radar unconfigured — skipped, not failed")

    async with httpx.AsyncClient(
        timeout=30,
        headers={"User-Agent": IODA_USER_AGENT},
    ) as client:
        last_long = 0.0
        last_radar = 0.0
        while True:
            try:
                now = time.time()

                if now - last_long >= LONG_POLL_INTERVAL:
                    # Stamped whether or not it succeeded: a failed 7-day refresh
                    # waits out the full hour rather than retrying every 300s.
                    # The cached sensors from the last good refresh stand.
                    last_long = now
                    try:
                        await _refresh_long(client)
                    except Exception as e:
                        logger.error("Connectivity long refresh failed: %s", e)

                if now - last_radar >= RADAR_POLL_INTERVAL:
                    last_radar = now
                    try:
                        await _refresh_radar(client)
                    except Exception as e:
                        logger.error("Connectivity radar refresh failed: %s", e)
                        _radar_cache.update(
                            status="error", error=str(e), timestamp=int(time.time()),
                            # Same rule as a single failed branch: what was
                            # fetched still stands, and is labelled as stale.
                            outages_status="stale" if _radar_cache["outages_as_of"] else "error",
                            attacks_status="stale" if _radar_cache["attacks_as_of"] else "error",
                        )

                until = int(time.time())
                frm = until - WINDOW_SECONDS
                countries = []
                answers = 0

                for code, name in WATCHED:
                    try:
                        sensors, answered = await _measure_country(client, code, name, frm, until)
                    except Exception as e:
                        # One country must never take down the cycle or the task.
                        logger.error("Connectivity: %s measurement failed: %s", code, e)
                        sensors = {ds: _score_series(None, until, ds) for ds in DATASOURCES}
                        answered = 0
                    # The short sensors are fresh; the long baseline and the Radar
                    # view come from their own caches, so the verdict sees both
                    # clocks without re-fetching either.
                    countries.append(_build_country(
                        code, name, sensors,
                        _long_cache["sensors"].get(code),
                        _corroboration(code),
                    ))
                    answers += answered

                _cache["countries"] = countries
                _cache["timestamp"] = int(time.time())
                # Not one answer out of 40 requests means IODA itself is gone.
                _cache["status"] = "ok" if answers else "unavailable"

                states = [c["state"] for c in countries]
                logger.info(
                    "Connectivity: %d countries — %d sudden, %d sustained, %d partial, "
                    "%d degraded (status=%s)",
                    len(countries),
                    states.count("disruption_sudden"),
                    states.count("disruption_sustained"),
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
        "long_poll_interval": LONG_POLL_INTERVAL,
        "long_as_of": _long_cache["timestamp"],
        "long_status": _long_cache["status"],
        "countries": _cache["countries"],
        # A second organisation, different method.  Unconfigured is reported as
        # unconfigured — not faked, not errored, not retried.
        "cloudflare": {
            "configured": bool(settings.cloudflare_radar_token),
            "status": _radar_cache["status"],
            "error": _radar_cache["error"],
            "as_of": _radar_cache["timestamp"],
            "poll_interval": RADAR_POLL_INTERVAL,
            # Two calls that fail independently, so two statuses: "ok" | "stale"
            # (last good, this poll's call failed) | "error" | "unconfigured" |
            # "no_data", each with the timestamp of the answer being served.
            "outages_status": _radar_cache["outages_status"],
            "outages_as_of": _radar_cache["outages_as_of"],
            "attacks_status": _radar_cache["attacks_status"],
            "attacks_as_of": _radar_cache["attacks_as_of"],
            # Outages in countries NOT on the watchlist: the discovery channel.
            "discovery": _radar_cache["discovery"],
            "attacks": _radar_cache["attacks"],
        },
    }
