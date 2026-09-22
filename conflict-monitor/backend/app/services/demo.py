"""Demo mode — synthetic data generation for zero-config demos.

When DEMO_MODE=true, this module replaces the Telegram listener, ADS-B poller,
and maritime poller with realistic synthetic data generators. CelesTrak TLE
fetching remains real (it's free, no auth).

Usage:
    DEMO_MODE=true docker-compose up
"""

import asyncio
import datetime
import json
import logging
import math
import random
import time

from geoalchemy2.shape import from_shape
from shapely.geometry import Point

from app.db import async_session
from app.models import Event
from app.schemas import EventRead, EventWS
from app.services.broadcaster import broadcaster
from app.services.classifier import evidence_span

logger = logging.getLogger(__name__)

# =========================================================================
# LOCATION DATABASE — real coordinates
# =========================================================================

LOCATIONS = [
    # Iran
    {"name": "Tehran", "lat": 35.6892, "lon": 51.3890, "country": "Iran"},
    {"name": "Isfahan", "lat": 32.6546, "lon": 51.6680, "country": "Iran"},
    {"name": "Bandar Abbas", "lat": 27.1865, "lon": 56.2808, "country": "Iran"},
    {"name": "Bushehr", "lat": 28.9684, "lon": 50.8385, "country": "Iran"},
    {"name": "Natanz", "lat": 33.5130, "lon": 51.7270, "country": "Iran"},
    {"name": "Fordow", "lat": 34.8830, "lon": 51.2430, "country": "Iran"},
    {"name": "Kharg Island", "lat": 29.2333, "lon": 50.3167, "country": "Iran"},
    {"name": "Chabahar", "lat": 25.2919, "lon": 60.6430, "country": "Iran"},
    {"name": "Tabriz", "lat": 38.0962, "lon": 46.2738, "country": "Iran"},
    {"name": "Shiraz", "lat": 29.5918, "lon": 52.5836, "country": "Iran"},
    {"name": "Qom", "lat": 34.6401, "lon": 50.8764, "country": "Iran"},
    {"name": "Parchin", "lat": 35.5167, "lon": 51.7667, "country": "Iran"},
    {"name": "Arak", "lat": 34.0954, "lon": 49.7013, "country": "Iran"},
    {"name": "Abadan", "lat": 30.3392, "lon": 48.2973, "country": "Iran"},
    {"name": "Dezful", "lat": 32.3811, "lon": 48.4018, "country": "Iran"},
    {"name": "Kermanshah", "lat": 34.3142, "lon": 47.0650, "country": "Iran"},
    {"name": "Mashhad", "lat": 36.2605, "lon": 59.6168, "country": "Iran"},
    # Iraq
    {"name": "Baghdad", "lat": 33.3128, "lon": 44.3615, "country": "Iraq"},
    {"name": "Basra", "lat": 30.5085, "lon": 47.7804, "country": "Iraq"},
    {"name": "Erbil", "lat": 36.1901, "lon": 44.0091, "country": "Iraq"},
    {"name": "Kirkuk", "lat": 35.4681, "lon": 44.3953, "country": "Iraq"},
    {"name": "Mosul", "lat": 36.3409, "lon": 43.1300, "country": "Iraq"},
    {"name": "Al-Asad Airbase", "lat": 33.7856, "lon": 42.4411, "country": "Iraq"},
    {"name": "Taji", "lat": 33.5272, "lon": 44.2617, "country": "Iraq"},
    # Syria
    {"name": "Damascus", "lat": 33.5138, "lon": 36.2765, "country": "Syria"},
    {"name": "Aleppo", "lat": 36.2021, "lon": 37.1343, "country": "Syria"},
    {"name": "Latakia", "lat": 35.5317, "lon": 35.7918, "country": "Syria"},
    {"name": "Tartus", "lat": 34.8890, "lon": 35.8866, "country": "Syria"},
    {"name": "Deir ez-Zor", "lat": 35.3359, "lon": 40.1408, "country": "Syria"},
    {"name": "Abu Kamal", "lat": 34.4509, "lon": 40.9188, "country": "Syria"},
    {"name": "Homs", "lat": 34.7272, "lon": 36.7200, "country": "Syria"},
    {"name": "T4 Airbase", "lat": 34.5222, "lon": 37.6275, "country": "Syria"},
    # Israel
    {"name": "Tel Aviv", "lat": 32.0853, "lon": 34.7818, "country": "Israel"},
    {"name": "Haifa", "lat": 32.7940, "lon": 34.9896, "country": "Israel"},
    {"name": "Dimona", "lat": 31.0666, "lon": 35.2083, "country": "Israel"},
    {"name": "Nevatim AFB", "lat": 31.2083, "lon": 34.9390, "country": "Israel"},
    {"name": "Ramon AFB", "lat": 30.7761, "lon": 34.6668, "country": "Israel"},
    {"name": "Eilat", "lat": 29.5577, "lon": 34.9519, "country": "Israel"},
    {"name": "Jerusalem", "lat": 31.7683, "lon": 35.2137, "country": "Israel"},
    # Lebanon
    {"name": "Beirut", "lat": 33.8938, "lon": 35.5018, "country": "Lebanon"},
    {"name": "Dahieh", "lat": 33.8547, "lon": 35.5233, "country": "Lebanon"},
    {"name": "Baalbek", "lat": 34.0047, "lon": 36.2110, "country": "Lebanon"},
    {"name": "Tyre", "lat": 33.2705, "lon": 35.2038, "country": "Lebanon"},
    {"name": "Nabatieh", "lat": 33.3778, "lon": 35.4834, "country": "Lebanon"},
    # Yemen
    {"name": "Sanaa", "lat": 15.3694, "lon": 44.1910, "country": "Yemen"},
    {"name": "Hodeidah", "lat": 14.7979, "lon": 42.9545, "country": "Yemen"},
    {"name": "Aden", "lat": 12.7855, "lon": 45.0187, "country": "Yemen"},
    {"name": "Marib", "lat": 15.4543, "lon": 45.3220, "country": "Yemen"},
    {"name": "Saada", "lat": 16.9400, "lon": 43.7600, "country": "Yemen"},
    # Persian Gulf / Red Sea
    {"name": "Strait of Hormuz", "lat": 26.5667, "lon": 56.2500, "country": ""},
    {"name": "Bab el-Mandeb", "lat": 12.5833, "lon": 43.3333, "country": ""},
    {"name": "Qeshm Island", "lat": 26.8500, "lon": 55.9000, "country": "Iran"},
    {"name": "Jask", "lat": 25.6395, "lon": 57.7703, "country": "Iran"},
]

