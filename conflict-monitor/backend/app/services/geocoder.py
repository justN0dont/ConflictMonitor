"""Geocoding service — resolves location names to lat/lon coordinates.

Pipeline:
  1. LRU cache (in-memory, 500 entries)
  2. Hardcoded precision table (~400 Middle East locations, facilities, districts)
  3. Directional qualifier resolution ("southern Lebanon" → Tyre region)
  4. Nominatim / OpenStreetMap API (rate-limited to 1 req/sec per ToS)

PRECISION PHILOSOPHY:
  OSINT messages name specific facilities, neighborhoods, and bases — not just
  countries. This table prioritises sub-city precision. "Dahieh" resolves to
  the Dahieh district of Beirut (~4km south of city centre), not Beirut centre.
  "Natanz" resolves to the actual enrichment facility (33.72N, 51.73E), not
  the nearby city of Isfahan.

  Coordinate sources:
  - Military/nuclear sites: cross-referenced from declassified NGA data,
    Google Earth, and GlobalSecurity.org
  - Urban districts: OpenStreetMap centroid
  - Chokepoints/straits: standard nautical waypoints
"""

import asyncio
import logging
import re
from collections import OrderedDict
from math import cos, hypot, radians
from typing import NamedTuple

import httpx

logger = logging.getLogger(__name__)

# =========================================================================
# PRECISION MODEL
# =========================================================================
# A bare (lat, lon) is the same shape whether it came from a centrifuge hall
# or from "United States", so the caller could not tell a 500m answer from a
# continental one. GeoResult carries how precise the answer is and how it was
# reached. Returning None still means "not a place / unresolved" — that case
# is not a coordinate and must never be given one.

class GeoResult(NamedTuple):
    lat: float
    lon: float
    precision: str       # facility | city | admin1 | country_centroid | region_named
    # None means the extent was never measured and no estimate applies. It is
    # not "zero" and not "small": callers must render it as unknown rather
    # than invent a bound. Only the Nominatim path can produce it, when the
    # response carried no bounding box.
    uncertainty_m: int | None
    method: str          # table-exact | directional-exact | partial | nominatim


PRECISION_FACILITY = "facility"
PRECISION_CITY = "city"
PRECISION_ADMIN1 = "admin1"
PRECISION_COUNTRY = "country_centroid"
PRECISION_REGION = "region_named"

# Per-tier uncertainty for table hits. THESE ARE ESTIMATES, NOT MEASUREMENTS.
# The table stores one point per name and carries no extent, so there is
# nothing here to measure: a facility is a few hundred metres across, a city
# centroid is wrong by single-digit kilometres at the edges, an admin1
# centroid by ~100km, and a named sea or strait is tens of km wide. Nominatim
# hits do NOT use this map — their uncertainty is computed from the response's
# own bounding box, which is a real measurement of the thing that was found.
_TIER_UNCERTAINTY_M: dict[str, int] = {
    PRECISION_FACILITY: 500,
    PRECISION_CITY: 10_000,
    PRECISION_ADMIN1: 100_000,
    PRECISION_REGION: 50_000,
    PRECISION_COUNTRY: 500_000,
}

# Exactly one tier coarser. A partial match ("strike near Natanz") names a
# place the event was NEAR, not the place itself, so it may never claim the
# tier an exact hit would have claimed. Tiers not listed are unchanged: a
# region or a country centroid is already as coarse as it gets.
_COARSER: dict[str, str] = {
    PRECISION_FACILITY: PRECISION_CITY,
    PRECISION_CITY: PRECISION_ADMIN1,
}

# Most precise first. Used to pick a winner when several table keys share one
# coordinate (see _build_coord_precision).
_PRECISION_RANK: dict[str, int] = {
    PRECISION_FACILITY: 0,
    PRECISION_CITY: 1,
    PRECISION_REGION: 2,
    PRECISION_ADMIN1: 3,
    PRECISION_COUNTRY: 4,
}

# Water names, split by scale. Both are derived from the key itself so that
# adding "Gulf of Sidra" to the table needs no second edit here.
#
# The split exists because one water tier was a lie at both ends: a strait is
# a chokepoint tens of km across, while "Arabian Sea" is 2,400km across and
# "Mediterranean" 3,900km. Calling both region_named stamped ±50km on a body
# of water the size of a continent — a 48x understatement in the one field
# that is supposed to bound the answer.
_CHOKEPOINT_RE = re.compile(
    r"\b(strait|straits|bab|mandeb|mandab|hormuz|channel|canal|bay)\b"
)
_OPEN_WATER_RE = re.compile(r"\b(sea|gulf|ocean|waters)\b|mediterranean")

# Either kind of water, for callers that only care that it is not land.
_WATER_RE = re.compile(
    _CHOKEPOINT_RE.pattern + "|" + _OPEN_WATER_RE.pattern
)

# Key text that names a built facility rather than a settlement.
#
# 'mosque' joined the list with 'al-aqsa mosque' (2026-09-21). It changed the
# tier of no existing key — there were none containing the word — so it is a
# rule for the entry that arrived with it, not a reclassification of the table.
_FACILITY_RE = re.compile(
    r"\b(airbase|air base|afb|base|facility|enrichment|port|refinery|terminal"
    r"|plant|reactor|airport|field|complex|hq|mosque)\b"
)

# Table keys that name an area rather than a settlement or a structure.
#
# This is an explicit list, and it is explicit on purpose. Every other tier in
# this file is derived from the key's own text, because "airbase" and "strait"
# are words that carry their own scale. "Oman", "Sinai" and "Nineveh" do not:
# nothing in those strings distinguishes a governorate of 37,000 km2 from a
# town, and no regex ever will — it is world knowledge, not spelling. The
# alternative was to keep defaulting them to city, which is what shipped:
# Oman resolved to a point in empty desert and claimed ±10km, while Iran, the
# same class of name, reached Nominatim and honestly reported ±1,231km. Two
# orders of magnitude apart, and the confident one was the wrong one.
#
# It names only the exceptions — twenty entries out of 392, not an audit of
# the table. Anything absent keeps its derived tier.
_AREA_KEYS: dict[str, str] = {
    "oman":                 PRECISION_COUNTRY,   # country centroid, empty desert
    "kuwait":               PRECISION_ADMIN1,    # the country, not Kuwait City
    "sinai":                PRECISION_ADMIN1,    # ~60,000 km2
    "west bank":            PRECISION_ADMIN1,
    "golan":                PRECISION_ADMIN1,
    "golan heights":        PRECISION_ADMIN1,
    "nineveh":              PRECISION_ADMIN1,    # governorate
    "bekaa":                PRECISION_ADMIN1,    # governorate
    "beqaa":                PRECISION_ADMIN1,
    "west bekaa":           PRECISION_ADMIN1,
    "nabatieh governorate": PRECISION_ADMIN1,    # 'nabatieh' the city stays city
    "baalbek district":     PRECISION_ADMIN1,    # 'baalbek' the city stays city
    "qalamoun":             PRECISION_ADMIN1,    # mountain region
    "ghawar":               PRECISION_ADMIN1,    # oil field, ~280km long
    # Added 2026-09-21 with the entries below. Each tier is the nearest one
    # that does NOT understate the extent Nominatim measured for the name, and
    # that direction is deliberate: _TIER_UNCERTAINTY_M is a table of estimates,
    # so a table hit trades a measured bound for a guessed one, and the guess
    # must be the wide one. Erring narrow is the 48x understatement that split
    # _CHOKEPOINT_RE from _OPEN_WATER_RE.
    "galilee":              PRECISION_ADMIN1,    # region; derived tier was city
    "western galilee":      PRECISION_ADMIN1,
    "upper galilee":        PRECISION_ADMIN1,
    # Governorate, 138,000 km2 — an admin1 and nothing else. Its measured
    # extent does not fit admin1's estimate; see _KEY_UNCERTAINTY_M, which is
    # where that fact now lives.
    "anbar":                PRECISION_ADMIN1,
    "al-anbar":             PRECISION_ADMIN1,
    # The EMIRATE, measured at +/-50.5km — not Fujairah city and not the oil
    # terminal the messages name, both of which are inside that bound. A port
    # entry would need its own resolution and does not have one.
    "fujairah":             PRECISION_ADMIN1,
}


