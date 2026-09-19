import asyncio
import json
import logging
import re

import anthropic
from pydantic import BaseModel, Field, field_validator

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

Respond ONLY with valid JSON. No markdown, no explanation.
Example: {"event_type":"military","severity":7,"location_name":"Natanz","summary":"Israeli airstrikes targeted centrifuge halls at the Natanz enrichment facility."}"""


class ClassifierResult(BaseModel):
    event_type: str = "military"
    severity: int = Field(default=5, ge=1, le=10)
    location_name: str = "Unknown"
    summary: str = ""
    is_noise: bool = False   # set by post-init logic, not from Claude output

    @field_validator("event_type")
    @classmethod
    def validate_event_type(cls, v):
        allowed = {"military", "diplomatic", "economic", "humanitarian", "cyber"}
        return v if v in allowed else "military"

    @field_validator("severity")
    @classmethod
    def clamp_severity(cls, v):
        return max(1, min(10, v))

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


def _regex_location_fallback(text: str) -> str | None:
    """Find the most specific (longest) location keyword in the text."""
    text_lower = text.lower()
    best: str | None = None
    best_len = 0
    for loc in LOCATION_KEYWORDS:
        loc_lower = loc.lower()
        if loc_lower in text_lower and len(loc) > best_len:
            best = loc
            best_len = len(loc)
    return best


_client: anthropic.AsyncAnthropic | None = None


def _get_client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    return _client


async def classify_message(raw_text: str) -> dict:
    """Classify a Telegram message. Returns structured dict with event_type,
    severity, location_name, summary."""

    if not settings.anthropic_api_key:
        logger.warning("No Anthropic API key — using regex fallback")
        return _build_fallback(raw_text)

    # Pre-extract flag countries to hint Claude
    flag_countries = _extract_flags(raw_text)
    hint = ""
    if flag_countries:
        hint = f"\n\nFlag context: {', '.join(flag_countries)} are involved."

    client = _get_client()

    for attempt in range(3):
        try:
            response = await client.messages.create(
                model="claude-haiku-4-5-20251001",  # Haiku: faster + cheaper for classification
                max_tokens=200,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": raw_text[:2000] + hint}],
            )
            text = response.content[0].text.strip()
            # Strip any accidental markdown fences
            text = re.sub(r"^```json\s*|```$", "", text, flags=re.MULTILINE).strip()
            raw = json.loads(text)
            result = ClassifierResult(**raw)
            classified = result.model_dump()

            # Claude signals noise/commentary by returning summary="[NOISE]" or severity=1
            # Mark it so callers can fast-drop without geocoding
            if classified["summary"] == "[NOISE]" or classified["severity"] <= 1:
                classified["is_noise"] = True
                logger.debug("Classifier: noise/commentary dropped: %s", raw_text[:80])
                return classified

            # If Claude returned Unknown, try regex fallback before giving up
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
            logger.info(
                "Classified: type=%s sev=%d loc='%s'",
                classified["event_type"],
                classified["severity"],
                classified["location_name"],
            )
            return classified

        except anthropic.RateLimitError:
            wait = 2 ** (attempt + 1)
            logger.warning("Rate limited, retrying in %ds", wait)
            await asyncio.sleep(wait)
        except (json.JSONDecodeError, Exception) as e:
            logger.error("Classification error (attempt %d): %s", attempt + 1, e)
            if attempt == 2:
                break

    return _build_fallback(raw_text)


def _build_fallback(raw_text: str) -> dict:
    """Build best-effort result without API — uses regex + flag extraction."""
    flag_countries = _extract_flags(raw_text)
    regex_loc = _regex_location_fallback(raw_text)
    location = regex_loc or (flag_countries[0] if flag_countries else "Unknown")
    return {
        "event_type": "military",
        "severity": 5,
        "location_name": location,
        "summary": raw_text[:200],
    }
