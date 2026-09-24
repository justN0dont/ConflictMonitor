"""Maritime vessel tracking via AISStream.io WebSocket.

Connects to the AISStream WebSocket and maintains a cache of
vessel positions within the Middle East bounding box.

Feed health (docs/FINDINGS.md, Roadmap > Phase 2 > feed_health decisions):

- A missing key is recorded as `unconfigured`, never as an empty sea.
- Liveness follows ACCEPTED POSITION REPORTS, not the socket. The tracker is
  told of a success only when a PositionReport with a usable position is
  cached, and its upstream clock is that message's own `time_utc`.
- A socket that is open but silent is recycled: every read has a deadline
  (RECV_DEADLINE_S). `async for` over the socket blocked indefinitely on a
  peer that answered pings and sent nothing (docs/GODS-EYE-VIEW.md, PHASE2-8).
- Reconnects back off on a ladder instead of a fixed 10 s forever; a refused
  key waits an hour. The ladder resets only when data arrives. This closes the
  untriaged "maritime reconnect" item (FINDINGS.md, Collection).
"""

import asyncio
import json
import logging
import re
import time
from datetime import datetime, timezone

from app import feeds
from app.config import settings
from app.feeds import ErrorKind
from app.services.track_history import record_vessel_position

logger = logging.getLogger(__name__)

# Bounding box covering Middle East + Eastern Mediterranean + Red Sea + Persian Gulf
BOUNDING_BOXES = [
    [[10, 25], [45, 65]],  # Main Middle East
    [[25, -5], [45, 25]],  # Eastern Mediterranean
]

AISSTREAM_URL = "wss://stream.aisstream.io/v0/stream"

# No recognised message for this long and the socket is recycled. Unmeasured:
# re-set from the first week of feed_health rows.
RECV_DEADLINE_S = 300
# Reconnect delays after consecutive transport failures, then DOWN_RETRY_S.
BACKOFF_LADDER_S = (5, 15, 60, 300)
DOWN_RETRY_S = 900
# A refused key does not fix itself; probe hourly rather than every 10 s.
AUTH_RETRY_S = 3600
# Vessels not seen for this long are dropped from what is served.
VESSEL_MAX_AGE_S = 600

# AISStream's error text for a bad or missing key; anything else is transport.
_AUTH_ERROR = re.compile(r"api\s*key\s*(is\s*)?(invalid|required|missing|not\s*valid)|unauthori[sz]ed",
                         re.IGNORECASE)

_cache: dict = {"vessels": {}, "last_update": 0}

# AIS ship type codes to human-readable categories
SHIP_TYPES = {
    range(20, 30): "Wing in Ground",
    range(30, 36): "Fishing",
    range(36, 40): "Towing/Dredging",
    range(40, 50): "High Speed Craft",
    range(50, 55): "Special Craft",
    range(60, 70): "Passenger",
    range(70, 80): "Cargo",
    range(80, 90): "Tanker",
    range(90, 100): "Other",
}


def _ship_type_name(code: int) -> str:
    for rng, name in SHIP_TYPES.items():
        if code in rng:
            return name
    return "Unknown"