# Per-KEY uncertainty, for the case a tier cannot carry. MEASUREMENTS, not
# estimates: each is the half-diagonal `_query_nominatim` computed for that name
# on 2026-09-21, kept because the tier's estimate understates it.
#
# It exists because geo_precision is NOT a private uncertainty knob. It is a
# category the UI renders as words — frontend/src/lib/tokens.ts turns
# country_centroid into "country-level" — so tiering Anbar as a country to
# borrow the 500km that goes with it told every reader the monitor had resolved
# an Iraqi governorate only as far as a country, and counted it into the
# country-level rollup. Two facts were being forced through one field: WHAT
# KIND of place was named, and HOW WIDE the answer is. Oman is in _AREA_KEYS at
# country tier because Oman IS a country and the label is literally true there;
# Anbar was the first non-country key given that tier, and the label was the
# price. The tier now says what was named and the number below says how wide.
#
# ONLY ONE DIRECTION BELONGS HERE. _TIER_UNCERTAINTY_M holds estimates and a
# table hit trades a measured bound for a guessed one, so the guess must err
# WIDE: where it already does — 'fujairah' takes admin1's 100km against a
# measured 50.5km — the estimate stands and no entry is needed. An entry is
# only for a tier that errs NARROW against a measurement, which is the
# understatement that split _CHOKEPOINT_RE from _OPEN_WATER_RE.
#
# Exact hits only. A partial match ("strike near Ben Gurion Airport") has
# already been coarsened one tier on purpose, to 10km here, and a measured
# extent of the key is not the bound for something merely NEAR it. The cost is
# recorded rather than hidden: "Anbar Governorate" partial-matches 'anbar' and
# still takes admin1's 100km, because admin1's own 100km — unmeasured, and what
# actually failed here — is a question about _TIER_UNCERTAINTY_M and the other
# 17 keys _AREA_KEYS declares admin1, not about these four entries.
_KEY_UNCERTAINTY_M: dict[str, int] = {
    "anbar":                            352_000,  # admin1's 100km is 3.5x narrow
    "al-anbar":                         352_000,
    # The runway and terminals, measured at 3,265m against the facility tier's
    # 500m — 6.5x narrow. The tier is right about what this is (a built
    # facility, which is why it beats 'tel aviv' 13km away) and wrong about how
    # big it is. Widening the tier itself would move ~8 other airport keys on
    # one measurement, so the measurement stays where it was made.
    "ben gurion airport":                 3_265,
    "ben gurion international airport":   3_265,
}


def _key_precision(key: str) -> str:
    """Tier implied by one table key's own text."""
    area = _AREA_KEYS.get(key)
    if area is not None:
        return area
    if _CHOKEPOINT_RE.search(key):
        return PRECISION_REGION
    if _OPEN_WATER_RE.search(key):
        return PRECISION_COUNTRY
    if _FACILITY_RE.search(key):
        return PRECISION_FACILITY
    return PRECISION_CITY


def _build_coord_precision(
    table: dict[str, tuple[float, float]]
) -> dict[tuple[float, float], str]:
    """Resolve a tier per COORDINATE, not per key.

    A bare name and its qualified aliases share one point: "natanz",
    "natanz enrichment" and "natanz nuclear" are all (33.7224, 51.7261), and
    that point is the enrichment complex — the module docstring says so. The
    bare key names no facility, so keying off it alone would call the
    centrifuge halls a city. Instead every key pointing at a point votes and
    the most precise vote wins. That keeps the tier derived from the table
    rather than from a hand-audit of 385 entries.
    """
    best: dict[tuple[float, float], str] = {}
    for key, coords in table.items():
        tier = _key_precision(key)
        current = best.get(coords)
        if current is None or _PRECISION_RANK[tier] < _PRECISION_RANK[current]:
            best[coords] = tier
    return best


# =========================================================================
# LRU CACHE
# =========================================================================

_cache: OrderedDict[str, GeoResult | None] = OrderedDict()
_CACHE_MAX = 1000


def _cache_get(key: str) -> GeoResult | None | bool:
    normalized = key.strip().lower()
    if normalized in _cache:
        _cache.move_to_end(normalized)
        return _cache[normalized]
    return False


def _cache_set(key: str, value: GeoResult | None):
    normalized = key.strip().lower()
    _cache[normalized] = value
    _cache.move_to_end(normalized)
    while len(_cache) > _CACHE_MAX:
        _cache.popitem(last=False)


# =========================================================================
# DIRECTIONAL QUALIFIERS
# Handles OSINT phrases like "northern Israel", "south Lebanon", "near Tehran"
# Maps to approximate region centroids, not country/city centres.
# =========================================================================

_DIRECTIONAL_REGIONS: dict[str, tuple[float, float]] = {
    # Israel regions
    "northern israel":        (32.90, 35.50),   # Galilee / Haifa district
    "north israel":           (32.90, 35.50),
    "southern israel":        (30.60, 34.90),   # Negev
    "south israel":           (30.60, 34.90),
    "central israel":         (32.07, 34.90),   # Greater Tel Aviv
    "central israel coast":   (32.07, 34.90),
    "israel coast":           (32.30, 34.87),
    # Lebanon regions
    "southern lebanon":       (33.27, 35.43),   # Tyre / Nabatieh
    "south lebanon":          (33.27, 35.43),
    "northern lebanon":       (34.44, 35.83),   # Tripoli area
    "north lebanon":          (34.44, 35.83),
    "eastern lebanon":        (33.55, 35.90),   # Beqaa valley
    "beqaa valley":           (33.55, 35.90),
    "bekaa valley":           (33.55, 35.90),
    "southern suburbs beirut":(33.854, 35.524),  # Dahieh
    "beirut suburbs":         (33.854, 35.524),
    # Syria regions
    "southern syria":         (33.10, 36.50),
    "eastern syria":          (35.30, 40.00),
    "northern syria":         (36.50, 37.50),
    "deir ezzor region":      (35.34, 40.14),
    # Iraq regions
    "western iraq":           (33.50, 41.50),
    "northern iraq":          (36.00, 43.50),   # Kurdistan
    "southern iraq":          (31.00, 46.50),
    # Iran regions
    "western iran":           (34.00, 47.00),
    "northern iran":          (36.50, 52.00),   # Alborz/Caspian
    "southern iran":          (27.50, 55.00),
    "southwestern iran":      (31.50, 49.50),
    "central iran":           (33.00, 51.50),
    "eastern iran":           (32.00, 59.00),
    # Yemen regions
    "northern yemen":         (16.00, 43.50),
    "western yemen":          (15.00, 43.00),
    "southern yemen":         (13.00, 45.00),
    # Gulf / sea areas
    "northern red sea":       (27.50, 33.80),
    "southern red sea":       (13.00, 43.50),
    "gulf of oman":           (24.50, 59.00),
    "northern persian gulf":  (29.50, 49.50),
    "strait of hormuz":       (26.57, 56.25),
}