# =========================================================================
# SUMMARY TEMPLATES
# =========================================================================

MILITARY_SUMMARIES = [
    "IRGC launches ballistic missiles toward {loc}",
    "IDF confirms airstrikes on weapons depot near {loc}",
    "Houthi anti-ship missile fired toward commercial vessel near {loc}",
    "Explosion reported at military installation near {loc}",
    "SAM battery activation detected near {loc}",
    "CENTCOM confirms strike on Iran-backed militia position near {loc}",
    "Drone swarm detected approaching {loc}",
    "Artillery exchanges reported along border near {loc}",
    "Naval assets repositioning near {loc}",
    "Iron Dome intercepts launched over {loc}",
    "Cruise missile debris recovered near {loc}",
    "Military convoy movement spotted heading toward {loc}",
    "Air raid sirens activated across {loc}",
    "Retaliatory strikes reported targeting installations near {loc}",
    "UAV shot down over {loc} airspace",
    "Special forces operation reported near {loc}",
    "Ballistic missile launch detected from {loc} area",
    "Multiple explosions heard in {loc}",
    "F-35 sorties observed departing toward {loc}",
    "Ground incursion reported near {loc} border zone",
    "Anti-tank missile strikes reported near {loc}",
    "Military helicopter crash reported near {loc}",
    "Tunnel network discovered near {loc}",
    "EW jamming activity detected near {loc}",
    "Rocket barrage impacts reported in {loc}",
    "IRGC naval exercises commence near {loc}",
    "Carrier strike group repositioning toward {loc}",
    "B-52 bomber spotted on flight path near {loc}",
    "Ammunition depot explosion reported at {loc}",
    "Counter-battery fire exchanged near {loc}",
]

