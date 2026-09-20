import asyncio
import json
import logging
import re

import anthropic
import httpx
from pydantic import BaseModel, Field, ValidationError, field_validator

from app.config import settings

logger = logging.getLogger(__name__)

# Maps flag emoji to country/city for pre-extraction before Claude sees the text
FLAG_TO_LOCATION = {
    "🇮🇷": "Iran",
    "🇮🇱": "Israel",
    "🇺🇸": "United States",
    "🇱🇧": "Lebanon",
    "🇾🇪": "Yemen",
    "🇸🇾": "Syria",
    "🇮🇶": "Iraq",
    "🇸🇦": "Saudi Arabia",
    "🇯🇴": "Jordan",
    "🇪🇬": "Egypt",
    "🇹🇷": "Turkey",
    "🇷🇺": "Russia",
    "🇬🇧": "United Kingdom",
    "🇫🇷": "France",
    "🇩🇪": "Germany",
    "🇨🇳": "China",
    "🇵🇸": "Gaza",
    "🇰🇼": "Kuwait",
    "🇶🇦": "Qatar",
    "🇦🇪": "UAE",
    "🇴🇲": "Oman",
    "🇧🇭": "Bahrain",
    "🇵🇰": "Pakistan",
}

# Known location keywords for regex fallback — ordered from MOST to LEAST specific
# (specific facilities listed before their host cities so longest match wins)
LOCATION_KEYWORDS = [
    # Iran — specific facilities first
    "Natanz", "Fordow", "Parchin", "Khojir", "Arak nuclear", "IR-40",
    "Imam Ali base", "Shahid Hemmat", "Shahid Bagheri", "IRGC HQ",
    "Bushehr nuclear", "Bushehr plant", "Kharg Island", "Kharg",
    "Bandar Abbas", "Jask naval", "Jask", "Chabahar", "Qeshm",
    "Mehrabad", "Dezful", "Ahvaz", "Abadan", "Shahrokhi",
    "Nojeh", "Omidiyeh", "Shahroud", "Tabriz", "Mashhad",
    "Isfahan enrichment", "Isfahan", "Isfahan nuclear",
    "Tehran", "Shiraz", "Qom", "Kermanshah", "Hamadan",
    # Israel — specific bases/districts first
    "Nevatim", "Palmachim", "Tel Nof", "Ramat David", "Hatzerim",
    "Hatzerим", "Ramon airbase", "Hatzor", "Ovda", "Kirya", "IDF HQ",
    "Dimona", "Soreq", "Negev Nuclear",
    "Tel Aviv", "Haifa port", "Haifa", "Beer Sheva", "Eilat",
    "Ashdod port", "Ashdod", "Ashkelon", "Sderot",
    "Nahariya", "Kiryat Shmona", "Metula", "Golan Heights", "Golan",
    "West Bank", "Jenin", "Nablus", "Ramallah", "Hebron",
    "Gaza City", "Gaza", "Rafah", "Khan Younis", "Jabalia",
    # Lebanon — southern suburbs and districts before city
    "Dahieh", "Dahiyeh", "Southern suburbs Beirut", "Southern suburbs",
    "Beirut port", "Beirut airport", "Beirut",
    "Bint Jbeil", "Nabatieh", "Khiam", "Marjayoun",
    "Tyre", "Sidon", "Baalbek", "Bekaa", "Beqaa",
    "South Lebanon", "Southern Lebanon", "Tripoli Lebanon",
    # Syria
    "Mezzeh", "Damascus airport", "Damascus", "Shayrat airbase", "Shayrat",
    "T-4", "T4", "Tiyas", "Khmeimim", "Hmeimim",
    "Tartus naval", "Tartus", "Latakia", "Aleppo",
    "Deir ez-Zor", "Deir Ezzor", "Abu Kamal", "Al-Bukamal",
    "Homs", "Idlib", "Raqqa", "Qusayr",
    # Iraq
    "Al-Asad", "Ain al-Asad", "Camp Taji", "Taji", "Balad",
    "Camp Victory", "Q-West", "K1 airbase",
    "Baghdad airport", "Baghdad Green Zone", "Baghdad",
    "Erbil airport", "Erbil", "Basra port", "Basra",
    "Umm Qasr", "Mosul", "Kirkuk", "Fallujah", "Ramadi",
    "Najaf", "Karbala", "Samarra",
    # Yemen
    "Sanaa airport", "Sanaa", "Sana'a",
    "Hodeidah port", "Hodeidah", "Hodeida",
    "Ras Isa terminal", "Ras Isa",
    "Marib dam", "Marib gas", "Marib",
    "Aden port", "Aden", "Saada", "Taiz",
    # Saudi
    "Abqaiq", "Ras Tanura", "Safaniya", "Ghawar",
    "Prince Sultan airbase", "King Khalid Military City",
    "Riyadh", "Jeddah", "Dammam", "Dhahran", "Jubail",
    # UAE/Qatar/Kuwait/Bahrain
    "Al Udeid", "Al Dhafra", "Al Minhad", "Jebel Ali",
    "Doha", "Abu Dhabi", "Dubai", "Ruwais",
    "Kuwait City", "Ali Al Salem", "Camp Arifjan",
    "Manama", "NSF Bahrain",
    # Waterways
    "Strait of Hormuz", "Hormuz", "Bab el-Mandeb", "Bab al-Mandab",
    "Suez Canal", "Red Sea", "Persian Gulf", "Gulf of Oman",
    "Gulf of Aden", "Arabian Sea", "Mediterranean",
    # Bases
    "Diego Garcia", "Camp Lemonnier", "Incirlik",
    "Muwaffaq Salti", "Al Udeid", "Al Dhafra",
]