# =========================================================================
# PRECISION LOCATION TABLE
# ~400 entries: facilities, districts, bases, ports, infrastructure
# =========================================================================
#
# ── Batch of 2026-09-21, marked `+2026-09-21` on every line it added ──────
#
# Picked by measurement, not by memory. `tools/gazetteer_gaps.py` replays this
# table against all 3,665 distinct `location_name` values in the 2026-08-18
# archive and ranks the ones it MISSES by how many EVENTS carry them, because
# forty spellings on one event each are worth less than one name on 4,970 and
# the table costs the same either way. 13,424 events (16.0%) miss it today.
#
# Two thirds of that bucket was deliberately NOT taken. "Iran" alone is 3,846
# events and "United States" 1,881; those already resolve to a centroid and the
# country tier is the honest answer, so a facility-shaped entry for them would
# be `C6` written by hand. Borders and rivers ("Lebanon-Israel border", "Litani
# River", 88 events, all sentinel in the archive) are lines, and the fix for a
# line is not a point.
#
# NO COORDINATE BELOW WAS RECALLED. Eight were RESOLVED: each came back from
# `_query_nominatim` — this file's own function, so the same URL, params,
# viewbox, contact-bearing User-Agent and >=1.1s spacing — on 2026-09-21, and
# each was then checked against an anchor ALREADY IN THIS TABLE before being
# written down:
#
#   query sent to Nominatim            ->  result            anchor check
#   "Karaj, Iran"                          35.8225,50.9905   38.9km from 'tehran'
#   "Galilee, Israel"                      32.8008,35.5890   13.8km from 'northern israel'
#   "Anbar Governorate, Iraq"              32.7889,41.6094   263.3km from 'baghdad'
#   "Arad, Israel"                         31.2612,35.2146   40.3km from 'beer sheva'
#   "Al-Aqsa Mosque, Jerusalem"            31.7763,35.2356   2.3km from 'jerusalem'
#   "Ben Gurion Airport"                   32.0027,34.8809   13.1km from 'tel aviv'
#   "Fujairah, United Arab Emirates"       25.4147,56.2314   99.5km from 'dubai'
#   "Beit Shemesh, Israel"                 31.7462,34.9887   21.4km from 'jerusalem'
#
# THE OTHER THREE WERE NOT RESOLVED, and say so on their own lines (four keys:
# the two Khiyam spellings share one comment). 'beersheba', 'prince sultan air
# base' and 'khiyam'/'al-khiyam' REUSE the coordinate of an alias already in
# this table, and spent no request. That is the right call for
# an alias — a second resolution of the same village buys a second point to
# disagree with — but it is a different provenance, and the comment is the
# whole audit trail for entries with no request behind them. What each of those
# lines records is the CHECK, not a derivation: the archive's own stored point
# for the new spelling, and its haversine distance from the coordinate being
# reused (0.683km, 0.493km, 2.126km). The distance is the evidence that the two
# spellings name one place; the coordinate is the neighbour's.
#
# FOUR CANDIDATES WERE LEFT OUT because Nominatim has no answer for them:
# "Dura, Hebron, Palestine", "Masafer Yatta", "RAF Akrotiri, Cyprus" and
# "Nabi Sheet, Lebanon" each returned HTTP 200 with a body of `[]`. That was
# confirmed rather than assumed — the four failed consecutively, which is also
# what a rate-limit burst looks like, and "the request failed" and "there is no
# such place" are the two states this project exists to keep apart. They cost
# four more requests to separate, and separating them was worth it: three of
# the four (Masafer Yatta 32 events, RAF Akrotiri 17, Nabi Sheet 17) are
# stored as the Indian Ocean sentinel in the archive, so they are exactly the
# rows where an invented point would look like an improvement. They stay
# missing, and missing is the honest state.
#
# "Taybeh" was left out for the opposite reason: it resolves too well. Its
# three archive spellings gave three points up to 50km apart and the raw_texts
# describe at least two different villages, so one key would pin one real place
# onto another.