DIPLOMATIC_SUMMARIES = [
    "Emergency UNSC session called regarding {loc} escalation",
    "Iran foreign minister issues statement on {loc} situation",
    "{country} recalls ambassador following {loc} incident",
    "Ceasefire negotiations underway for {loc} region",
    "UN envoy arrives in {loc} for emergency mediation talks",
    "Joint statement from Gulf states condemning strikes on {loc}",
    "G7 emergency call scheduled regarding {loc} crisis",
    "Red Cross requests humanitarian corridor access to {loc}",
    "Russia calls for restraint following {loc} strikes",
    "China offers to mediate {loc} conflict",
]

ECONOMIC_SUMMARIES = [
    "Oil prices surge 8% following attack near {loc}",
    "Shipping insurance rates spike for {loc} transit routes",
    "Commercial vessels rerouting away from {loc}",
    "Port operations suspended at {loc}",
    "Energy markets react to {loc} escalation",
    "Iran threatens to close {loc} to commercial traffic",
    "LNG shipments delayed due to {loc} security concerns",
    "Brent crude hits $120/barrel on {loc} fears",
    "Major shipping line suspends {loc} routes indefinitely",
    "Currency markets volatile on {loc} escalation",
]

CYBER_SUMMARIES = [
    "Major DDoS attack reported against {country} government infrastructure",
    "Cyber operation disrupts power grid in {loc} area",
    "GPS spoofing detected affecting navigation near {loc}",
    "Communication networks jammed across {loc} region",
    "State-linked hackers target financial systems in {loc}",
    "SCADA systems compromised at industrial facility near {loc}",
    "Wiper malware deployed against targets in {loc}",
    "Critical infrastructure alert issued for {loc}",
]

CHANNELS = [
    "Aurora Intel", "OSINTdefender", "MidEast Spectator",
    "Sentdefender", "Intel Slava Z", "Israel Radar", "CIG",
    "MilitaryOSINT", "IranIntl", "QudsAlert", "WarMonitor",
]

# =========================================================================
# AIRCRAFT DEFINITIONS
# =========================================================================

MILITARY_AIRCRAFT = [
    {"callsign": "FORTE11", "type": "RQ-4 Global Hawk", "alt_range": (50000, 60000), "speed_kts": 340},
    {"callsign": "FORTE12", "type": "RQ-4 Global Hawk", "alt_range": (50000, 60000), "speed_kts": 340},
    {"callsign": "JAKE21", "type": "RC-135V Rivet Joint", "alt_range": (30000, 38000), "speed_kts": 460},
    {"callsign": "JAKE11", "type": "RC-135W Rivet Joint", "alt_range": (30000, 38000), "speed_kts": 460},
    {"callsign": "RCH801", "type": "C-17 Globemaster", "alt_range": (28000, 36000), "speed_kts": 450},
    {"callsign": "RCH445", "type": "C-17 Globemaster", "alt_range": (28000, 36000), "speed_kts": 450},
    {"callsign": "EVAC01", "type": "C-130J Hercules", "alt_range": (20000, 28000), "speed_kts": 320},
    {"callsign": "DUKE21", "type": "E-3 Sentry AWACS", "alt_range": (29000, 34000), "speed_kts": 380},
    {"callsign": "TOPCAT1", "type": "P-8A Poseidon", "alt_range": (25000, 35000), "speed_kts": 410},
    {"callsign": "NCHO44", "type": "EP-3E Aries II", "alt_range": (24000, 30000), "speed_kts": 350},
]

CIVILIAN_AIRCRAFT = [
    {"callsign": "UAE205", "speed_kts": 480}, {"callsign": "QTR8", "speed_kts": 490},
    {"callsign": "SV123", "speed_kts": 470}, {"callsign": "THY42", "speed_kts": 480},
    {"callsign": "ELY315", "speed_kts": 470}, {"callsign": "MEA404", "speed_kts": 460},
    {"callsign": "ETH701", "speed_kts": 490}, {"callsign": "RJ185", "speed_kts": 460},
    {"callsign": "GFA210", "speed_kts": 470}, {"callsign": "KAC301", "speed_kts": 480},
    {"callsign": "OMA654", "speed_kts": 470}, {"callsign": "IAW112", "speed_kts": 460},
]