def _parse_time_utc(value) -> float | None:
    """AISStream MetaData `time_utc`, e.g. "2024-05-01 12:34:56.789012 +0000 UTC",
    as epoch seconds, or None. Parsed tolerantly; never replaced by receipt time."""
    if not isinstance(value, str):
        return None
    m = re.match(r"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})(\.\d+)?", value.strip())
    if not m:
        return None
    frac = (m.group(3) or "")[:7]  # at most microseconds
    try:
        dt = datetime.strptime(f"{m.group(1)} {m.group(2)}{frac}",
                               "%Y-%m-%d %H:%M:%S.%f" if frac else "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc).timestamp()


def classify_error_frame(text: str) -> ErrorKind:
    return ErrorKind.AUTH_FAILED if _AUTH_ERROR.search(text or "") else ErrorKind.FETCH_ERROR


def handle_message(msg: dict, now: float) -> str:
    """Apply one decoded AISStream message to the cache.

    Returns what it was: "accepted" (a usable position, cached), "static",
    "ignored" (recognised but unusable) or "error:<text>". Pure apart from the
    cache and the track history; the caller decides what each outcome means
    for the feed.
    """
    if "error" in msg:
        return f"error:{msg['error']}"

    msg_type = msg.get("MessageType", "")
    meta = msg.get("MetaData") or msg.get("Metadata") or msg.get("metadata", {})
    mmsi = str(meta.get("MMSI", ""))
    if not mmsi:
        return "ignored"

    if msg_type == "PositionReport":
        report = msg.get("Message", {}).get("PositionReport", {})
        lat = report.get("Latitude")
        lon = report.get("Longitude")
        if lat is None or lon is None or (lat == 0 and lon == 0):
            return "ignored"

        vessel = _cache["vessels"].get(mmsi, {})
        vessel.update({
            "mmsi": mmsi,
            "name": (meta.get("ShipName") or vessel.get("name") or "").strip(),
            "lat": lat,
            "lon": lon,
            "speed": report.get("Sog", 0),
            "heading": report.get("TrueHeading", report.get("Cog", 0)),
            "course": report.get("Cog", 0),
            "nav_status": report.get("NavigationalStatus", 15),
            "last_seen": now,
            # The upstream's own time for this fix, or None. Kept beside
            # last_seen (receipt) until the parse is checked on real frames.
            "observed_at": _parse_time_utc(meta.get("time_utc")),
        })
        _cache["vessels"][mmsi] = vessel
        _cache["last_update"] = now
        record_vessel_position(mmsi, lon, lat)
        return "accepted"

    if msg_type == "ShipStaticData":
        static = msg.get("Message", {}).get("ShipStaticData", {})
        vessel = _cache["vessels"].get(mmsi, {})
        vessel.update({
            "mmsi": mmsi,
            "name": (meta.get("ShipName") or "").strip(),
            "ship_type": static.get("Type", 0),
            "ship_type_name": _ship_type_name(static.get("Type", 0)),
            "destination": (static.get("Destination") or "").strip(),
            "length": static.get("Dimension", {}).get("A", 0) + static.get("Dimension", {}).get("B", 0),
        })
        _cache["vessels"][mmsi] = vessel
        return "static"

    return "ignored"


def _prune(now: float) -> None:
    """Drop vessels not seen for VESSEL_MAX_AGE_S. Runs only while messages flow,
    so an outage does not empty the cache; freshness is the tracker's job."""
    if len(_cache["vessels"]) > 100 and now - _cache.get("last_prune", 0) > 60:
        _cache["vessels"] = {
            k: v for k, v in _cache["vessels"].items()
            if now - v.get("last_seen", 0) < VESSEL_MAX_AGE_S
        }
        _cache["last_prune"] = now


def next_delay(kind: ErrorKind, consecutive_failures: int) -> int:
    """Seconds to wait before reconnecting after a failure."""
    if kind is ErrorKind.AUTH_FAILED:
        return AUTH_RETRY_S
    i = consecutive_failures - 1
    return BACKOFF_LADDER_S[i] if 0 <= i < len(BACKOFF_LADDER_S) else DOWN_RETRY_S


async def _run_websocket(connect, tracker: feeds.FeedTracker, clock=time.time) -> tuple[ErrorKind, str]:
    """One connection, read until it fails. Returns why it ended."""
    subscribe_msg = json.dumps({
        "APIKey": settings.aisstream_api_key,
        "BoundingBoxes": BOUNDING_BOXES,
        "FilterMessageTypes": ["PositionReport", "ShipStaticData"],
    })

    async with connect(AISSTREAM_URL, open_timeout=30, ping_interval=20, ping_timeout=20) as ws:
        await ws.send(subscribe_msg)
        logger.info("AISStream: subscribed to %d bounding boxes", len(BOUNDING_BOXES))
        while True:
            try:
                raw = await asyncio.wait_for(ws.recv(), RECV_DEADLINE_S)
            except asyncio.TimeoutError:
                # Open but silent: ping keeps a dead-quiet socket alive forever.
                return ErrorKind.TIMEOUT, f"no message for {RECV_DEADLINE_S} s; socket recycled"
            try:
                msg = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(msg, dict):
                continue

            now = clock()
            outcome = handle_message(msg, now)
            if outcome.startswith("error:"):
                text = outcome[len("error:"):]
                return classify_error_frame(text), f"AISStream error frame: {text}"
            if outcome == "accepted":
                vessels = get_vessels()
                newest = max((v.get("observed_at") or 0 for v in vessels), default=0) or None
                tracker.succeeded(now, count=len(vessels), source="aisstream", source_epoch=newest)
            _prune(now)


async def start_maritime_poller(connect=None, sleep=asyncio.sleep, clock=time.time):
    """Connect to AISStream and stream vessel positions, reporting to feed_health."""
    tracker = feeds.tracker("vessels")
    if not settings.aisstream_api_key:
        tracker.configured = False
        logger.info("AISStream: no API key set (AISSTREAM_API_KEY) — maritime tracking not configured")
        return

    if connect is None:
        import websockets  # pinned in requirements; an ImportError is a deploy bug, not a state
        connect = websockets.connect

    logger.info("AISStream: connecting for maritime vessel tracking")
    last_state = None
    while True:
        try:
            kind, detail = await _run_websocket(connect, tracker, clock)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            kind = feeds.ErrorKind.TIMEOUT if isinstance(e, (asyncio.TimeoutError, TimeoutError)) \
                else feeds.ErrorKind.FETCH_ERROR
            detail = f"{type(e).__name__}: {e}"
        tracker.failed(clock(), kind, detail)
        delay = next_delay(kind, tracker.consecutive_failures)
        state = tracker.state(clock())
        # Log transitions, not every retry.
        if state != last_state or kind is ErrorKind.AUTH_FAILED:
            logger.warning("AISStream: %s (%s) — %s; reconnecting in %ds", state.value, kind.value, detail, delay)
            last_state = state
        await sleep(delay)


def get_vessels() -> list[dict]:
    """Vessels seen within VESSEL_MAX_AGE_S of the last message.

    Age is measured against the feed's last success, not the wall clock, when
    the feed is not live: an outage must not empty the list into a clean []
    that reads as an empty sea. The envelope says what the rows are worth.
    """
    tracker = feeds.tracker("vessels")
    now = time.time()
    ref = now
    if tracker.state(now) is not feeds.FeedState.LIVE and tracker.last_success_at is not None:
        ref = tracker.last_success_at
    return [
        v for v in _cache["vessels"].values()
        if v.get("lat") is not None and ref - v.get("last_seen", 0) < VESSEL_MAX_AGE_S
    ]


def get_vessels_envelope() -> dict:
    """/tracking/vessels: the vessel list with the feed's state beside it."""
    return feeds.envelope("vessels", get_vessels(), time.time())