SYSTEM_PROMPT = """You are a precision OSINT analyst classifying conflict intelligence messages about the Iran-Israel-US war.

Your job: extract structured, factual event data. You apply the same rigorous standard to ALL sources regardless of which side they support.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
STEP 1 — REJECT NON-EVENTS IMMEDIATELY
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Return {"event_type":"military","severity":1,"location_name":"Unknown","summary":"[NOISE]"}
for ANY of these:
  • Commentary, opinions, reactions: "Wait I thought...", "Is he serious?", "Can you believe..."
  • Questions with no factual event content
  • Social-media meta: subscribe, follow, join our channel, link in bio
  • Test messages or placeholder text
  • Pure political opinion without a specific verifiable event
  • Raw URL dumps with no descriptive context
  • Messages under 10 words that describe no specific incident
  • Propaganda claims with NO verifiable supporting detail (e.g. "We have destroyed everything")

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
STEP 2 — CLASSIFY THE EVENT
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Flag emojis indicate countries involved:
🇮🇷=Iran 🇮🇱=Israel 🇺🇸=USA 🇱🇧=Lebanon 🇾🇪=Yemen 🇸🇾=Syria 🇮🇶=Iraq 🇸🇦=Saudi Arabia 🇰🇼=Kuwait 🇶🇦=Qatar 🇦🇪=UAE 🇵🇸=Gaza

Extract these fields as JSON:
- event_type: "military" | "diplomatic" | "economic" | "humanitarian" | "cyber"
- severity: 1-10 integer (see guide below)
- killed_reported: integer, or null (see KILLED_REPORTED below)
- location_name: THE MOST PRECISE location (see LOCATION HIERARCHY)
- summary: one factual, neutral sentence — no adjectives, no opinion, no framing

SUMMARY RULES:
  ✓ "IDF conducted airstrikes on weapon depots near Damascus airport"
  ✓ "IRGC launched ballistic missiles toward targets in northern Israel"
  ✗ Never use "terrorist", "resistance", "genocide", "liberation", "brutal", "heroic"
  ✗ Never add evaluative words: "devastating", "powerful", "cowardly", "justified"
  ✗ Describe the event, not its moral status
  ✗ Do not attribute blame beyond what the source explicitly states

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
LOCATION HIERARCHY (most → least specific)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  LEVEL 1 — Facility / base / district / port / airfield:
    "Natanz enrichment facility", "Fordow", "Dahieh", "Nevatim AFB",
    "Al Udeid", "Ain al-Asad", "Kharg Island", "Parchin", "Kirya",
    "Marib dam", "Ras Tanura", "Jebel Ali port", "Abqaiq"
  LEVEL 2 — Named sub-area within a city:
    "southern suburbs Beirut", "Baghdad airport", "Haifa port",
    "Mezzeh airbase", "Erbil airport", "Hodeidah port"
  LEVEL 3 — City: "Tehran", "Tel Aviv", "Beirut", "Baghdad", "Sanaa"
  LEVEL 4 — Region: "southern Lebanon", "northern Israel", "western Iraq"
  LEVEL 5 — Country (only if nothing more specific exists)

KEY RULES:
  - NEVER return country when city/facility is mentioned
  - "Dahieh" > "Beirut", "Natanz" > "Isfahan" > "Iran", "Al Udeid" > "Qatar"
  - "southern suburbs" in Beirut → "Dahieh"
  - "Ain al-Asad" / "Al-Asad airbase" → "Ain al-Asad"
  - NEVER return "Unknown" if ANY location is mentioned anywhere

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SEVERITY GUIDE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  1:   Noise/commentary — not a real event (return [NOISE])
  2:   Unverifiable claim, likely propaganda
  3-4: Official statements, diplomacy, troop movements, sirens, exercises
  5-6: Confirmed strikes on infrastructure, missile salvos, ship attacks
  7-8: Nuclear facility strikes, capital city attacks, carrier movements, mass casualties
  9-10: Nuclear device use, capital destroyed, war-defining escalation

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
KILLED_REPORTED — the ONE number
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Put the number of people the message says were KILLED.
  - Copy the number from the text. Never estimate one.
  - "more than 150", "at least 60", "nearly 200" -> 150, 60, 200.
  - One person killed, named or described -> 1.
  - Wounded, injured, hospitalised and missing do NOT go here.
  - If the message does not state how many were killed -> null.
  - A number or null. NEVER true or false — this is a count, not a yes/no.
Most messages carry no number. null is the normal answer.

Respond ONLY with valid JSON. No markdown, no explanation.
Example: {"event_type":"military","severity":7,"killed_reported":null,"location_name":"Natanz","summary":"Israeli airstrikes targeted centrifuge halls at the Natanz enrichment facility."}
Example: {"event_type":"military","severity":6,"killed_reported":17,"location_name":"Zrariyeh","summary":"An Israeli airstrike on Zrariyeh killed 17 people."}"""