# =========================================================================
# VESSEL DEFINITIONS
# =========================================================================

VESSEL_TEMPLATES = [
    # Tankers
    {"name": "FRONT ALTAIR", "type": "Tanker", "speed_range": (10, 14), "mmsi_prefix": "3"},
    {"name": "STENA IMPERO", "type": "Tanker", "speed_range": (10, 14), "mmsi_prefix": "3"},
    {"name": "SABITI", "type": "Tanker", "speed_range": (10, 14), "mmsi_prefix": "4"},
    {"name": "PACIFIC VOYAGER", "type": "Tanker", "speed_range": (11, 15), "mmsi_prefix": "3"},
    {"name": "CASPIAN STAR", "type": "Tanker", "speed_range": (10, 14), "mmsi_prefix": "2"},
    {"name": "GULF PEARL", "type": "Tanker", "speed_range": (10, 13), "mmsi_prefix": "3"},
    {"name": "HORMUZ SPIRIT", "type": "Tanker", "speed_range": (10, 14), "mmsi_prefix": "4"},
    {"name": "RED SEA FALCON", "type": "Tanker", "speed_range": (11, 14), "mmsi_prefix": "3"},
    {"name": "ARABIAN MOON", "type": "Tanker", "speed_range": (10, 13), "mmsi_prefix": "2"},
    {"name": "PERSIAN WAVE", "type": "Tanker", "speed_range": (11, 14), "mmsi_prefix": "4"},
    {"name": "ATLAS FORTUNE", "type": "Tanker", "speed_range": (10, 14), "mmsi_prefix": "3"},
    {"name": "LIBRA TIDE", "type": "Tanker", "speed_range": (11, 14), "mmsi_prefix": "3"},
    # Cargo
    {"name": "COSCO HARMONY", "type": "Cargo", "speed_range": (12, 16), "mmsi_prefix": "4"},
    {"name": "MSC ARINA", "type": "Cargo", "speed_range": (13, 17), "mmsi_prefix": "3"},
    {"name": "MAERSK SENTOSA", "type": "Cargo", "speed_range": (14, 18), "mmsi_prefix": "2"},
    {"name": "CMA CGM LOIRE", "type": "Cargo", "speed_range": (13, 17), "mmsi_prefix": "2"},
    {"name": "EVERGREEN ACE", "type": "Cargo", "speed_range": (14, 18), "mmsi_prefix": "4"},
    {"name": "OOCL ATLAS", "type": "Cargo", "speed_range": (13, 17), "mmsi_prefix": "3"},
    {"name": "HYUNDAI TRUST", "type": "Cargo", "speed_range": (13, 16), "mmsi_prefix": "4"},
    # Military / Special
    {"name": "USNS PATUXENT", "type": "Military", "speed_range": (14, 20), "mmsi_prefix": "3"},
    {"name": "USS BATAAN", "type": "Military", "speed_range": (16, 24), "mmsi_prefix": "3"},
    {"name": "FS ALSACE", "type": "Military", "speed_range": (15, 22), "mmsi_prefix": "2"},
    {"name": "HMS DIAMOND", "type": "Military", "speed_range": (16, 25), "mmsi_prefix": "2"},
    {"name": "IRISL GOLESTAN", "type": "Cargo", "speed_range": (10, 14), "mmsi_prefix": "4"},
]

# Shipping lanes as waypoint sequences
SHIPPING_ROUTES = [
    # Strait of Hormuz transit (Persian Gulf → Gulf of Oman)
    [(27.0, 51.0), (26.5, 53.0), (26.2, 56.0), (25.5, 57.5), (24.0, 59.0)],
    # Red Sea northbound (Bab el-Mandeb → Suez approach)
    [(12.6, 43.3), (14.0, 42.5), (16.0, 41.5), (19.0, 39.5), (22.0, 37.5), (25.0, 35.5)],
    # Persian Gulf patrol
    [(26.0, 50.0), (27.0, 52.0), (26.5, 54.0), (26.0, 52.0)],
    # Eastern Med
    [(33.0, 34.0), (34.0, 35.0), (35.0, 34.5), (34.0, 33.5)],
    # Gulf of Aden
    [(12.0, 45.0), (12.5, 47.0), (13.0, 49.0), (13.5, 51.0)],
]


