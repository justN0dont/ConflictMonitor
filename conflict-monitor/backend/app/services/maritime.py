"""Maritime vessel tracking via AISStream.io WebSocket.

Connects to the AISStream WebSocket and maintains a cache of
vessel positions within the Middle East bounding box.
"""

import asyncio
import json
import logging
import time

import httpx

from app.config import settings
from app.services.track_history import record_vessel_position

logger = logging.getLogger(__name__)

# Bounding box covering Middle East + Eastern Mediterranean + Red Sea + Persian Gulf
BOUNDING_BOXES = [
    [[10, 25], [45, 65]],  # Main Middle East
    [[25, -5], [45, 25]],  # Eastern Mediterranean
]

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


async def start_maritime_poller():
    """Connect to AISStream WebSocket and stream vessel positions."""
    if not settings.aisstream_api_key:
        logger.info("AISStream: no API key set (AISSTREAM_API_KEY) — maritime tracking disabled")
        return

    logger.info("AISStream: connecting for maritime vessel tracking")

    while True:
        try:
            await _run_websocket()
        except Exception as e:
            logger.error("AISStream connection error: %s (type: %s)", e, type(e).__name__)
            import traceback
            logger.error("AISStream traceback: %s", traceback.format_exc())
        logger.info("AISStream: reconnecting in 10s...")
        await asyncio.sleep(10)


async def _run_websocket():
    """Run the WebSocket connection to AISStream."""
    try:
        import websockets
    except ImportError:
        logger.error("AISStream: 'websockets' package not installed")
        return

    subscribe_msg = json.dumps({
        "APIKey": settings.aisstream_api_key,
        "BoundingBoxes": BOUNDING_BOXES,
        "FilterMessageTypes": ["PositionReport", "ShipStaticData"],
    })

    logger.info("AISStream: connecting to WebSocket...")
    async with websockets.connect(
        "wss://stream.aisstream.io/v0/stream",
        open_timeout=30,
        ping_interval=20,
        ping_timeout=20,
    ) as ws:
        await ws.send(subscribe_msg)
        logger.info("AISStream: subscribed to %d bounding boxes", len(BOUNDING_BOXES))
        msg_count = 0

        async for raw in ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            msg_count += 1
            if msg_count <= 3:
                logger.info("AISStream raw msg #%d: %s", msg_count, str(raw)[:300])

            if "error" in msg:
                logger.error("AISStream error: %s", msg["error"])
                return

            msg_type = msg.get("MessageType", "")
            meta = msg.get("MetaData") or msg.get("Metadata") or msg.get("metadata", {})
            mmsi = str(meta.get("MMSI", ""))
            if not mmsi:
                continue

            now = time.time()

            if msg_type == "PositionReport":
                report = msg.get("Message", {}).get("PositionReport", {})
                lat = report.get("Latitude")
                lon = report.get("Longitude")
                if lat is None or lon is None or (lat == 0 and lon == 0):
                    continue

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
                })
                _cache["vessels"][mmsi] = vessel
                _cache["last_update"] = now
                record_vessel_position(mmsi, lon, lat)

            elif msg_type == "ShipStaticData":
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

            # Prune stale vessels (not seen in 10 minutes)
            if len(_cache["vessels"]) > 100 and now - _cache.get("last_prune", 0) > 60:
                _cache["vessels"] = {
                    k: v for k, v in _cache["vessels"].items()
                    if now - v.get("last_seen", 0) < 600
                }
                _cache["last_prune"] = now


def get_vessels() -> list[dict]:
    """Return list of tracked vessels with positions."""
    now = time.time()
    return [
        v for v in _cache["vessels"].values()
        if v.get("lat") is not None and now - v.get("last_seen", 0) < 600
    ]