class ClassifierResult(BaseModel):
    event_type: str = "military"
    # default=None, NOT 5. With default=5 a reply that simply omitted the key
    # validated clean and emerged as a median threat stamped
    # extraction_status="ok" — an invented measurement indistinguishable from
    # a real one. None means Claude did not give us a severity.
    severity: int | None = Field(default=None, ge=1, le=10)
    # The one component of the severity bake-off that measured well on its own:
    # people this message SAYS were killed, copied out of the text rather than
    # graded. Optional because "the message states no count" is the normal
    # answer: 95.5% of the 83,938 stored events in the 2026-08-18 archive state
    # no death toll, measured by tools/archive_killed_rate.py — this comment
    # used to assert ~86% and no measurement existed anywhere. Quote the 95.5%
    # with the two caveats the script prints, because neither is small: the
    # population is STORED EVENTS, not messages seen (noise and low-severity
    # articles are dropped before insert), and the patterns catch only the
    # phrasings written into them, so it is an UPPER bound on the null rate,
    # not a point estimate.
    #
    # And it is NOT zero — 0 means the message said nobody was killed (88 rows
    # in that archive did), None means it said nothing. An absent key is legal
    # here, unlike severity: a reply that simply omits it has still classified
    # the event.
    #
    # Two rules, from two different places. Out of range — negative, or absurd
    # — is refused by ge/le below and stays a parse failure rather than being
    # clamped, which is the rule severity's own ge/le has always followed. The
    # bool rule is separate: pydantic would validate true/false as 1/0 for both
    # fields, so reject_boolean below raises on them for killed_reported AND
    # for severity.
    killed_reported: int | None = Field(default=None, ge=0, le=1_000_000)
    location_name: str = "Unknown"
    summary: str = ""
    is_noise: bool = False   # set by post-init logic, not from Claude output

    @field_validator("event_type")
    @classmethod
    def validate_event_type(cls, v):
        allowed = {"military", "diplomatic", "economic", "humanitarian", "cyber"}
        return v if v in allowed else "military"

    # clamp_severity was deleted here, not repaired. It was dead code:
    # Field(ge=1, le=10) rejects an out-of-range value before a mode="after"
    # validator ever runs, so severity=11 raised ValidationError and never
    # reached the clamp. Moving the clamp to mode="before" would have brought
    # it to life — and that is the wrong repair. A reply of 11 is not a 10; it
    # is a reply that did not follow the schema. Clamping would manufacture a
    # measurement out of a parse failure, which is the exact defect this pass
    # exists to remove. Out of range stays a parse failure, retried, and
    # finally recorded as a fallback with no severity at all.

    @field_validator("severity", "killed_reported", mode="before")
    @classmethod
    def reject_boolean(cls, v, info):
        # Both fields are numbers, and pydantic validates bool as int: true
        # arrives as 1 and false as 0. A model that read either question as a
        # yes/no answers true, and that answer then validates clean.
        #
        # killed_reported: a model that read "were people killed" instead of
        # "how many" answers true. The row is then stamped
        # extraction_status="ok" and the feed prints "1 KILLED": a yes/no guess
        # wearing a measurement's clothes, which is the one thing this field
        # exists to prevent. The local qwen3:8b makes that misreading likelier
        # than Haiku did.
        #
        # severity is the worse of the two, and the reason this validator
        # covers both fields instead of one. true coerces to 1, and 1 is the
        # [NOISE] score: it trips the `severity <= 1` branch in
        # _handle_response, which sets is_noise=True with
        # extraction_status="ok", and the caller drops the event before insert.
        # A model answering the yes/no reading of the severity question — "is
        # this severe?" — therefore ERASES the event. That is state 3 ("I
        # looked and couldn't tell") written into state 1 ("nothing happened"),
        # and unlike a bad killed_reported it leaves NO ROW BEHIND to audit.
        # Only the bool is refused: an integer severity of 1 is a real answer
        # meaning noise, and still takes that branch exactly as before.
        #
        # Rejected, not coerced to None. For killed_reported, None is a claim —
        # the message stated no count — and a model answering true is saying
        # the opposite: that deaths were reported and it failed to give the
        # figure. No integer and no null in this schema means "I could not
        # tell", and the answer to that is not to elect the least wrong one. A
        # reply that answered a different question is a parse failure, the rule
        # both fields' out-of-range values already follow.
        #
        # The price is paid knowingly: the ValidationError this raises is
        # caught around ClassifierResult(**raw) and discards the whole row,
        # good severity and location with it. Anthropic retries twice first;
        # Ollama does not retry, so one bool costs that event. A row saying "I
        # could not parse this" is still true, and a row saying "the message
        # reported no deaths" when nothing said so is not — and nothing later
        # can tell the second kind apart from a real null. For severity the
        # trade is not even close: the alternative is not a wrong row but no
        # row.
        #
        # bool only. '17' must still coerce (LLMs emit quoted numbers
        # constantly) and every other value keeps the behaviour it had.
        if isinstance(v, bool):
            raise ValueError(
                f"{info.field_name} must be a number or null, not true/false"
            )
        return v

    @field_validator("location_name")
    @classmethod
    def clean_location(cls, v):
        # Reject vague/empty values
        if not v or v.strip().lower() in ("unknown", "n/a", "", "various", "multiple"):
            return "Unknown"
        return v.strip()

    @field_validator("summary")
    @classmethod
    def flag_noise_summary(cls, v):
        # Claude returns "[NOISE]" summary for commentary/junk — pass through
        return v.strip()