# =========================================================================
# HELPERS
# =========================================================================

def _pick(lst):
    return random.choice(lst)


def _severity():
    """Weighted severity: mostly 3-6, occasionally high."""
    r = random.random()
    if r < 0.35:
        return random.randint(3, 4)
    elif r < 0.65:
        return random.randint(5, 6)
    elif r < 0.88:
        return random.randint(7, 8)
    else:
        return random.randint(9, 10)


def _jitter(val, amount=0.3):
    return val + random.uniform(-amount, amount)


def _gen_event(ts: datetime.datetime | None = None) -> dict:
    """Generate a single synthetic event dict."""
    roll = random.random()
    if roll < 0.60:
        etype, templates = "military", MILITARY_SUMMARIES
    elif roll < 0.80:
        etype, templates = "diplomatic", DIPLOMATIC_SUMMARIES
    elif roll < 0.92:
        etype, templates = "economic", ECONOMIC_SUMMARIES
    else:
        etype, templates = "cyber", CYBER_SUMMARIES

    loc = _pick(LOCATIONS)
    template = _pick(templates)
    summary = template.replace("{loc}", loc["name"]).replace("{country}", loc.get("country") or "the region")

    return {
        "source": "demo",
        "channel_name": _pick(CHANNELS),
        "raw_text": summary,
        "summary": summary,
        "event_type": etype,
        "severity": _severity(),
        "lat": _jitter(loc["lat"]),
        "lon": _jitter(loc["lon"]),
        "timestamp": ts or datetime.datetime.now(datetime.timezone.utc),
    }


# =========================================================================
# SEED HISTORICAL EVENTS
# =========================================================================

async def seed_demo_history(count: int = 300):
    """Pre-populate the database with historical events spanning 7 days."""
    logger.info("Seeding %d historical demo events...", count)
    now = datetime.datetime.now(datetime.timezone.utc)
    created = 0

    async with async_session() as session:
        for i in range(count):
            # Exponential distribution: more events in recent days
            hours_ago = random.expovariate(0.02)  # mean ~50 hours
            hours_ago = min(hours_ago, 7 * 24)  # cap at 7 days
            ts = now - datetime.timedelta(hours=hours_ago)

            evt_data = _gen_event(ts)
            geometry = None
            if evt_data["lat"] is not None and evt_data["lon"] is not None:
                geometry = from_shape(
                    Point(evt_data["lon"], evt_data["lat"]), srid=4326
                )

            db_event = Event(
                source=evt_data["source"],
                channel_name=evt_data["channel_name"],
                raw_text=evt_data["raw_text"],
                summary=evt_data["summary"],
                event_type=evt_data["event_type"],
                severity=evt_data["severity"],
                lat=evt_data["lat"],
                lon=evt_data["lon"],
                geometry=geometry,
                timestamp=evt_data["timestamp"],
                # A demo row states no location_name — the place reaches the
                # database as a coordinate and as words inside raw_text, never
                # as a claim — so the one rule answers '' for all of them, and
                # '' is the truth about this row rather than a placeholder.
                # Written here because NULL is not: NULL means nothing looked,
                # and in demo mode it used to mean only "this row came from
                # demo.py", which is what an unwired writer costs a detector.
                evidence_span=evidence_span(evt_data["raw_text"], ""),
            )
            session.add(db_event)
            created += 1

        await session.commit()
    logger.info("Seeded %d historical events", created)


# =========================================================================
# LIVE EVENT GENERATOR
# =========================================================================

