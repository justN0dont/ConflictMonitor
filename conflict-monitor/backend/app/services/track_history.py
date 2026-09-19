"""In-memory track history for aircraft and vessels.

Stores position breadcrumbs in bounded deques keyed by entity ID.
"""

import time
from collections import deque

AIRCRAFT_MAX_POINTS = 120  # ~30 min at 15s intervals
VESSEL_MAX_POINTS = 200

_aircraft_tracks: dict[str, deque] = {}
_vessel_tracks: dict[str, deque] = {}
_last_prune = 0.0


def record_aircraft_position(icao24: str, lon: float, lat: float) -> None:
    ts = time.time()
    if icao24 not in _aircraft_tracks:
        _aircraft_tracks[icao24] = deque(maxlen=AIRCRAFT_MAX_POINTS)
    track = _aircraft_tracks[icao24]
    # Skip if position unchanged
    if track and track[-1][0] == lon and track[-1][1] == lat:
        return
    track.append((lon, lat, ts))
    _maybe_prune()


def record_vessel_position(mmsi: str, lon: float, lat: float) -> None:
    ts = time.time()
    if mmsi not in _vessel_tracks:
        _vessel_tracks[mmsi] = deque(maxlen=VESSEL_MAX_POINTS)
    track = _vessel_tracks[mmsi]
    if track and track[-1][0] == lon and track[-1][1] == lat:
        return
    track.append((lon, lat, ts))
    _maybe_prune()


def get_aircraft_tracks() -> dict[str, list]:
    return {k: list(v) for k, v in _aircraft_tracks.items() if len(v) >= 2}


def get_vessel_tracks() -> dict[str, list]:
    return {k: list(v) for k, v in _vessel_tracks.items() if len(v) >= 2}


def _maybe_prune() -> None:
    global _last_prune
    now = time.time()
    if now - _last_prune < 60:
        return
    _last_prune = now
    cutoff = now - 600  # 10 minutes
    for tracks in (_aircraft_tracks, _vessel_tracks):
        stale = [k for k, v in tracks.items() if v[-1][2] < cutoff]
        for k in stale:
            del tracks[k]
