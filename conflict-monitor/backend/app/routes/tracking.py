from fastapi import APIRouter

from app.services.connectivity import get_connectivity
from app.services.maritime import get_vessels_envelope
from app.services.opensky import get_aircraft_envelope, get_jamming_zones
from app.services.satellites import get_tles
from app.services.track_history import get_aircraft_tracks, get_vessel_tracks

router = APIRouter(prefix="/tracking", tags=["tracking"])


@router.get("/aircraft")
async def aircraft():
    """Aircraft positions, wrapped in the feed envelope.

    `items` is the last good fleet; `state` says what it is worth (live,
    retrying, stale, unavailable, ...). `count` is null unless the feed is live
    or stale, so a dead feed cannot print "0 AC". See app/feeds.py.
    """
    return get_aircraft_envelope()


@router.get("/tle")
async def tle():
    """Return cached TLE records from CelesTrak."""
    return get_tles()


@router.get("/jamming")
async def jamming():
    """Return measured GPS navigation-integrity degradation.

    Each zone is a 0.8-degree cell where the share of aircraft failing the
    gpsjam.org threshold (nic < 7 or nac_p < 8) reached the reporting minimum.
    `status` distinguishes a real measurement from an unevaluable feed:
    "ok", "no_integrity_data" (the source carried no nic/nac_p at all),
    "insufficient_coverage" (no cell held enough aircraft to form a ratio) or
    "feed_not_live" (the aircraft feed is not live or stale, so the last verdict
    is withheld; `feed_state` says why).
    """
    return get_jamming_zones()


@router.get("/connectivity")
async def connectivity():
    """Return measured internet disruption per watched country, from IODA.

    Four independent sensors (bgp, ping-slash24, merit-nt, gtr) are reported
    separately and never averaged: agreement between them is the confidence
    signal.  Each is judged against ITS OWN normal swing — robust_z =
    deviation / typical — because the four have wildly different natural
    variance and one shared percentage threshold cannot serve both a sensor
    that moves 19% on a quiet evening and one that has not moved all week.
    `typical` is measured on THE SAME COMPARISON the deviation makes (the
    short baseline's hourly same-clock-hour test, replayed across the day; the
    long baseline's daily medians), never on the raw trace, whose spread is
    the diurnal cycle.  `deviation` is still reported as the human-readable
    magnitude, but `robust_z` is what decides; `z_basis` says which rule was
    applied ("relative", or "flat-sensor-absolute" for a sensor with no swing
    to divide by, whose z is therefore null).

    TWO baselines are scored separately and never blended: `sensors` against
    the same clock hours yesterday (a sudden cut) and `sensors_long` against
    the median of days 2-7 (a step down inside the week, which a trailing 24h
    baseline cannot see because it normalises to the outage).  A STEADY
    decline is invisible to both — it inflates its own denominator — so the
    long sensors also carry `trend_per_day`, the week's slope, which is
    reported and never voted with.  Per-country `state` is "disruption_sudden"
    / "disruption_sustained" (a MAJORITY of the available sensors depressed on
    that baseline, and never fewer than two), "partial" (2 depressed on either
    baseline but not a majority), "nominal" (0 or 1, since one sensor alone is
    a measurement artifact) or "degraded" (fewer than 2 sensors available on
    the short baseline and nothing fired, so no claim is possible); `basis`
    names which baseline fired.

    `corroboration` carries Cloudflare Radar's independent view of the same
    country — a different organisation measuring by a different method, and
    reported beside the IODA count rather than averaged into it.  Its
    `available`/`stale`/`as_of` track the OUTAGES call on its own, because
    Radar's two calls fail independently and last-good annotations must never
    read as this poll's.  `status` distinguishes a real measurement from an
    unfetched one: "ok", "no_data" (nothing polled yet) or "unavailable"
    (IODA did not answer).
    """
    return get_connectivity()


@router.get("/vessels")
async def vessels():
    """Vessel positions, wrapped in the feed envelope.

    With no AISSTREAM_API_KEY the state is `unconfigured` and the count null:
    a missing key is not an empty sea. See app/feeds.py and app/services/maritime.py.
    """
    return get_vessels_envelope()


@router.get("/aircraft/tracks")
async def aircraft_tracks():
    """Return track history trails for all tracked aircraft."""
    return get_aircraft_tracks()


@router.get("/vessels/tracks")
async def vessel_tracks():
    """Return track history trails for all tracked vessels."""
    return get_vessel_tracks()