async def start_demo_event_generator():
    """Continuously generate synthetic conflict events."""
    logger.info("Demo event generator started")
    burst_counter = 0

    while True:
        try:
            # Normal event
            evt_data = _gen_event()
            geometry = None
            if evt_data["lat"] is not None and evt_data["lon"] is not None:
                geometry = from_shape(
                    Point(evt_data["lon"], evt_data["lat"]), srid=4326
                )

            async with async_session() as session:
                db_event = Event(
                    source=evt_data["source"],
                    channel_name=evt_data["channel_name"],
                    raw_text=evt_data["raw_text"],
                    summary=evt_data["summary"],
                    event_type=evt_data["event_type"],
                    severity=evt_data["severity"],
                    lat=evt_data["lat"],
                    lon=evt_data["lon"],
                    geometry=geometry,
                    timestamp=evt_data["timestamp"],
                    evidence_span=evidence_span(evt_data["raw_text"], ""),
                )
                session.add(db_event)
                await session.commit()
                await session.refresh(db_event)

                ws_payload = EventWS(
                    type="new_event",
                    event=EventRead.model_validate(db_event),
                )
                await broadcaster.broadcast(ws_payload.model_dump_json())
                logger.info(
                    "Demo event #%d: [%s] sev=%d %s",
                    db_event.id, db_event.event_type, db_event.severity,
                    db_event.summary[:60],
                )

            # Occasionally trigger burst events (escalation simulation)
            burst_counter += 1
            if burst_counter % 8 == 0 and random.random() < 0.3:
                burst_count = random.randint(3, 5)
                logger.info("BURST: generating %d rapid events", burst_count)
                for _ in range(burst_count):
                    await asyncio.sleep(random.uniform(1, 4))
                    burst_data = _gen_event()
                    burst_data["severity"] = random.randint(7, 10)
                    geo = None
                    if burst_data["lat"] and burst_data["lon"]:
                        geo = from_shape(
                            Point(burst_data["lon"], burst_data["lat"]), srid=4326
                        )
                    async with async_session() as session:
                        be = Event(
                            source=burst_data["source"],
                            channel_name=burst_data["channel_name"],
                            raw_text=burst_data["raw_text"],
                            summary=burst_data["summary"],
                            event_type=burst_data["event_type"],
                            severity=burst_data["severity"],
                            lat=burst_data["lat"],
                            lon=burst_data["lon"],
                            geometry=geo,
                            timestamp=burst_data["timestamp"],
                            evidence_span=evidence_span(burst_data["raw_text"], ""),
                        )
                        session.add(be)
                        await session.commit()
                        await session.refresh(be)
                        ws = EventWS(
                            type="new_event",
                            event=EventRead.model_validate(be),
                        )
                        await broadcaster.broadcast(ws.model_dump_json())

            # Wait 15-45 seconds before next event
            await asyncio.sleep(random.uniform(15, 45))

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("Demo event generator error: %s", e)
            await asyncio.sleep(5)


# =========================================================================
# SYNTHETIC AIRCRAFT GENERATOR
# =========================================================================

class SyntheticAircraft:
    """A single synthetic aircraft flying between Middle East locations."""

    def __init__(self, template: dict, is_military: bool):
        self.callsign = template["callsign"]
        self.is_military = is_military
        self.icao24 = f"demo{hash(self.callsign) % 0xFFFFFF:06x}"

        if is_military:
            alt_min, alt_max = template.get("alt_range", (30000, 40000))
            self.altitude = random.uniform(alt_min, alt_max)
            self.speed_kts = template.get("speed_kts", 420)
        else:
            self.altitude = random.uniform(30000, 42000)
            self.speed_kts = template.get("speed_kts", 470)

        # Pick start and target
        start = _pick(LOCATIONS)
        target = _pick(LOCATIONS)
        self.lat = _jitter(start["lat"], 2.0)
        self.lon = _jitter(start["lon"], 2.0)
        self.target_lat = _jitter(target["lat"], 1.0)
        self.target_lon = _jitter(target["lon"], 1.0)
        self.heading = 0.0
        self._update_heading()

    def _update_heading(self):
        dlat = self.target_lat - self.lat
        dlon = self.target_lon - self.lon
        self.heading = (math.degrees(math.atan2(dlon, dlat)) + 360) % 360

    def step(self, dt_seconds: float = 15.0):
        """Advance position by dt_seconds."""
        # Convert speed from knots to degrees per second (very approximate)
        speed_deg_per_sec = (self.speed_kts * 1.852 / 111.0) / 3600.0

        dlat = self.target_lat - self.lat
        dlon = self.target_lon - self.lon
        dist = math.sqrt(dlat * dlat + dlon * dlon)

        # Arrived? Pick new target
        if dist < 0.5:
            new_target = _pick(LOCATIONS)
            self.target_lat = _jitter(new_target["lat"], 1.0)
            self.target_lon = _jitter(new_target["lon"], 1.0)
            self._update_heading()
            return

        # Move toward target
        move = speed_deg_per_sec * dt_seconds
        if move > dist:
            move = dist
        self.lat += (dlat / dist) * move + random.uniform(-0.01, 0.01)
        self.lon += (dlon / dist) * move + random.uniform(-0.01, 0.01)
        self._update_heading()

        # Slight altitude variation
        self.altitude += random.uniform(-50, 50)

    def to_dict(self) -> dict:
        return {
            "icao24": self.icao24,
            "callsign": self.callsign,
            "origin_country": "",
            "lon": round(self.lon, 4),
            "lat": round(self.lat, 4),
            "altitude": round(self.altitude),
            "velocity": round(self.speed_kts * 0.5144),  # kts to m/s
            "heading": round(self.heading, 1),
            "on_ground": False,
            "position_source": 0,
            "nac_p": 10,
            "nic": 8,
        }