KNOWN_LOCATIONS: dict[str, tuple[float, float]] = {

    # ─── IRAN: Nuclear & Missile Facilities ─────────────────────────────────
    "natanz":                      (33.7224, 51.7261),   # enrichment complex
    "natanz enrichment":           (33.7224, 51.7261),
    "natanz nuclear":              (33.7224, 51.7261),
    "fordow":                      (34.8834, 50.2278),   # FIXED — was wrong lon
    "fordow enrichment":           (34.8834, 50.2278),
    "fordow facility":             (34.8834, 50.2278),
    "fdo":                         (34.8834, 50.2278),
    "arak":                        (34.0975, 49.1947),   # IR-40 heavy water
    "arak nuclear":                (34.0975, 49.1947),
    "ir-40":                       (34.0975, 49.1947),
    "khondab":                     (34.0975, 49.1947),
    "bushehr":                     (28.8296, 50.8880),   # nuclear plant
    "bushehr nuclear":             (28.8296, 50.8880),
    "bushehr plant":               (28.8296, 50.8880),
    "parchin":                     (35.5167, 51.7667),   # military/explosive research
    "parchin complex":             (35.5167, 51.7667),
    "isfahan":                     (32.6546, 51.6680),
    "esfahan":                     (32.6546, 51.6680),
    "isfahan enrichment":          (32.5730, 51.4620),   # UCF — NOT city centre
    "isfahan nuclear":             (32.5730, 51.4620),
    "ucf isfahan":                 (32.5730, 51.4620),
    "shahroud":                    (36.2000, 55.0400),   # missile test facility
    "shahroud missile":            (36.2000, 55.0400),
    "semnan":                      (35.5762, 53.3975),   # space/missile launch
    "imam khomeini space":         (35.2345, 53.9211),   # IRGC space centre

    # ─── IRAN: IRGC Bases & Missile Complexes ──────────────────────────────
    "imam ali base":               (33.6110, 46.2630),   # W Iran, IRGC missiles
    "imam ali":                    (33.6110, 46.2630),
    "irgc hq":                     (35.7156, 51.4063),   # Tehran
    "irgc headquarters":           (35.7156, 51.4063),
    "khojir":                      (35.6990, 51.6010),   # missile factory E Tehran
    "khojir complex":              (35.6990, 51.6010),
    "shahid hemmat":               (35.7512, 51.2226),   # missile factory NW Tehran
    "hemmat":                      (35.7512, 51.2226),
    "shahid bagheri":              (35.6848, 51.4634),   # missile factory E Tehran
    "bagheri":                     (35.6848, 51.4634),
    # ('aerospace force' / 'irgc aerospace' deleted: the IRGC Aerospace Force is
    #  the branch that launches the missiles, so it is named in reports of strikes
    #  that landed 1,500km away. 'irgc hq' below is a building and stays.)
    "masjed soleyman":             (31.9390, 49.3050),   # IRGC air base, SW Iran
    "dezful":                      (32.3811, 48.4018),
    "dezful airbase":              (32.3811, 48.4018),
    "ahvaz":                       (31.3183, 48.6706),
    "ahvaz airbase":               (31.3183, 48.6706),
    "omidiyeh":                    (30.8350, 49.5350),   # IRIAF base
    "hamadan":                     (34.8685, 48.5355),
    # Shahrokhi is the pre-1979 name of Shahid Nojeh Air Base, which this
    # table already places at (35.2100, 48.6500) under three other keys. It
    # was tabled here on Hamadan CITY CENTRE, 40km away — an internal
    # contradiction the table settles by itself. Left as it was, the shared
    # coordinate made the tier vote stamp "facility ±500m" on a city of
    # 550,000 people.
    "shahrokhi airbase":           (35.2100, 48.6500),
    "nojeh":                       (35.2100, 48.6500),
    "shahid nojeh":                (35.2100, 48.6500),
    "nojeh airbase":               (35.2100, 48.6500),
    "tabriz airbase":              (38.1340, 46.2350),
    "tabriz":                      (38.0962, 46.2738),
    "mashhad airbase":             (36.2350, 59.6400),
    "mashhad":                     (36.2605, 59.6168),
    "mehrabad":                    (35.6892, 51.3100),   # Tehran military airport
    "mehrabad airport":            (35.6892, 51.3100),
    "tehran":                      (35.6892, 51.3890),
    "tehran north":                (35.8000, 51.4500),
    "tehran refinery":             (35.6300, 51.3300),
    # +2026-09-21. 50 events. The archive stored (48.701, 17.687) — Slovakia,
    # 3,096km away — for messages reading "Karaj, west of Tehran".
    "karaj":                       (35.8225, 50.9905),
    "isfahan":                     (32.6546, 51.6680),
    "shiraz":                      (29.5918, 52.5836),
    "shiraz airbase":              (29.5400, 52.5900),
    "qom":                         (34.6401, 50.8764),
    "tehran":                      (35.6892, 51.3890),
    "kharg island":                (29.2333, 50.3167),   # main oil export terminal
    "kharg":                       (29.2333, 50.3167),
    "lavan island":                (26.8108, 53.3528),   # offshore oil
    "bandar abbas":                (27.1865, 56.2808),
    "bandar abbas port":           (27.1080, 56.1350),
    "jask":                        (25.6395, 57.7703),   # new IRGC naval base
    "jask naval":                  (25.6395, 57.7703),
    "chabahar":                    (25.2919, 60.6430),
    "chahbahar":                   (25.2919, 60.6430),
    "qeshm":                       (26.8500, 55.9000),
    "qeshm island":                (26.8500, 55.9000),
    "bushehr port":                (28.9684, 50.8385),
    "bandar lengeh":               (26.5579, 54.8807),
    "tabas":                       (33.5983, 56.9261),
    "yazd":                        (31.8972, 54.3678),
    "kermanshah":                  (34.3142, 47.0650),
    "ilam":                        (33.6380, 46.4220),
    "abadan":                      (30.3392, 48.2973),
    "abadan refinery":             (30.3392, 48.2973),
    "khorramshahr":                (30.4333, 48.1750),
    "arvand":                      (30.4333, 48.1750),

    # ─── ISRAEL: Air Force Bases ────────────────────────────────────────────
    "nevatim":                     (31.2083, 34.9390),
    "nevatim airbase":             (31.2083, 34.9390),
    "nevatim afb":                 (31.2083, 34.9390),
    "ramon":                       (29.7290, 34.9563),   # southern AF base
    "ramon airbase":               (29.7290, 34.9563),
    "ramon afb":                   (29.7290, 34.9563),
    "tel nof":                     (31.8390, 34.8180),
    "tel nof airbase":             (31.8390, 34.8180),
    "palmachim":                   (31.8980, 34.6920),   # missile testing, UAVs
    "palmachim airbase":           (31.8980, 34.6920),
    "ramat david":                 (32.6650, 35.1790),
    "ramat david airbase":         (32.6650, 35.1790),
    "hatzor airbase":              (31.7635, 34.7269),
    "hatzor":                      (31.7635, 34.7269),
    "hatzerim airbase":            (31.2330, 34.6610),
    "hatzerim":                    (31.2330, 34.6610),
    "ovda airbase":                (29.9403, 34.9350),
    "ovda":                        (29.9403, 34.9350),

    # ─── ISRAEL: Military/Nuclear/Sensitive Sites ────────────────────────────
    "dimona":                      (31.0666, 35.2083),   # nuclear reactor
    "dimona reactor":              (31.0150, 35.1450),   # actual reactor site
    "negev nuclear":               (31.0150, 35.1450),
    "kirya":                       (32.0790, 34.7860),   # IDF HQ Tel Aviv
    "idf hq":                      (32.0790, 34.7860),
    "soreq":                       (31.8970, 34.7610),   # nuclear research
    "soreq nuclear":               (31.8970, 34.7610),
    "sdot micha":                  (31.7000, 34.9380),   # Jericho ICBM base (approx)
    "jericho missile":             (31.7000, 34.9380),
    "nahal sorek":                 (31.8970, 34.7610),

    # ─── ISRAEL: Cities & Districts ─────────────────────────────────────────
    "tel aviv":                    (32.0853, 34.7818),
    "greater tel aviv":            (32.0853, 34.7818),
    "gush dan":                    (32.0853, 34.7818),
    "haifa":                       (32.7940, 34.9896),
    "haifa port":                  (32.8190, 34.9980),
    "haifa bay":                   (32.8600, 35.0500),
    "jerusalem":                   (31.7683, 35.2137),
    "west jerusalem":              (31.7760, 35.2050),
    "east jerusalem":              (31.7810, 35.2290),
    # +2026-09-21, 59 events, 43 of which currently answer as 'jerusalem' via
    # partial match — the city centre standing in for a building 2.3km away.
    # Bare "al-aqsa" is deliberately NOT a key: the Al-Aqsa Martyrs Brigades are
    # an actor and "Al-Aqsa Flood" is an operation, and the note at the foot of
    # this table explains what happens when an actor is given a coordinate.
    "al-aqsa mosque":              (31.7763, 35.2356),
    "al aqsa mosque":              (31.7763, 35.2356),
    "beer sheva":                  (31.2518, 34.7913),
    "be'er sheva":                 (31.2518, 34.7913),
    # +2026-09-21, 36 events. Pure alias gap, and NOT resolved through
    # Nominatim: this is the 'beer sheva' coordinate above, copied. The
    # archive's own stored point for "Beersheba" is (31.2457442, 34.7925181),
    # which is 0.683km away and is the evidence that the two spellings name one
    # city — not the coordinate written here.
    "beersheba":                   (31.2518, 34.7913),
    # +2026-09-21, 76 events — the highest-volume single candidate in the pool.
    "arad":                        (31.2612, 35.2146),
    "beit shemesh":                (31.7462, 34.9887),   # +2026-09-21, 27 events
    # +2026-09-21, 48 events. The archive resolved "Galilee" to (40.34,-73.97)
    # — Long Island — "Western Galilee" to Australia and "Upper Galilee" to the
    # Belgian coast. The two qualified keys take the Galilee's own point: at the
    # admin1 bound declared in _AREA_KEYS the sub-region is inside the answer,
    # which is a containment claim and not a second resolution.
    "galilee":                     (32.8008, 35.5890),
    "western galilee":             (32.8008, 35.5890),
    "upper galilee":               (32.8008, 35.5890),
    # +2026-09-21, 44 events. Also the fix for a partial-match hijack: with no
    # key of its own, "Ben Gurion Airport, Tel Aviv" matched 'tel aviv' and
    # answered with the city centre 13km away, at a tier it had not earned.
    # Bare "ben gurion" is deliberately NOT a key — Ben-Gurion University is in
    # Beer Sheva, 70km from this runway, and the airport must not swallow it.
    "ben gurion airport":          (32.0027, 34.8809),
    "ben gurion international airport": (32.0027, 34.8809),
    "eilat":                       (29.5577, 34.9519),
    "eilat port":                  (29.5500, 34.9480),
    "ashdod":                      (31.8014, 34.6503),
    "ashdod port":                 (31.8168, 34.6466),
    "ashkelon":                    (31.6688, 34.5743),
    "sderot":                      (31.5251, 34.5960),
    "netanya":                     (32.3328, 34.8599),
    "rishon lezion":               (31.9730, 34.7868),
    "petah tikva":                 (32.0869, 34.8878),
    "nahariya":                    (33.0073, 35.0974),
    "acre":                        (32.9228, 35.0684),
    "akko":                        (32.9228, 35.0684),
    "tiberias":                    (32.7940, 35.5300),
    "safed":                       (32.9648, 35.4987),
    "metula":                      (33.2780, 35.5710),   # northern border
    "kiryat shmona":               (33.2070, 35.5710),
    "golan heights":               (32.9500, 35.8000),
    "golan":                       (32.9500, 35.8000),
    "west bank":                   (31.9522, 35.2332),
    "jenin":                       (32.4618, 35.2961),
    "nablus":                      (32.2211, 35.2544),
    "ramallah":                    (31.9038, 35.2034),
    "hebron":                      (31.5326, 35.0998),
    "tulkarm":                     (32.3120, 35.0282),
    "jericho":                     (31.8550, 35.4444),
    "gaza":                        (31.5017, 34.4668),
    "gaza city":                   (31.5017, 34.4668),
    "rafah":                       (31.2969, 34.2455),
    "rafah crossing":              (31.2730, 34.2220),
    "khan younis":                 (31.3462, 34.3061),
    "deir al-balah":               (31.4133, 34.3536),
    "jabalia":                     (31.5317, 34.4818),
    "beit hanoun":                 (31.5350, 34.5350),

    # ─── LEBANON: Districts & Sites ─────────────────────────────────────────
    "beirut":                      (33.8938, 35.5018),
    "downtown beirut":             (33.8938, 35.5018),
    "dahieh":                      (33.8547, 35.5233),   # Hezbollah southern suburbs
    "dahiyeh":                     (33.8547, 35.5233),
    "southern suburbs":            (33.8547, 35.5233),
    "southern suburbs beirut":     (33.8547, 35.5233),
    "beirut port":                 (33.9000, 35.5192),
    "beirut airport":              (33.8208, 35.4884),   # RHIA
    "sidon":                       (33.5633, 35.3697),
    "saida":                       (33.5633, 35.3697),
    "tyre":                        (33.2705, 35.2038),
    "sur":                         (33.2705, 35.2038),
    "nabatieh":                    (33.3778, 35.4834),
    "nabatieh governorate":        (33.3778, 35.4834),
    "bint jbeil":                  (33.1200, 35.4320),
    "bint jbayl":                  (33.1200, 35.4320),
    "khiam":                       (33.3440, 35.5980),
    # +2026-09-21, 47 events, and the worst single error the ranking found.
    # "Al-Khiyam" resolved to (16.822, 43.779) — Yemen, 2,011km away — on
    # messages reading "southern Lebanon's Shiite village of Al-Khiyam". NOT
    # resolved through Nominatim and no request was spent: both keys take the
    # 'khiam' coordinate above. The archive is what shows they are one village
    # — its stored point for bare "Khiyam" is (33.3272226, 35.6089702), 2.126km
    # from that key, against 2,011km for the spelling with the article.
    "al-khiyam":                   (33.3440, 35.5980),
    "khiyam":                      (33.3440, 35.5980),
    "marjayoun":                   (33.3640, 35.5820),
    "baalbek":                     (34.0047, 36.2110),
    "baalbek district":            (34.0047, 36.2110),
    "hermel":                      (34.3940, 36.3850),
    "qalamoun":                    (34.4330, 35.8330),
    "tripoli":                     (34.4333, 35.8333),
    "tripoli lebanon":             (34.4333, 35.8333),
    "bekaa":                       (33.8500, 35.9000),
    "beqaa":                       (33.8500, 35.9000),
    "west bekaa":                  (33.7000, 35.6800),
    "yaroun":                      (33.0940, 35.4440),   # southern Lebanon village
    "kfar kila":                   (33.2840, 35.5660),
    "aitaroun":                    (33.0660, 35.4080),
    "qana":                        (33.2100, 35.3000),

    # ─── SYRIA ──────────────────────────────────────────────────────────────
    "damascus":                    (33.5138, 36.2765),
    "damascus outskirts":          (33.4800, 36.2500),
    "mezzeh":                      (33.4830, 36.2190),   # Damascus military airport
    "mezzeh airbase":              (33.4830, 36.2190),
    "damascus airport":            (33.4114, 36.5158),
    "aleppo":                      (36.2021, 37.1343),
    "aleppo airbase":              (36.1780, 37.2240),
    "latakia":                     (35.5317, 35.7918),
    "khmeimim":                    (35.4010, 35.9490),   # Russian AB
    "khmeimim airbase":            (35.4010, 35.9490),
    "hmeimim":                     (35.4010, 35.9490),
    "tartus":                      (34.8890, 35.8866),
    "tartus naval base":           (34.9000, 35.8700),   # Russian naval facility
    "homs":                        (34.7272, 36.7200),
    "t4":                          (34.5222, 37.6275),
    "t-4":                         (34.5222, 37.6275),
    "tiyas":                       (34.5222, 37.6275),
    "tiyas airbase":               (34.5222, 37.6275),
    "palmyra":                     (34.5600, 38.2700),
    "deir ez-zor":                 (35.3359, 40.1408),
    "deir ezzor":                  (35.3359, 40.1408),
    "deir ez zor":                 (35.3359, 40.1408),
    "deir ezzor airport":          (35.2853, 40.1759),
    "abu kamal":                   (34.4509, 40.9188),
    "al-bukamal":                  (34.4509, 40.9188),
    "al bukamal":                  (34.4509, 40.9188),
    "qusayr":                      (34.5130, 36.5740),
    "idlib":                       (35.9306, 36.6339),
    "raqqa":                       (35.9594, 39.0078),
    "kobane":                      (36.8920, 38.3560),
    "manbij":                      (36.5178, 37.9467),
    "hasaka":                      (36.4820, 40.7490),
    "qamishli":                    (37.0527, 41.2279),
    "shayrat":                     (34.4900, 36.9130),   # Syrian AF base
    "shayrat airbase":             (34.4900, 36.9130),
    "seen airbase":                (33.6520, 36.8820),
    "marj sultan":                 (33.5700, 36.4400),   # Hezbollah airfield claim

    # ─── IRAQ ───────────────────────────────────────────────────────────────
    "baghdad":                     (33.3128, 44.3615),
    "baghdad airport":             (33.2625, 44.2346),   # BIAP
    "biap":                        (33.2625, 44.2346),
    "baghdad green zone":          (33.3089, 44.4054),
    "green zone baghdad":          (33.3089, 44.4054),
    "camp victory":                (33.2970, 44.2460),
    "camp slayer":                 (33.2880, 44.2670),
    "victoria base":               (33.2960, 44.2280),
    "camp taji":                   (33.5272, 44.2617),
    "taji":                        (33.5272, 44.2617),
    "al-taji":                     (33.5272, 44.2617),
    "balad airbase":               (33.9402, 44.3616),
    "balad":                       (33.9402, 44.3616),
    "ain al-asad":                 (33.7856, 42.4411),
    # +2026-09-21, 41 events. The archive stored (38.746, 35.352) — central
    # Turkey, 1,010km from Baghdad — for messages reading "Anbar, Iraq". It is
    # an Iraqi governorate, so the tier is admin1 (_AREA_KEYS), and its measured
    # 352km extent lives in _KEY_UNCERTAINTY_M rather than being forced through
    # the tier. An earlier draft tiered it country_centroid to borrow that
    # tier's wider bound — which made every Anbar event tell a reader the
    # monitor had resolved only a country, because geo_precision is rendered as
    # text in the UI. What kind of place was named and how wide the answer is
    # are two facts; one field cannot carry both.
    "anbar":                       (32.7889, 41.6094),
    "al-anbar":                    (32.7889, 41.6094),
    "al-asad airbase":             (33.7856, 42.4411),
    "al asad":                     (33.7856, 42.4411),
    "ain al asad":                 (33.7856, 42.4411),
    "q-west":                      (35.7650, 41.9230),   # Qayyarah West
    "qayyarah west":               (35.7650, 41.9230),
    "erbil":                       (36.1901, 44.0091),
    "erbil airport":               (36.2376, 43.9637),
    "erbil airbase":               (36.2376, 43.9637),
    "sulaymaniyah":                (35.5600, 45.4350),
    "kirkuk":                      (35.4681, 44.3953),
    "k1 airbase":                  (35.4680, 44.1050),
    "kirkuk airbase":              (35.4680, 44.1050),
    "mosul":                       (36.3409, 43.1300),
    "nineveh":                     (36.3409, 43.1300),
    "basra":                       (30.5085, 47.7804),
    "basra port":                  (30.4960, 47.8180),
    "umm qasr":                    (29.9880, 48.0170),   # Iraq's main port
    "tikrit":                      (34.6119, 43.6767),
    "samarra":                     (34.1977, 43.8747),
    "fallujah":                    (33.3500, 43.7797),
    "ramadi":                      (33.4234, 43.2997),
    "karbala":                     (32.6160, 44.0244),
    "najaf":                       (32.0013, 44.3237),
    "nasiriyah":                   (31.0586, 46.2620),
    "amara":                       (31.8355, 47.1527),
    "al-amarah":                   (31.8355, 47.1527),

    # ─── YEMEN ──────────────────────────────────────────────────────────────
    "sanaa":                       (15.3694, 44.1910),
    "sanaa airport":               (15.4773, 44.2197),
    "sana'a":                      (15.3694, 44.1910),
    "hodeidah":                    (14.7979, 42.9545),
    "hudaydah":                    (14.7979, 42.9545),
    "hodeida":                     (14.7979, 42.9545),
    "hodeidah port":               (14.8190, 42.9400),
    "ras isa":                     (15.4100, 42.7300),   # Yemen oil terminal
    "ras isa terminal":            (15.4100, 42.7300),
    "marib":                       (15.4543, 45.3220),
    "marib dam":                   (15.4590, 45.2560),
    "marib gas plant":             (15.4543, 45.3220),
    "aden":                        (12.7855, 45.0187),
    "aden port":                   (12.7880, 44.9980),
    "saada":                       (16.9400, 43.7600),
    "sa'ada":                      (16.9400, 43.7600),
    "taiz":                        (13.5775, 43.9908),
    "hajja":                       (15.7058, 43.6057),
    "al-bayda":                    (14.0000, 45.5700),

    # ─── SAUDI ARABIA ───────────────────────────────────────────────────────
    "riyadh":                      (24.7136, 46.6753),
    "king khalid airport":         (24.9578, 46.6989),   # KKIA Riyadh
    "jeddah":                      (21.4858, 39.1925),
    "jeddah port":                 (21.5345, 39.1380),
    "king fahd airport":           (26.4712, 49.7979),   # Dammam
    "dammam":                      (26.3927, 49.9777),
    "dhahran":                     (26.2172, 50.1971),
    "abqaiq":                      (25.9380, 49.6680),   # Aramco oil hub (attacked 2019)
    "abqaiq oil":                  (25.9380, 49.6680),
    "ghawar":                      (25.1360, 49.4190),   # world's largest oil field
    "ras tanura":                  (26.6550, 50.1700),   # oil export terminal
    "ras tanura refinery":         (26.6550, 50.1700),
    "safaniya":                    (27.9200, 48.7500),   # offshore oil terminal
    "jubail":                      (27.0020, 49.6200),
    "jubail industrial":           (27.0020, 49.6200),
    "prince sultan airbase":       (24.0620, 47.5810),
    # +2026-09-21, 37 events. NOT resolved through Nominatim: this is the
    # coordinate of the key above it, reused. What was checked is that the two
    # spellings are the same place — the archive's own stored point for the
    # spelling with the space is (24.066252, 47.5823882), 0.493km from the
    # point written here — and no request was spent.
    "prince sultan air base":      (24.0620, 47.5810),
    "al-kharj":                    (24.0620, 47.5810),
    "king khalid military city":   (27.9000, 45.5200),
    "tabuk":                       (28.3838, 36.5550),
    "tabuk airbase":               (28.3700, 36.6200),
    "mecca":                       (21.4225, 39.8262),
    "medina":                      (24.5247, 39.5692),
    "yanbu":                       (24.0880, 38.0580),   # industrial port

    # ─── UAE ────────────────────────────────────────────────────────────────
    "abu dhabi":                   (24.4539, 54.3773),
    "dubai":                       (25.2048, 55.2708),
    "jebel ali":                   (24.9960, 55.0570),   # Dubai's major port
    "jebel ali port":              (24.9960, 55.0570),
    "al dhafra":                   (24.2474, 54.5478),   # USAF base
    "al dhafra airbase":           (24.2474, 54.5478),
    "al minhad":                   (25.0260, 55.3660),
    "al minhad airbase":           (25.0260, 55.3660),
    "abu dhabi port":              (24.4800, 54.3600),
    "ruwais":                      (24.1100, 52.7300),   # refinery complex
    "fujairah":                    (25.4147, 56.2314),   # +2026-09-21, 34 events

    # ─── QATAR ──────────────────────────────────────────────────────────────
    "doha":                        (25.2854, 51.5310),
    "al udeid":                    (25.1175, 51.3150),   # USAF HQ CENTCOM
    "al udeid airbase":            (25.1175, 51.3150),
    "udeid":                       (25.1175, 51.3150),

    # ─── KUWAIT ─────────────────────────────────────────────────────────────
    "kuwait city":                 (29.3759, 47.9774),
    "kuwait":                      (29.3759, 47.9774),
    "ali al salem":                (29.3434, 47.5208),   # USAF base
    "ali al salem airbase":        (29.3434, 47.5208),
    "camp arifjan":                (29.1167, 48.0667),
    "camp buehring":               (29.6000, 47.8333),
    "mina abdullah":               (29.0440, 48.0800),   # Kuwait oil terminal

    # ─── BAHRAIN ────────────────────────────────────────────────────────────
    "manama":                      (26.2154, 50.5832),
    "bahrain":                     (26.2154, 50.5832),
    "nsf bahrain":                 (26.2200, 50.5500),   # US Naval Support Facility
    "mina salman":                 (26.2000, 50.6000),

    # ─── OMAN ───────────────────────────────────────────────────────────────
    "muscat":                      (23.5880, 58.3829),
    "oman":                        (22.0000, 57.0000),
    "masirah island":              (20.6740, 58.8980),   # US/UK airbase access
    "duqm":                        (19.6630, 57.7060),   # strategic port

    # ─── JORDAN ─────────────────────────────────────────────────────────────
    "amman":                       (31.9454, 35.9284),
    "muwaffaq salti":              (32.3560, 36.2590),   # RJAF + US forces
    "muwaffaq salti airbase":      (32.3560, 36.2590),
    "azraq airbase":               (31.8382, 36.7882),
    "h4 airbase":                  (33.1900, 38.2000),   # western Jordan
    "prince hassan":               (32.1607, 37.1489),   # RJAF

    # ─── TURKEY ─────────────────────────────────────────────────────────────
    "ankara":                      (39.9334, 32.8597),
    "istanbul":                    (41.0082, 28.9784),
    "incirlik":                    (37.0024, 35.4259),   # NATO airbase
    "incirlik airbase":            (37.0024, 35.4259),
    "diyarbakir":                  (37.9144, 40.2306),
    "batman airbase":              (37.9290, 41.1160),

    # ─── EGYPT ──────────────────────────────────────────────────────────────
    "cairo":                       (30.0444, 31.2357),
    "suez":                        (30.0130, 32.5498),
    "suez canal":                  (30.4358, 32.3443),
    "port said":                   (31.2565, 32.2841),
    "ismailia":                    (30.5852, 32.2654),
    "sharm el-sheikh":             (27.9157, 34.3300),
    "hurghada":                    (27.2578, 33.8116),
    "sinai":                       (29.7500, 33.8300),

    # ─── WATERWAYS & CHOKEPOINTS ─────────────────────────────────────────────
    "strait of hormuz":            (26.5667, 56.2500),
    "hormuz":                      (26.5667, 56.2500),
    "hormuz strait":               (26.5667, 56.2500),
    "bab el-mandeb":               (12.5833, 43.3333),
    "bab al-mandab":               (12.5833, 43.3333),
    "mandeb strait":               (12.5833, 43.3333),
    "suez canal":                  (30.4358, 32.3443),
    "red sea":                     (20.0000, 38.0000),
    "persian gulf":                (26.5000, 51.5000),
    "gulf of oman":                (24.5000, 59.0000),
    "gulf of aden":                (12.5000, 47.0000),
    "arabian sea":                 (15.0000, 65.0000),
    "mediterranean":               (34.0000, 33.0000),
    "eastern mediterranean":       (34.0000, 34.5000),

    # ─── DJIBOUTI ───────────────────────────────────────────────────────────
    "camp lemonnier":              (11.5500, 43.1667),
    "djibouti":                    (11.5721, 43.1456),

    # ─── PAKISTAN ───────────────────────────────────────────────────────────
    "islamabad":                   (33.6844, 73.0479),
    "karachi":                     (24.8607, 67.0011),

    # ─── RUSSIA (in context of Syria ops) ───────────────────────────────────
    "moscow":                      (55.7558, 37.6176),
    "sevastopol":                  (44.6166, 33.5254),

    # ─── UKRAINE (cross-conflict context) ───────────────────────────────────
    "kyiv":                        (50.4501, 30.5234),
    "kiev":                        (50.4501, 30.5234),
    "kharkiv":                     (49.9935, 36.2304),
    "odessa":                      (46.4825, 30.7233),

    # ─── DIEGO GARCIA ───────────────────────────────────────────────────────
    "diego garcia":                (-7.3195, 72.4229),

    # ─── ADDITIONAL KEY ALIASES ─────────────────────────────────────────────
    "khomeini":                    (35.6892, 51.3890),   # refers to Tehran
    # DO NOT re-add 'idf', 'iaf', 'irgc', 'centcom', 'central command',
    # 'us central command', 'fifth fleet' or 'us fifth fleet' here. They are
    # actors, not places: "IDF confirms strikes in Gaza" would pin on IDF HQ in
    # Tel Aviv, the wrong side of a border, and "US Central Command confirms
    # strikes in Yemen" would pin on Al Udeid in Qatar. Word boundaries cannot
    # save them — the name really is a whole word. An event is located by where
    # it happened, not by who is named in it. A headquarters is a building and
    # may stay ('idf hq', 'irgc headquarters'); a command or a fleet is not.
}