def _extract_flags(text: str) -> list[str]:
    """Pull country names from flag emojis at start of message."""
    countries = []
    for flag, country in FLAG_TO_LOCATION.items():
        if flag in text[:20]:  # flags are usually at the very start
            countries.append(country)
    return countries


# Word-boundary patterns, compiled once at import — _regex_location_fallback runs
# per message. A plain substring test matched "Kirya" (IDF HQ, Tel Aviv) inside
# "Kiryat Shemona" on the Lebanese border.
_LOCATION_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (loc, re.compile(r"\b" + re.escape(loc.lower()) + r"\b"))
    for loc in LOCATION_KEYWORDS
]


def _regex_location_fallback(text: str) -> str | None:
    """Find the most specific (longest) location keyword in the text."""
    text_lower = text.lower()
    best: str | None = None
    best_len = 0
    for loc, pattern in _LOCATION_PATTERNS:
        if len(loc) > best_len and pattern.search(text_lower):
            best = loc
            best_len = len(loc)
    return best


_client: anthropic.AsyncAnthropic | None = None

# Recorded as extraction_model on every row this backend produces, so a row
# can say which model classified it instead of the reader having to guess.
ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"  # Haiku: faster + cheaper for classification

# There is ONE GPU. The RSS poller classifies articles in a loop, and
# concurrent /api/generate calls would thrash VRAM (and on a cold start can
# make Ollama load the model more than once). One generate at a time.
_OLLAMA_SEM = asyncio.Semaphore(1)