_demo_aircraft: list[SyntheticAircraft] = []


def _init_demo_aircraft():
    """Initialize the fleet of synthetic aircraft."""
    global _demo_aircraft
    _demo_aircraft = []
    for tmpl in MILITARY_AIRCRAFT:
        _demo_aircraft.append(SyntheticAircraft(tmpl, is_military=True))
    for tmpl in CIVILIAN_AIRCRAFT:
        _demo_aircraft.append(SyntheticAircraft(tmpl, is_military=False))
    logger.info("Initialized %d synthetic aircraft", len(_demo_aircraft))


async def start_demo_aircraft_poller():
    """Background task that updates synthetic aircraft positions."""
    from app.services.opensky import _cache as opensky_cache

    _init_demo_aircraft()

    while True:
        for ac in _demo_aircraft:
            ac.step(dt_seconds=15.0)

        states = [ac.to_dict() for ac in _demo_aircraft]
        zones = _generate_demo_jamming(states)
        opensky_cache["states"] = states
        opensky_cache["timestamp"] = int(time.time())
        opensky_cache["source"] = "demo"
        opensky_cache["jamming"] = zones
        opensky_cache["jamming_status"] = "ok" if zones else "insufficient_coverage"
        opensky_cache["cells_evaluated"] = len(zones)
        opensky_cache["aircraft_evaluable"] = len(states)

        logger.debug("Demo aircraft: %d tracked", len(states))
        await asyncio.sleep(15)


def _generate_demo_jamming(states: list[dict]) -> list[dict]:
    """Simulate 1-3 GPS jamming zones."""
    from app.services.opensky import MIN_CELL_AIRCRAFT

    # Randomly create jamming zones near contested areas
    jam_locations = [
        {"lat": 33.5, "lon": 36.3, "label": "Damascus"},   # Syria
        {"lat": 35.5, "lon": 51.4, "label": "Tehran"},      # Iran
        {"lat": 33.9, "lon": 35.5, "label": "Beirut"},      # Lebanon
        {"lat": 26.5, "lon": 56.3, "label": "Hormuz"},      # Strait of Hormuz
    ]
    zones = []
    remaining = len(states)  # a cell cannot hold more aircraft than the fleet has
    for jl in jam_locations:
        if remaining < MIN_CELL_AIRCRAFT:
            break
        if random.random() < 0.4:  # 40% chance each zone is active
            total = random.randint(MIN_CELL_AIRCRAFT, min(remaining, 40))
            degraded = max(1, round(total * random.uniform(0.3, 0.9)))
            remaining -= total
            zones.append({
                "lat": jl["lat"] + random.uniform(-0.3, 0.3),
                "lon": jl["lon"] + random.uniform(-0.3, 0.3),
                "radius_km": random.uniform(30, 80),
                "degraded": degraded,
                "total": total,
                "ratio": degraded / total,
                "intensity": degraded / total,
            })
    return zones