# =========================================================================
# WORD-BOUNDARY PATTERNS FOR PARTIAL MATCHING
# A plain substring test put 'arak' inside "Maarakeh" (south Lebanon → Arak,
# Iran, 1,100km away), 'kirya' inside "Kiryat Shemona" and 'oman' inside
# "Romania". Compiled once at import — the partial-match loop runs per message.
# =========================================================================

_KNOWN_PATTERNS: list[tuple[str, tuple[float, float], re.Pattern[str]]] = [
    (name, coords, re.compile(r"\b" + re.escape(name) + r"\b"))
    for name, coords in KNOWN_LOCATIONS.items()
]

_DIRECTIONAL_PATTERNS: list[tuple[str, tuple[float, float], re.Pattern[str]]] = [
    (name, coords, re.compile(r"\b" + re.escape(name) + r"\b"))
    for name, coords in _DIRECTIONAL_REGIONS.items()
]

# Derived tiers, built once at import from the tables above.
# A directional region is an admin1-scale area unless its name is water.
_KNOWN_PRECISION: dict[tuple[float, float], str] = _build_coord_precision(KNOWN_LOCATIONS)
_DIRECTIONAL_PRECISION: dict[str, str] = {
    name: (
        PRECISION_REGION if _CHOKEPOINT_RE.search(name)
        else PRECISION_COUNTRY if _OPEN_WATER_RE.search(name)
        else PRECISION_ADMIN1
    )
    for name in _DIRECTIONAL_REGIONS
}