# One budget, named once. A cold start loads the model into VRAM and measured
# ~20s, so 30s would fail every cold start; a bigger model would take longer.
_OLLAMA_TIMEOUT = 180.0

# qwen3 is a reasoning model. With format:"json" it emitted no <think> block in
# testing, but if it ever does the block precedes the JSON and would otherwise
# read as a parse failure. Leading only — never strip mid-payload.
_THINK_RE = re.compile(r"\A\s*<think>.*?</think>\s*", re.DOTALL)


def _get_client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    return _client


def _handle_response(
    text: str, raw_text: str, flag_countries: list[str], model: str
) -> dict:
    """Turn one LLM reply into a classified dict.

    Shared by BOTH backends deliberately: fence stripping, schema validation,
    the [NOISE]/severity-1 rule and the location fallback have to be identical
    for Anthropic and Ollama, or the archive stops being comparable across the
    two. Raises on unusable output — the caller decides retry vs parse_failed.
    """
    text = text.strip()
    # Strip any accidental markdown fences
    text = re.sub(r"^```json\s*|```$", "", text, flags=re.MULTILINE).strip()
    raw = json.loads(text)
    if not isinstance(raw, dict):
        # Valid JSON, but a list/number/string is not a classification. Raised
        # as a decode error so both backends record it as parse_failed.
        raise json.JSONDecodeError("expected a JSON object", text, 0)
    result = ClassifierResult(**raw)
    classified = result.model_dump()
    if classified["severity"] is None:
        # The prompt mandates a severity, so a reply without one ("{}", or an
        # object of keys we do not know) extracted nothing. Recording that as
        # extraction_status "ok" files a row claiming a classification that
        # never happened — severity NULL and summary empty, but indistinguishable
        # from a real result by its status. Same rule as the out-of-range case:
        # a schema violation is a parse failure, not a measurement.
        raise json.JSONDecodeError("reply carried no severity", text, 0)
    # Which model actually produced this row. Without it an archive mixing
    # Haiku and qwen3 rows is uninterpretable.
    classified["extraction_model"] = model

    # The LLM signals noise/commentary by returning summary="[NOISE]" or severity=1
    # Mark it so callers can fast-drop without geocoding.
    # A missing severity is NOT a severity of 1: an omitted key says
    # nothing about whether the message was noise, so it must not
    # short-circuit to a drop here.
    severity = classified["severity"]
    if classified["summary"] == "[NOISE]" or (severity is not None and severity <= 1):
        classified["is_noise"] = True
        classified["extraction_status"] = "ok"
        logger.debug("Classifier: noise/commentary dropped: %s", raw_text[:80])
        return classified

    # If the LLM returned Unknown, try regex fallback before giving up
    if classified["location_name"] == "Unknown":
        regex_loc = _regex_location_fallback(raw_text)
        if regex_loc:
            classified["location_name"] = regex_loc
            logger.debug("Regex fallback location: '%s'", regex_loc)
        elif flag_countries:
            # Last resort: use primary flag country
            classified["location_name"] = flag_countries[0]
            logger.debug("Flag fallback location: '%s'", flag_countries[0])

    classified["is_noise"] = False
    classified["extraction_status"] = "ok"
    logger.info(
        "Classified [%s]: type=%s sev=%s killed=%s loc='%s'",
        model,
        classified["event_type"],
        classified["severity"],
        classified["killed_reported"],
        classified["location_name"],
    )
    return classified


