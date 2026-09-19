from fastapi import APIRouter

from app.services.connectivity import get_connectivity
from app.services.maritime import get_vessels
from app.services.opensky import get_aircraft, get_jamming_zones
from app.services.satellites import get_tles
from app.services.track_history import get_aircraft_tracks, get_vessel_tracks

router = APIRouter(prefix="/tracking", tags=["tracking"])


@router.get("/aircraft")
async def aircraft():
    """Return cached aircraft positions from OpenSky."""
    return get_aircraft()


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
    "ok", "no_integrity_data" (the source carried no nic/nac_p at all) or
    "insufficient_coverage" (no cell held enough aircraft to form a ratio).
    """
    return get_jamming_zones()


@router.get("/connectivity")
async def connectivity():
    """Return measured internet disruption per watched country, from IODA.

    Four independent sensors (bgp, ping-slash24, merit-nt, gtr) are reported
    separately and never averaged: agreement between them is the confidence
    signal.  Per-country `state` is "disruption" (3+ sensors depressed against
    their own 24h baselines), "partial" (2 — corroborated only in part),
    "nominal" (0 or 1, since one sensor alone is a measurement artifact) or
    "degraded" (fewer than 2 sensors available, so no claim is possible).
    `status` distinguishes a real measurement from an unfetched one: "ok",
    "no_data" (nothing polled yet) or "unavailable" (IODA did not answer).
    """
    return get_connectivity()


@router.get("/vessels")
async def vessels():
    """Return cached maritime vessel positions from AISStream."""
    return get_vessels()


@router.get("/aircraft/tracks")
async def aircraft_tracks():
    """Return track history trails for all tracked aircraft."""
    return get_aircraft_tracks()


@router.get("/vessels/tracks")
async def vessel_tracks():
    """Return track history trails for all tracked vessels."""
    return get_vessel_tracks()