def _precision_for_key(key: str, coords: tuple[float, float]) -> str:
    """Tier for one table lookup.

    _KNOWN_PRECISION resolves a tier per COORDINATE, which is what lets a bare
    "natanz" inherit "facility" from the qualified aliases sharing its point.
    That inference must not outrank a declaration: "nineveh" is tabled on
    Mosul's coordinate and "baalbek district" on Baalbek's, so the
    most-precise-vote rule handed a governorate the city's ±10km. An entry in
    _AREA_KEYS is a statement about what the NAME covers, so it wins over a
    tier inferred from whatever else happens to sit on the same point.
    """
    declared = _AREA_KEYS.get(key)
    if declared is not None:
        return declared
    return _KNOWN_PRECISION[coords]


def _table_result(
    coords: tuple[float, float],
    precision: str,
    method: str,
    uncertainty_m: int | None = None,
) -> GeoResult:
    return GeoResult(
        lat=coords[0],
        lon=coords[1],
        precision=precision,
        uncertainty_m=(
            _TIER_UNCERTAINTY_M[precision] if uncertainty_m is None else uncertainty_m
        ),
        method=method,
    )


# =========================================================================
# NOMINATIM CLIENT
# =========================================================================

_nominatim_semaphore = asyncio.Semaphore(1)
_last_nominatim_call = 0.0
_VIEWBOX = "25,8,70,42"  # lon_min, lat_min, lon_max, lat_max  (wider Middle East)