async def _classify_ollama(raw_text: str, hint: str, flag_countries: list[str]) -> dict:
    """Classify against a local Ollama model. Returns a fallback tagged with the
    specific failure mode rather than switching to another backend."""
    model = settings.ollama_model
    url = settings.ollama_url.rstrip("/") + "/api/generate"
    payload = {
        "model": model,
        "system": SYSTEM_PROMPT,
        "prompt": raw_text[:2000] + hint,
        "stream": False,
        "format": "json",
        "options": {"temperature": 0, "num_predict": 250},
    }

    try:
        async with _OLLAMA_SEM:
            # The client — and so the timeout clock — is built AFTER the
            # semaphore is acquired, so a queued request does not spend its
            # budget waiting for the one GPU.
            async with httpx.AsyncClient(timeout=_OLLAMA_TIMEOUT) as client:
                response = await client.post(url, json=payload)
    except httpx.TimeoutException as e:
        # Caught before RequestError, which it subclasses. A daemon that took
        # the connection and then hung is not a daemon that is down, and an
        # archive filing both as "unreachable" cannot tell them apart later.
        logger.error("Ollama timed out after %ss at %s: %s", _OLLAMA_TIMEOUT, url, e)
        return _build_fallback(raw_text, "ollama_timeout")
    except httpx.RequestError as e:
        # Connection refused or DNS failure — nothing reached the model.
        logger.error("Ollama unreachable at %s: %s", url, e)
        return _build_fallback(raw_text, "ollama_unreachable")

    if response.status_code != 200:
        body = response.text[:300]
        if response.status_code == 404 and "not found" in body.lower():
            # A model name that was never pulled must not look like a parse error.
            logger.error("Ollama model '%s' not pulled: %s", model, body)
            return _build_fallback(raw_text, "ollama_model_missing")
        logger.error("Ollama HTTP %d: %s", response.status_code, body)
        return _build_fallback(raw_text, f"ollama_http_{response.status_code}")

    try:
        text = response.json()["response"]
        # Stripped INSIDE the guard: a non-string "response" makes re.sub raise
        # TypeError straight out of classify_message, and one raise costs the
        # rest of that feed cycle — the poller only guards per feed, and the
        # raising article is already marked seen, so it is never retried.
        text = _THINK_RE.sub("", text)
    except (ValueError, KeyError, TypeError) as e:
        logger.error("Ollama envelope unreadable: %s", e)
        return _build_fallback(raw_text, "parse_failed", model)

    try:
        return _handle_response(text, raw_text, flag_countries, model)
    except (json.JSONDecodeError, ValidationError) as e:
        logger.error("Ollama output not a valid classification: %s | got: %s", e, text[:300])
        return _build_fallback(raw_text, "parse_failed", model)


