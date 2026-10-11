"""The link between events: the rule that says two rows are one incident.

Phase 3's "link, don't merge", the half between events. dedup.check_duplicate
merges two reports into one row when they arrive within 15 minutes, in the
same place or both unplaced, and their summaries agree; 83 of the 112
same-incident pairs in the gold set lie where it never looks — beyond 15
minutes, one side unlocated, or 50–500 km apart (FINDINGS.md, Measured facts,
"The gold set"). A link is the second matcher at the lower bar. It never moves
anything between the rows: no severity, no reliability, no death toll, no
count. It writes one row that says "these two are one incident, at this score,
for these reasons", and the reader sees the score.

Rule v1, decided 2026-10-11 (FINDINGS.md, Roadmap, "Link between events —
decisions"):

  candidates   same event_type, within WINDOW_MIN of each other, both within
               MAX_KM when both are located, any unlocated mix — the population
               the gold pairs were drawn from, so the numbers transfer
  score        containment over the word sets of raw_text with tags and URLs
               stripped: |A∩B| / min(|A|, |B|), how much of the shorter text
               the longer one holds — a repost that adds a paragraph scores 1.0
  link         score > BAR and neither veto fires
  place veto   the two stored location names are both names (not "" and not
               the classifier's "Unknown") and incompatible: equal, or one a
               part of the other, after normalisation; not a geocoder
  towns veto   the towns the two TEXTS name (after "town of", "in", "near" and
               the Arabic equivalents) are both non-empty and share none. The
               stored name is often a region — "West Bank", "Southern Lebanon"
               — and one outlet's template with a different town in it is the
               false positive a text score cannot see

Measured on the labelled gold pairs by the scorer in tools/, which imports THIS
module so the number is the shipped rule's, never a copy's, and pins the counts. The bar and
the vetoes were chosen on those pairs, so the figures are in-sample and
optimistic; the pairs are agreement with one labeller and reviewed by nobody.

Stdlib only, on purpose: tools/ runs on the host without the backend's
dependencies. normalize() must equal dedup._normalize (pinned by
tests/unit/test_linkrule.py), so the two matchers read the same words.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass

RULE = "v1"
BAR = 0.4
WINDOW_MIN = 180.0
MAX_KM = 500.0

_TAG = re.compile(r"<[^>]+>")
_URL = re.compile(r"https?://\S+|t\.co/\S+")


def normalize(text: str) -> set[str]:
    """dedup._normalize: lowercase, strip punctuation, words longer than 2."""
    words = re.sub(r"[^\w\s]", "", text.lower()).split()
    return {w for w in words if len(w) > 2}


def clean_text(text: str) -> str:
    """raw_text without HTML tags and URLs — the RSS feeds carry both."""
    return " ".join(_URL.sub(" ", _TAG.sub(" ", text or "")).split())


def containment(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


# ── places ───────────────────────────────────────────────────────────────────


def named(loc: str | None) -> bool:
    """A stored location name that names something. classifier.clean_location
    folds "", n/a, various and multiple into the literal "Unknown", and the
    fallback writes it when it finds nothing: that is "I could not tell", and
    two of those are not agreement about a place."""
    return bool(loc) and loc.strip().casefold() != "unknown"


def norm_loc(s: str | None) -> str:
    s = re.sub(r"[^\w\s]", " ", (s or "").casefold())
    return re.sub(r"^(the|al|el)\s+", "", " ".join(s.split()))


def loc_compatible(a: str | None, b: str | None) -> bool:
    """Equal after normalisation, or one names a part of the other ("Beirut" ~
    "southern Beirut"). Not a geocoder: "Lebanon" is not "Beirut"."""
    a, b = norm_loc(a), norm_loc(b)
    if not a or not b:
        return False
    return a == b or (" " + a + " ") in (" " + b + " ") or (" " + b + " ") in (" " + a + " ")


def place_relation(a: str | None, b: str | None) -> str:
    """"same", "different" or "unknown" for two stored location names."""
    if not named(a) or not named(b):
        return "unknown"
    return "same" if loc_compatible(a, b) else "different"


# The towns a text names. English: a place word followed by "of", or a
# preposition, then one or two capitalised words. Arabic: the place word then
# one or two tokens. Both read the cleaned text and return casefolded names.
_NAME = r"([A-Z][\w'’-]+(?:\s+(?:[Aa]l-)?[A-Z][\w'’-]+)?)"
_PLACE_OF = re.compile(
    r"\b(?:town|city|village|camp|neighbou?rhood|area|outskirts|vicinity|district|entrance|governorate|suburb|port"
    r"|airport|base)s?\s+of\s+(?:the\s+)?" + _NAME
)
_PREP = re.compile(r"\b(?:in|near|at|from|over|across|towards|toward|into|on)\s+(?:the\s+)?" + _NAME)
_ARABIC = re.compile(
    "(?:بلدة|مدينة|قرية|منطقة|مخيم|حي|ضاحية|ميناء|مطار|موقع|بلدات|قرى)\\s+([^\\s،,.:;]+(?:\\s+(?!جنوب|شمال|شرق|غرب|في|من)[^\\s،,.:;]+)?)"
)
# Words that follow "in" or "of" without naming a town: nationalities,
# directions, calendar words, the countries and regions the stored name
# already covers, and the place words themselves when they come out alone.
_STOP = frozenset(
    "israeli palestinian lebanese iranian syrian american iraqi yemeni egyptian jordanian turkish russian ukrainian "
    "breaking hezbollah hamas the a an occupied southern northern eastern western central west east north south "
    "monday tuesday wednesday thursday friday saturday sunday january february march april may june july august "
    "september october november december israel lebanon palestine gaza iran syria iraq yemen us united "
    "city town village camp area district".split()
)


def towns(text: str) -> set[str]:
    t = clean_text(text)
    out: set[str] = set()
    for rx in (_PLACE_OF, _PREP, _ARABIC):
        for m in rx.finditer(t):
            name = m.group(1).strip().casefold().replace("’", "'")
            name = re.sub(r"'s$", "", name)
            words = [w for w in name.split() if w not in _STOP]
            if words:
                out.add(" ".join(words))
    return out


def towns_relation(text_a: str, text_b: str) -> str:
    """"same" when the two texts name a town in common, "different" when both
    name towns and share none, "unknown" when either names none."""
    ta, tb = towns(text_a), towns(text_b)
    if not ta or not tb:
        return "unknown"
    return "same" if any(loc_compatible(x, y) for x in ta for y in tb) else "different"


# ── the rule ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Assessment:
    """What the rule saw. `linked` is the verdict; the rest is why, and is
    stored with the link so a reader can disagree with the bar."""
    score: float
    place: str  # place_relation of the stored names
    towns: str  # towns_relation of the texts
    linked: bool


def assess(text_a: str, loc_a: str | None, text_b: str, loc_b: str | None) -> Assessment:
    """The rule on two candidates. Time and distance are the candidate query's
    business (links.candidates); this reads only the texts and the names."""
    score = containment(normalize(clean_text(text_a)), normalize(clean_text(text_b)))
    place = place_relation(loc_a, loc_b)
    tw = towns_relation(text_a, text_b)
    linked = score > BAR and place != "different" and tw != "different"
    return Assessment(score=round(score, 4), place=place, towns=tw, linked=linked)


def within_window(dt_min: float, km: float | None) -> bool:
    """The candidate population: within WINDOW_MIN, and within MAX_KM when
    both sides are located (km is None when either is not)."""
    return abs(dt_min) <= WINDOW_MIN and (km is None or km <= MAX_KM)
