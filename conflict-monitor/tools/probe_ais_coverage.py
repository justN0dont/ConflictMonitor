import asyncio, json, time, collections
import websockets
import os
import sys

# One definition of the regions: the backend's, which production uses.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))
from app.feeds import region_of  # noqa: E402

KEY = os.environ["AISSTREAM_API_KEY"]
print("key present:", bool(KEY), flush=True)

def region(lat, lon):
    return region_of("vessels", lat, lon) or "OTHER"

async def probe(label, boxes, seconds):
    seen = collections.Counter()
    mmsis = set()
    n = 0
    try:
        async with websockets.connect("wss://stream.aisstream.io/v0/stream",
                                      open_timeout=30, ping_interval=20, ping_timeout=20) as ws:
            await ws.send(json.dumps({"APIKey": KEY, "BoundingBoxes": boxes,
                                      "FilterMessageTypes": ["PositionReport"]}))
            end = time.time() + seconds
            while time.time() < end:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=max(1, end - time.time()))
                except asyncio.TimeoutError:
                    break
                m = json.loads(raw)
                if "error" in m:
                    print(label, "ERROR", m["error"], flush=True); break
                meta = m.get("MetaData") or {}
                r = m.get("Message", {}).get("PositionReport", {})
                lat, lon = r.get("Latitude"), r.get("Longitude")
                if lat is None: continue
                n += 1
                mmsis.add(str(meta.get("MMSI")))
                seen[region(lat, lon)] += 1
    except Exception as e:
        print(label, "EXC", type(e).__name__, e, flush=True)
    print(f"{label}: msgs={n} distinct_mmsi={len(mmsis)} regions={dict(seen)}", flush=True)

async def main():
    await probe("A_code_boxes_as_written", [[[10,25],[45,65]], [[25,-5],[45,25]]], 75)
    await asyncio.sleep(5)
    await probe("B_gulf_only_SW_NE", [[[23,47],[31,57]]], 60)
    await asyncio.sleep(5)
    await probe("C_gulf_only_NW_SE", [[[31,47],[23,57]]], 60)
    await asyncio.sleep(5)
    await probe("D_emed_only_SW_NE", [[[30,25],[38,36]]], 60)

asyncio.run(main())