def _precision_from_bbox(bbox) -> tuple[str, int | None]:
    """Derive the tier from Nominatim's own bounding box.

    Nominatim returns "boundingbox" as [lat_min, lat_max, lon_min, lon_max]
    and this code used to throw it away, which is why "United States" came
    back looking exactly like a street address. The box is the extent of the
    thing that was found, so its diagonal is the only honest measure of how
    precise the returned centroid is.
    """
    # No usable extent in the response. Claim the coarsest tier — we cannot
    # tell a building from a country — and report uncertainty as None.
    #
    # It used to borrow 500km from _TIER_UNCERTAINTY_M here, which was the
    # one thing this function exists to stop: that map is a set of estimates
    # for table hits, nothing measured it, and on a Nominatim reply it is not
    # even the right order of magnitude (a measured US bbox is ±18,267km,
    # 36x larger). A number nobody measured does not belong in the field
    # whose whole purpose is to carry a measured bound.
    if not bbox or len(bbox) != 4:
        return PRECISION_COUNTRY, None
    try:
        lat_min, lat_max, lon_min, lon_max = (float(v) for v in bbox)
    except (TypeError, ValueError):
        return PRECISION_COUNTRY, None

    mid_lat = (lat_min + lat_max) / 2.0
    lat_span_m = abs(lat_max - lat_min) * 111_320.0
    lon_span_m = abs(lon_max - lon_min) * 111_320.0 * cos(radians(mid_lat))
    diagonal_m = hypot(lat_span_m, lon_span_m)

    if diagonal_m < 2_000:
        precision = PRECISION_FACILITY
    elif diagonal_m < 25_000:
        precision = PRECISION_CITY
    elif diagonal_m < 200_000:
        precision = PRECISION_ADMIN1
    else:
        precision = PRECISION_COUNTRY
    return precision, int(diagonal_m / 2)


