import asyncio
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.security import origin_allowed
from app.services.broadcaster import broadcaster

logger = logging.getLogger(__name__)
router = APIRouter()


@router.websocket("/ws/events")
async def websocket_events(ws: WebSocket):
    origin = ws.headers.get("origin")
    if not origin_allowed(origin):
        # CORS does not cover websockets; this is the check that does (C60).
        logger.warning("WebSocket refused: origin %s is not in CORS_ORIGINS", origin)
        await ws.close(code=1008)
        return
    await broadcaster.connect(ws)
    try:
        while True:
            try:
                # Wait for client messages (pings) with a timeout
                # so we can also detect dead connections
                await asyncio.wait_for(ws.receive_text(), timeout=60)
            except asyncio.TimeoutError:
                # No message from client — send a ping to keep alive
                try:
                    await ws.send_json({"type": "ping"})
                except Exception:
                    break
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("WebSocket error")
    finally:
        await broadcaster.disconnect(ws)