# =========================================================================
# SYNTHETIC VESSEL GENERATOR
# =========================================================================

class SyntheticVessel:
    """A single synthetic vessel following a shipping lane."""

    def __init__(self, template: dict, idx: int):
        self.mmsi = f"{template['mmsi_prefix']}{random.randint(10000000, 99999999)}"
        self.name = template["name"]
        self.ship_type_name = template["type"]
        speed_min, speed_max = template["speed_range"]
        self.speed = random.uniform(speed_min, speed_max)

        # Pick a route and starting position
        self.route = list(_pick(SHIPPING_ROUTES))
        self.route_idx = random.randint(0, len(self.route) - 2)
        self.progress = random.random()  # 0-1 along current segment
        self.direction = 1 if random.random() > 0.3 else -1  # mostly forward

        wp_a = self.route[self.route_idx]
        wp_b = self.route[min(self.route_idx + 1, len(self.route) - 1)]
        self.lat = wp_a[0] + (wp_b[0] - wp_a[0]) * self.progress
        self.lon = wp_a[1] + (wp_b[1] - wp_a[1]) * self.progress
        self.heading = 0.0
        self.destination = _pick(["FUJAIRAH", "JEBEL ALI", "RAS TANURA", "YANBU", "SUEZ", "ADEN", "BANDAR ABBAS", "BUSHEHR", ""])
        self._update_heading()

    def _update_heading(self):
        next_idx = self.route_idx + self.direction
        if 0 <= next_idx < len(self.route):
            wp = self.route[next_idx]
            dlat = wp[0] - self.lat
            dlon = wp[1] - self.lon
            self.heading = (math.degrees(math.atan2(dlon, dlat)) + 360) % 360

    def step(self, dt_seconds: float = 10.0):
        """Advance vessel along its route."""
        # Speed in degrees per second (very approximate for ocean)
        speed_deg = (self.speed * 1.852 / 111.0) / 3600.0
        move = speed_deg * dt_seconds

        next_idx = self.route_idx + self.direction
        if next_idx < 0 or next_idx >= len(self.route):
            self.direction *= -1
            next_idx = self.route_idx + self.direction

        wp = self.route[next_idx]
        dlat = wp[0] - self.lat
        dlon = wp[1] - self.lon
        dist = math.sqrt(dlat * dlat + dlon * dlon)

        if dist < 0.1:
            # Reached waypoint, advance to next
            self.route_idx = next_idx
            self._update_heading()
        else:
            self.lat += (dlat / dist) * move + random.uniform(-0.003, 0.003)
            self.lon += (dlon / dist) * move + random.uniform(-0.003, 0.003)
            self._update_heading()

    def to_dict(self) -> dict:
        return {
            "mmsi": self.mmsi,
            "name": self.name,
            "lat": round(self.lat, 4),
            "lon": round(self.lon, 4),
            "speed": round(self.speed, 1),
            "heading": round(self.heading, 1),
            "course": round(self.heading, 1),
            "nav_status": 0,
            "ship_type_name": self.ship_type_name,
            "destination": self.destination,
            "last_seen": time.time(),
        }


_demo_vessels: list[SyntheticVessel] = []


def _init_demo_vessels():
    global _demo_vessels
    _demo_vessels = []
    for i, tmpl in enumerate(VESSEL_TEMPLATES):
        _demo_vessels.append(SyntheticVessel(tmpl, i))
    logger.info("Initialized %d synthetic vessels", len(_demo_vessels))


async def start_demo_vessel_poller():
    """Background task that updates synthetic vessel positions."""
    from app.services.maritime import _cache as maritime_cache

    _init_demo_vessels()

    while True:
        for v in _demo_vessels:
            v.step(dt_seconds=10.0)

        vessel_dict = {}
        for v in _demo_vessels:
            d = v.to_dict()
            vessel_dict[d["mmsi"]] = d

        maritime_cache["vessels"] = vessel_dict
        maritime_cache["last_update"] = time.time()

        logger.debug("Demo vessels: %d tracked", len(vessel_dict))
        await asyncio.sleep(10)
