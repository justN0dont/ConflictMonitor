from fastapi import APIRouter

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
    """Return detected GPS jamming zones inferred from MLAT clustering."""
    return get_jamming_zones()


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
