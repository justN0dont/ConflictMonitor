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
from collections import OrderedDict

import httpx

logger = logging.getLogger(__name__)

# =========================================================================
# LRU CACHE
# =========================================================================

_cache: OrderedDict[str, tuple[float, float] | None] = OrderedDict()
_CACHE_MAX = 1000


def _cache_get(key: str) -> tuple[float, float] | None | bool:
    normalized = key.strip().lower()
    if normalized in _cache:
        _cache.move_to_end(normalized)
        return _cache[normalized]
    return False


def _cache_set(key: str, value: tuple[float, float] | None):
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
    "aerospace force":             (35.7100, 51.4200),   # IRGC aerospace, Tehran
    "irgc aerospace":              (35.7100, 51.4200),
    "masjed soleyman":             (31.9390, 49.3050),   # IRGC air base, SW Iran
    "dezful":                      (32.3811, 48.4018),
    "dezful airbase":              (32.3811, 48.4018),
    "ahvaz":                       (31.3183, 48.6706),
    "ahvaz airbase":               (31.3183, 48.6706),
    "omidiyeh":                    (30.8350, 49.5350),   # IRIAF base
    "hamadan":                     (34.8685, 48.5355),
    "shahrokhi airbase":           (34.8685, 48.5355),
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
    "beer sheva":                  (31.2518, 34.7913),
    "be'er sheva":                 (31.2518, 34.7913),
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
    "us fifth fleet":              (26.2200, 50.5500),
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
    "central command":             (25.1175, 51.3150),   # CENTCOM = Al Udeid
    "centcom":                     (25.1175, 51.3150),
    "us central command":          (25.1175, 51.3150),
    "fifth fleet":                 (26.2200, 50.5500),
    "iaf":                         (31.2083, 34.9390),   # Israeli AF → Nevatim
    "idf":                         (32.0790, 34.7860),   # IDF HQ → Kirya
    "irgc":                        (35.7156, 51.4063),   # IRGC HQ
}

# =========================================================================
# NOMINATIM CLIENT
# =========================================================================

_nominatim_semaphore = asyncio.Semaphore(1)
_last_nominatim_call = 0.0
_VIEWBOX = "25,8,70,42"  # lon_min, lat_min, lon_max, lat_max  (wider Middle East)


async def _query_nominatim(location_name: str) -> tuple[float, float] | None:
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
                        lat = float(results[0]["lat"])
                        lon = float(results[0]["lon"])
                        logger.info("Nominatim resolved '%s' → (%.4f, %.4f)", location_name, lat, lon)
                        return (lat, lon)
                else:
                    logger.warning("Nominatim HTTP %d for '%s'", resp.status_code, location_name)
        except Exception as e:
            logger.error("Nominatim error for '%s': %s", location_name, e)

    return None


# =========================================================================
# PUBLIC API
# =========================================================================

async def geocode(location_name: str) -> tuple[float, float] | None:
    """Resolve a location name to (lat, lon).

    Pipeline:
      1. Cache
      2. Directional region table ("southern Lebanon" → Tyre region)
      3. Precision facility table (400+ entries)
      4. Partial match (longest matching substring wins)
      5. Nominatim API
    """
    if not location_name or location_name.strip().lower() in ("unknown", "n/a", "", "various", "multiple"):
        return None

    name = location_name.strip()
    normalized = name.lower()

    # 1. Cache
    cached = _cache_get(name)
    if cached is not False:
        return cached

    # 2. Directional region table (exact match on normalized)
    if normalized in _DIRECTIONAL_REGIONS:
        coords = _DIRECTIONAL_REGIONS[normalized]
        _cache_set(name, coords)
        logger.debug("Directional region '%s' → %s", name, coords)
        return coords

    # 3. Precision table — exact match
    if normalized in KNOWN_LOCATIONS:
        coords = KNOWN_LOCATIONS[normalized]
        _cache_set(name, coords)
        logger.debug("Precision table exact '%s' → %s", name, coords)
        return coords

    # 4. Partial match — find longest known name contained in the query
    #    (handles "strike near Natanz" → "natanz", "Fordow enrichment complex" → "fordow enrichment")
    best_match: tuple[float, float] | None = None
    best_len = 0
    for known_name, coords in KNOWN_LOCATIONS.items():
        if known_name in normalized and len(known_name) > best_len:
            best_match = coords
            best_len = len(known_name)
    if not best_match:
        for known_name, coords in _DIRECTIONAL_REGIONS.items():
            if known_name in normalized and len(known_name) > best_len:
                best_match = coords
                best_len = len(known_name)
    if best_match:
        _cache_set(name, best_match)
        logger.debug("Partial match '%s' (len=%d) → %s", name, best_len, best_match)
        return best_match

    # 5. Nominatim
    result = await _query_nominatim(name)
    _cache_set(name, result)
    return result