async def classify_message(raw_text: str) -> dict:
    """Classify a Telegram message. Returns structured dict with event_type,
    severity, location_name, summary."""

    # Normalised: a stray space or a capital in .env must not change which
    # backend runs.
    backend = settings.llm_backend.strip().lower()

    if backend == "none":
        return _build_fallback(raw_text, "no_backend")

    # Pre-extract flag countries to hint the model
    flag_countries = _extract_flags(raw_text)
    hint = ""
    if flag_countries:
        hint = f"\n\nFlag context: {', '.join(flag_countries)} are involved."

    if backend == "ollama":
        # No silent fall-through to Anthropic if Ollama is down: a backend
        # switch nobody recorded makes the archive uninterpretable later.
        return await _classify_ollama(raw_text, hint, flag_countries)

    if backend != "anthropic":
        # Anything unrecognised used to fall through to Anthropic, so a typo
        # ("olama", or a trailing space) silently sent traffic and spend to the
        # paid API — the same unrecorded backend switch the branch above
        # refuses to make when Ollama is down.
        logger.error(
            "Unknown llm_backend %r — refusing to guess a backend", settings.llm_backend
        )
        return _build_fallback(raw_text, "bad_backend")

    if not settings.anthropic_api_key:
        logger.warning("No Anthropic API key — using regex fallback")
        return _build_fallback(raw_text, "no_api_key")

    client = _get_client()
    status = "llm_failed"
    # Travels with the status so a parse_failed row names whose output failed to
    # parse. None for statuses where no output was ever produced, and re-set on
    # every attempt so a later failure cannot inherit an earlier attribution.
    status_model: str | None = None

    for attempt in range(3):
        try:
            response = await client.messages.create(
                model=ANTHROPIC_MODEL,
                max_tokens=200,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": raw_text[:2000] + hint}],
            )
            return _handle_response(
                response.content[0].text, raw_text, flag_countries, ANTHROPIC_MODEL
            )

        except anthropic.RateLimitError:
            status = "rate_limited"
            status_model = None
            wait = 2 ** (attempt + 1)
            logger.warning("Rate limited, retrying in %ds", wait)
            await asyncio.sleep(wait)
        except (json.JSONDecodeError, ValidationError) as e:
            status = "parse_failed"
            status_model = ANTHROPIC_MODEL
            logger.error("Classification error (attempt %d): %s", attempt + 1, e)
            if attempt == 2:
                break
        except anthropic.APIStatusError as e:
            status = f"api_{e.status_code}"
            status_model = None
            logger.error("Classification error (attempt %d): %s", attempt + 1, e)
            if attempt == 2:
                break
        except Exception as e:
            status = "llm_failed"
            status_model = None
            logger.error("Classification error (attempt %d): %s", attempt + 1, e)
            if attempt == 2:
                break

    return _build_fallback(raw_text, status, status_model)


def _build_fallback(raw_text: str, status: str, model: str | None = None) -> dict:
    """Build best-effort result without API — uses regex + flag extraction.

    model is the model id only when a model actually produced output we then
    failed to parse; it stays None when nothing ever reached a model.
    """
    flag_countries = _extract_flags(raw_text)
    regex_loc = _regex_location_fallback(raw_text)
    location = regex_loc or (flag_countries[0] if flag_countries else "Unknown")
    # No "severity" key at all. A fallback is a regex scrape of the raw text,
    # not a threat assessment — it has no severity to report, and writing 5
    # here is what put a median threat score on 86.3% of the archive. Callers
    # read this with .get("severity"), so the absent key surfaces as None.
    return {
        "event_type": "military",
        "location_name": location,
        "summary": raw_text[:200],
        "extraction_status": status,
        "extraction_model": model,
    }