async def _query_nominatim(location_name: str) -> GeoResult | None:
    global _last_nominatim_call

    async with _nominatim_semaphore:
        import time
        now = time.monotonic()
        elapsed = now - _last_nominatim_call
        if elapsed < 1.1:
            await asyncio.sleep(1.1 - elapsed)

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    "https://nominatim.openstreetmap.org/search",
                    params={
                        "q": location_name,
                        "format": "json",
                        "limit": 1,
                        "viewbox": _VIEWBOX,
                        "bounded": 0,
                    },
                    headers={
                        "User-Agent": "ConflictMonitor/1.0 (research; https://github.com/conflict-monitor)",
                    },
                )
                _last_nominatim_call = time.monotonic()

                if resp.status_code == 200:
                    results = resp.json()
                    if results:
                        top = results[0]
                        lat = float(top["lat"])
                        lon = float(top["lon"])
                        precision, uncertainty_m = _precision_from_bbox(
                            top.get("boundingbox")
                        )
                        logger.info(
                            "Nominatim resolved '%s' → (%.4f, %.4f) "
                            "[%s ±%sm, addresstype=%s]",
                            location_name, lat, lon, precision,
                            uncertainty_m if uncertainty_m is not None else "unmeasured",
                            top.get("addresstype") or top.get("type") or "?",
                        )
                        return GeoResult(
                            lat=lat,
                            lon=lon,
                            precision=precision,
                            uncertainty_m=uncertainty_m,
                            method="nominatim",
                        )
                else:
                    logger.warning("Nominatim HTTP %d for '%s'", resp.status_code, location_name)
        except Exception as e:
            logger.error("Nominatim error for '%s': %s", location_name, e)

    return None


# Values that are not locations and must never be geocoded. Removing the actor
# acronyms from KNOWN_LOCATIONS stopped them hijacking a real place ("IDF confirms
# strikes in Gaza" used to pin on IDF HQ in Tel Aviv), but a BARE acronym then fell
# through to Nominatim, which answers confidently and wrongly — "IDF" resolved to
# (40.18, 44.51), in Armenia. An actor is not a place; the honest answer is None.
_NOT_A_PLACE: frozenset[str] = frozenset({
    "unknown", "n/a", "", "various", "multiple", "none", "unspecified",
    # actors, not places
    "idf", "irgc", "iaf", "centcom", "isis", "nato", "un", "hamas", "hezbollah",
    "houthi", "houthis", "cia", "mossad", "pmf", "sdf",
})


# =========================================================================
# PUBLIC API
# =========================================================================

async def geocode(location_name: str) -> GeoResult | None:
    """Resolve a location name to a GeoResult, or None if it is not a place.

    Pipeline:
      1. Cache
      2. Directional region table ("southern Lebanon" → Tyre region)
      3. Precision facility table (400+ entries)
      4. Partial match (longest matching substring wins), one tier coarser
      5. Nominatim API

    None means "not a place, or unresolved". Callers must persist that as NULL
    geometry — there is no placeholder coordinate for an unknown location.
    """
    if not location_name or location_name.strip().lower() in _NOT_A_PLACE:
        return None

    name = location_name.strip()
    normalized = name.lower()

    # 1. Cache
    cached = _cache_get(name)
    if cached is not False:
        return cached

    # 2. Directional region table (exact match on normalized)
    if normalized in _DIRECTIONAL_REGIONS:
        result = _table_result(
            _DIRECTIONAL_REGIONS[normalized],
            _DIRECTIONAL_PRECISION[normalized],
            "directional-exact",
        )
        _cache_set(name, result)
        logger.debug("Directional region '%s' → %s", name, result)
        return result

    # 3. Precision table — exact match
    if normalized in KNOWN_LOCATIONS:
        coords = KNOWN_LOCATIONS[normalized]
        result = _table_result(
            coords,
            _precision_for_key(normalized, coords),
            "table-exact",
            _KEY_UNCERTAINTY_M.get(normalized),
        )
        _cache_set(name, result)
        logger.debug("Precision table exact '%s' → %s", name, result)
        return result

    # 4. Partial match — find longest known name appearing as whole words in the query
    #    (handles "strike near Natanz" → "natanz", "Fordow enrichment complex" → "fordow enrichment")
    #    The event happened NEAR the matched place, not AT it, so the tier drops
    #    one step: "strike near Natanz" is not Natanz.
    best_match: tuple[float, float] | None = None
    best_precision = PRECISION_CITY
    best_len = 0
    for known_name, coords, pattern in _KNOWN_PATTERNS:
        if len(known_name) > best_len and pattern.search(normalized):
            best_match = coords
            best_precision = _precision_for_key(known_name, coords)
            best_len = len(known_name)
    if not best_match:
        for known_name, coords, pattern in _DIRECTIONAL_PATTERNS:
            if len(known_name) > best_len and pattern.search(normalized):
                best_match = coords
                best_precision = _DIRECTIONAL_PRECISION[known_name]
                best_len = len(known_name)
    if best_match:
        coarser = _COARSER.get(best_precision, best_precision)
        result = _table_result(best_match, coarser, "partial")
        _cache_set(name, result)
        logger.debug("Partial match '%s' (len=%d) → %s", name, best_len, result)
        return result

    # 5. Nominatim
    result = await _query_nominatim(name)
    _cache_set(name, result)
    return result
