"""Where AISStream's positions actually come from, by the monitor's own regions.

Behind FINDINGS.md, Measured facts, "AIS by region". Two steps, so the raw
facts outlive any one way of summarising them:

    AISSTREAM_API_KEY=... python tools/ais_regions.py record 1800 OUTDIR
    python tools/ais_regions.py analyse OUTDIR

`record` holds ONE connection with the production subscription and writes
every PositionReport (receipt time, upstream time_utc, MMSI, lat, lon) to
OUTDIR/ais_positions.csv, and every connect, disconnect and error frame to
OUTDIR/ais_events.log, so a transport gap is never read as a silent sea.

`analyse` buckets positions with the backend's own `feeds.region_of`, so the
regions measured are the regions served. Every region is measured against the
others on the same socket: a zero beside a busy control is the region's, not
the connection's (GODS-EYE-VIEW.md, PHASE2-13).
"""
import ast
import asyncio
import collections
import csv
import json
import os
import sys
import time

BACKEND = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend")
sys.path.insert(0, BACKEND)
from app.feeds import REGIONS, region_of  # noqa: E402  (stdlib-only module)


def _subscription_boxes():
    """maritime.BOUNDING_BOXES, read from the source so no app config is needed."""
    src = open(os.path.join(BACKEND, "app", "services", "maritime.py"), encoding="utf-8").read()
    start = src.index("BOUNDING_BOXES = [")
    body = src[start:src.index("\n]", start) + 2].split("=", 1)[1]
    return ast.literal_eval(body.strip())


async def record(seconds: int, out: str) -> None:
    import websockets
    from datetime import datetime, timezone
    import re

    key = os.environ["AISSTREAM_API_KEY"]
    boxes = _subscription_boxes()

    def parse_time_utc(v):
        m = re.match(r"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})(\.\d+)?", v.strip()) if isinstance(v, str) else None
        if not m:
            return None
        frac = (m.group(3) or "")[:7]
        fmt = "%Y-%m-%d %H:%M:%S.%f" if frac else "%Y-%m-%d %H:%M:%S"
        return datetime.strptime(f"{m.group(1)} {m.group(2)}{frac}", fmt).replace(tzinfo=timezone.utc).timestamp()

    os.makedirs(out, exist_ok=True)
    pos_f = open(os.path.join(out, "ais_positions.csv"), "w", newline="", encoding="utf-8")
    ev_f = open(os.path.join(out, "ais_events.log"), "w", encoding="utf-8")
    pos = csv.writer(pos_f)
    pos.writerow(["recv", "time_utc", "mmsi", "lat", "lon"])

    def ev(*a):
        ev_f.write(f"{time.time():.3f} " + " ".join(str(x) for x in a) + "\n")
        ev_f.flush()

    counts = collections.Counter()
    end = time.time() + seconds
    ev("start", seconds)
    while time.time() < end:
        try:
            async with websockets.connect("wss://stream.aisstream.io/v0/stream",
                                          open_timeout=30, ping_interval=20, ping_timeout=20) as ws:
                await ws.send(json.dumps({"APIKey": key, "BoundingBoxes": boxes,
                                          "FilterMessageTypes": ["PositionReport", "ShipStaticData"]}))
                ev("connected")
                while time.time() < end:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=min(300, max(1, end - time.time())))
                    except asyncio.TimeoutError:
                        ev("recv_timeout")
                        break
                    m = json.loads(raw)
                    if "error" in m:
                        ev("error_frame", m["error"])
                        break
                    if m.get("MessageType") != "PositionReport":
                        counts[m.get("MessageType", "other")] += 1
                        continue
                    meta = m.get("MetaData") or {}
                    r = m.get("Message", {}).get("PositionReport", {})
                    lat, lon = r.get("Latitude"), r.get("Longitude")
                    if lat is None or lon is None or (lat == 0 and lon == 0):
                        counts["unusable"] += 1
                        continue
                    t = parse_time_utc(meta.get("time_utc"))
                    counts["unparsed_time"] += t is None
                    pos.writerow([f"{time.time():.3f}", "" if t is None else f"{t:.3f}", meta.get("MMSI"), lat, lon])
                    counts["positions"] += 1
        except Exception as e:
            ev("disconnect", type(e).__name__, str(e)[:200])
            await asyncio.sleep(5)
    pos_f.close()
    ev("end", dict(counts))
    ev_f.close()
    print(dict(counts))


def _pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))] if xs else None


def analyse(out: str) -> None:
    rows = list(csv.DictReader(open(os.path.join(out, "ais_positions.csv"), encoding="utf-8")))
    events = [line.split(" ", 2) for line in open(os.path.join(out, "ais_events.log"), encoding="utf-8").read().splitlines()]
    conns = [float(e[0]) for e in events if e[1] == "connected"]
    breaks = [e for e in events if e[1] in ("disconnect", "recv_timeout", "error_frame")]
    start = float(next(e[0] for e in events if e[1] == "start"))
    end = float(next((e[0] for e in events if e[1] == "end"), rows[-1]["recv"] if rows else start))
    print(f"window {end - start:.0f} s, connections {len(conns)}, breaks {len(breaks)}")
    if not conns:
        print("TOOL_ERROR: never connected")
        return

    def conn_of(t):
        return sum(1 for c in conns if c <= t)

    by = collections.defaultdict(list)
    lags = []
    for r in rows:
        recv = float(r["recv"])
        if r["time_utc"]:
            lags.append(recv - float(r["time_utc"]))
        by[region_of("vessels", float(r["lat"]), float(r["lon"]))].append((recv, r["mmsi"]))

    print(f"{len(rows)} positions, {len({r['mmsi'] for r in rows})} distinct MMSI")
    if lags:
        print(f"upstream lag: p50 {_pct(lags, .5):.1f} s, p99 {_pct(lags, .99):.1f} s, "
              f"max {max(lags):.1f} s, negative {sum(1 for x in lags if x < 0)}")
    print(f"{'region':12} {'role':8} {'pos':>6} {'share':>6} {'mmsi':>5} {'/min':>6} {'longest silence':>16}")
    busy_control = any(by.get(s.key) for s in REGIONS.values() if s.role == "control")
    for spec in list(REGIONS.values()) + [None]:
        key = spec.key if spec else None
        pts = sorted(by.get(key, []))
        share = 100 * len(pts) / len(rows) if rows else 0
        if pts:
            gaps = [b[0] - a[0] for a, b in zip(pts, pts[1:]) if conn_of(a[0]) == conn_of(b[0])]
            silence = max(gaps + [pts[0][0] - start, end - pts[-1][0]])
            verdict = ""
        else:
            silence = end - start
            verdict = "" if spec is None else "MEASURED_EMPTY" if busy_control else "CONTROL_SILENT - inconclusive"
        print(f"{key or '(none)':12} {spec.role if spec else '-':8} {len(pts):>6} {share:>5.1f}% "
              f"{len({p[1] for p in pts}):>5} {60 * len(pts) / (end - start):>6.1f} {silence:>14.1f} s  {verdict}")


if __name__ == "__main__":
    if sys.argv[1:2] == ["record"]:
        asyncio.run(record(int(sys.argv[2]), sys.argv[3]))
    elif sys.argv[1:2] == ["analyse"]:
        analyse(sys.argv[2])
    else:
        sys.exit(__doc__)
