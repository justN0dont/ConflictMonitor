"""
News Feed Ingestion Service
============================
Polls multiple open-source RSS feeds from major news agencies covering the
Iran-Israel-US conflict. Runs the same classifier + geocoder pipeline as
Telegram to produce structured events in the DB.

Sources (no API keys required):
  TIER 4 — Wire services / rigorous verification:
  - Reuters World News            (cooperative model incentivises accuracy over speed)
  - BBC Middle East               (strong verification; UK-establishment framing on context)
  - AP News World                 (cooperative model, no shareholders = accuracy first)
  - The War Zone (twz.com)        (best English-language military analysis publication)

  TIER 3 — Useful, identifiable perspective, apply equal scrutiny to all:
  - Al Jazeera English            (Qatar state media; pro-Arab framing; factual on events)
  - Times of Israel               (Israeli perspective; subject to IDF military censorship)
  - Iran International (English)  (Saudi-funded, anti-Islamic Republic; mirrors ToI from other side)
  - RFI (France 24 Radio)         (French state radio; strong editorial independence in practice)
  - Defense One                   (Atlantic Media; US defense policy focus; no documented bias)

  EXCLUDED (documented reasons):
  - Middle East Eye               (Qatar-funded, documented Muslim Brotherhood editorial ties;
                                   functionally a perspective channel, not independent journalism)
  - IRNA / PressTV                (Iranian state broadcaster — official narrative, not reporting)

Articles are filtered by conflict-relevant keywords BEFORE sending to Claude,
so we don't waste API credits on unrelated news. Only severity >= 3 events
get saved (same threshold as Telegram ingestion).

Polling interval: every 5 minutes per feed.

NOTE ON BIAS (applies to ALL sources equally):
  Every source in this list has some perspective. Reuters is Western. Al Jazeera is
  Qatari. Iran International is Saudi-adjacent. Times of Israel is under IDF censorship.
  The classifier strips loaded language from summaries, and our dedup pipeline merges
  cross-source events so you can see how many independent outlets confirmed the same fact.
  No single source should be trusted alone — the cross-confirmation score is the signal.
"""

import asyncio
import hashlib
import logging
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree

import httpx
from geoalchemy2.shape import from_shape
from shapely.geometry import Point
from sqlalchemy import select

from app import feeds
from app.db import async_session
from app.feeds import ErrorKind
from app.models import Event, EventReport
from app.services.broadcaster import broadcaster
from app.schemas import EventRead, EventWS
from app.services.classifier import classify_message, evidence_span
from app.services.dedup import check_duplicate, merge_duplicate
from app.services.geocoder import geocode

logger = logging.getLogger(__name__)

# ── CONFIG ─────────────────────────────────────────────────────────────────────

POLL_INTERVAL = 300  # seconds between feed polls (5 min)
MIN_SEVERITY = 3

# There is no UNKNOWN_LAT/UNKNOWN_LON any more. An unresolvable location is
# written as NULL lat/lon/geometry with is_geolocated=False, rather than
# parked on a real point in the Indian Ocean.

# ── FEED REGISTRY ──────────────────────────────────────────────────────────────

FEEDS = [

    # ── TIER 4 — Wire services / rigorous independent verification ─────────────

    {
        "url": "https://feeds.reuters.com/reuters/worldNews",
        "source": "reuters",
        "reliability": 4,
        "bias_notes": (
            "Tier-1 wire cooperative. Accuracy-over-speed model. "
            "Western editorial perspective on geopolitical narratives; strong factual "
            "standards on events. Corrections published prominently."
        ),
    },
    {
        "url": "https://feeds.bbci.co.uk/news/world/middle_east/rss.xml",
        "source": "bbc_mideast",
        "reliability": 4,
        "bias_notes": (
            "UK public broadcaster. Strong editorial verification standards. "
            "Some UK-establishment framing on geopolitical context; factual on events. "
            "Covers the region continuously with dedicated correspondents."
        ),
    },
    {
        "url": "https://apnews.com/world-news.rss",
        "source": "ap_news",
        "reliability": 4,
        "bias_notes": (
            "Associated Press — cooperative model owned by its member news orgs. "
            "No shareholders = accuracy incentivised over clicks. One of the few "
            "wire services with no single geopolitical owner. Verification standards "
            "equivalent to Reuters. Apply same Western-framing caveat on context."
        ),
    },
    {
        "url": "https://www.twz.com/feed",
        "source": "the_war_zone",
        "reliability": 4,
        "bias_notes": (
            "The War Zone (twz.com) — best English-language military analysis "
            "publication. Deep technical sourcing on hardware, deployments, and "
            "doctrine. Founded by Tyler Rogoway; strong track record. "
            "US/Western focus but explicitly analytical rather than partisan. "
            "Good for military capability and equipment events specifically."
        ),
    },

    # ── TIER 3 — Useful with identifiable perspective — apply equal scrutiny ───
    # Each of these serves a different geopolitical lens. None is "better" than
    # the others from an accuracy standpoint. Use in combination; cross-confirmed
    # events that appear in MULTIPLE of these sources carry more weight.

    {
        "url": "https://www.aljazeera.com/xml/rss/all.xml",
        "source": "aljazeera",
        "reliability": 3,
        "bias_notes": (
            "Qatar state media. Qatar simultaneously hosts CENTCOM and Hamas political "
            "bureau — unique access, complex editorial interests. Pro-Arab/Palestinian "
            "framing on context; generally factual on events. Provides perspective "
            "largely absent from Western wire services."
        ),
    },
    {
        "url": "https://www.timesofisrael.com/feed/",
        "source": "times_of_israel",
        "reliability": 3,
        "bias_notes": (
            "Israeli perspective. Good primary Israeli source attribution. "
            "CRITICAL CAVEAT: Israel's IDF Military Censor actively restricts "
            "reporting on strikes ON Israeli territory (CPJ + RSF documented "
            "March 2026). Events favourable to Iranian strikes may be delayed, "
            "minimised or absent from Israeli media by law. Apply extra scepticism "
            "to any absence of coverage of Israeli-territory events."
        ),
    },
    {
        "url": "https://www.iranintl.com/en/rss",
        "source": "iran_international",
        "reliability": 3,
        "bias_notes": (
            "UK-based English satellite channel. CPJ research documents "
            "Saudi-adjacent funding and editorially opposes the Islamic Republic. "
            "Functionally a mirror image of PressTV from the opposite direction — "
            "will amplify Iranian military failures, downplay Iranian government "
            "context. Facts on Iranian military actions are usually accurate; "
            "framing is one-sided. Use alongside Al Jazeera to triangulate."
        ),
    },
    {
        "url": "https://www.rfi.fr/en/rss",
        "source": "rfi",
        "reliability": 3,
        "bias_notes": (
            "Radio France Internationale — French state radio with strong "
            "editorial independence enforced by charter. Good Levant, Africa, "
            "and Gulf coverage. France's independent diplomatic position "
            "(not a NATO full member until 2009) gives somewhat different "
            "Middle East framing than UK/US outlets."
        ),
    },
    {
        "url": "https://www.defenseone.com/rss/all/",
        "source": "defense_one",
        "reliability": 3,
        "bias_notes": (
            "Atlantic Media defense policy publication. US national security "
            "focus — procurement, doctrine, strategy, and escalation analysis. "
            "No documented political bias beyond general US-policy framing. "
            "Better than general news for military capability/decision context. "
            "Not a primary event-reporting source but strong on escalation signals."
        ),
    },

    # ── EXCLUDED SOURCES (documented rationale) ─────────────────────────────────
    # Middle East Eye — removed from active ingestion.
    #   UK-registered outlet with documented editorial and financial ties to Qatar
    #   and Muslim Brotherhood-aligned networks (per Middle East Forum and CPJ).
    #   Not editorially independent journalism; functions as a perspective channel.
    #   If added back, treat as reliability=2 (perspective-window, not reporting).
    #
    # IRNA / PressTV — Iranian state broadcasters.
    #   Official Islamic Republic narrative. Factnameh study documents >95%
    #   word-similarity with IRGC channel messaging = coordinated state narrative.
    #   Same standard applied here as to any other government mouthpiece.
]

# ── KEYWORD FILTER ─────────────────────────────────────────────────────────────
# Articles must contain at least one of these terms to be sent to the classifier.
# This avoids burning API credits on unrelated news.

CONFLICT_KEYWORDS = [
    # Parties
    "iran", "israel", "irgc", "idf", "mossad", "hezbollah", "hamas",
    "houthi", "houthis", "ansar allah", "islamic revolutionary",
    # Conflict terms
    "missile", "strike", "attack", "bomb", "explosion", "airstrike", "air strike",
    "drone", "ballistic", "cruise missile", "rocket", "intercept", "iron dome",
    "kill", "killed", "dead", "casualties", "wounded", "destroyed",
    # Locations
    "tehran", "tel aviv", "haifa", "beirut", "damascus", "baghdad", "sanaa",
    "hormuz", "red sea", "persian gulf", "natanz", "bushehr", "dimona",
    "gaza", "west bank", "golan", "rafah",
    # Nuclear
    "nuclear", "enrichment", "uranium", "centrifuge", "warhead",
    # Military/escalation
    "carrier", "warship", "navy", "troops", "military", "escalation",
    "retaliation", "ceasefire", "war", "combat",
    # US involvement
    "pentagon", "centcom", "camp david",
]

def _is_conflict_relevant(title: str, description: str) -> bool:
    """Return True if this article is about the Iran-Israel-US conflict."""
    combined = f"{title} {description}".lower()
    return any(kw in combined for kw in CONFLICT_KEYWORDS)


# ── SEEN ARTICLE CACHE ─────────────────────────────────────────────────────────
# In-memory set of URL hashes to avoid re-processing on each poll.
# Cleared on restart (acceptable — dedup check in DB handles that).

_seen_hashes: set[str] = set()
_MAX_SEEN = 5000  # prevent unbounded growth


def _article_hash(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:16]


def _already_seen(url: str) -> bool:
    h = _article_hash(url)
    if h in _seen_hashes:
        return True
    _seen_hashes.add(h)
    if len(_seen_hashes) > _MAX_SEEN:
        # Remove oldest ~500 entries (simple set doesn't track order, just clear half)
        to_remove = list(_seen_hashes)[:500]
        for item in to_remove:
            _seen_hashes.discard(item)
    return False


# ── RSS PARSER ─────────────────────────────────────────────────────────────────

def _parse_rss(xml_text: str) -> list[dict] | None:
    """Parse RSS/Atom feed XML and return list of article dicts.

    None means the reply was not a feed at all (malformed XML, or an HTML
    error page); [] means a well-formed feed with no items. Each article's
    `dated` says whether its time came from the feed or is receipt time.
    """
    articles = []
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError as e:
        logger.error("RSS parse error: %s", e)
        return None

    # Handle both RSS 2.0 (channel/item) and Atom (feed/entry)
    ns = {"atom": "http://www.w3.org/2005/Atom"}

    # RSS 2.0
    channel = root.find("channel")
    if channel is not None:
        for item in channel.findall("item"):
            title = (item.findtext("title") or "").strip()
            desc = (item.findtext("description") or "").strip()
            url = (item.findtext("link") or "").strip()
            pub_date_str = item.findtext("pubDate")
            dated = False
            try:
                pub_date = parsedate_to_datetime(pub_date_str) if pub_date_str else None
                dated = pub_date is not None
            except Exception:
                pub_date = None
            if pub_date is None:
                pub_date = datetime.now(timezone.utc)
            elif pub_date.tzinfo is None:
                pub_date = pub_date.replace(tzinfo=timezone.utc)
            if url:
                articles.append({"title": title, "description": desc, "url": url,
                                 "published": pub_date, "dated": dated})
        return articles

    if root.tag != "{http://www.w3.org/2005/Atom}feed":
        return None

    # Atom
    for entry in root.findall("atom:entry", ns):
        title = (entry.findtext("atom:title", namespaces=ns) or "").strip()
        summary = (entry.findtext("atom:summary", namespaces=ns) or "").strip()
        link_el = entry.find("atom:link", ns)
        url = (link_el.get("href") if link_el is not None else "") or ""
        published_str = entry.findtext("atom:published", namespaces=ns) or entry.findtext("atom:updated", namespaces=ns)
        dated = False
        try:
            pub_date = datetime.fromisoformat(published_str.replace("Z", "+00:00")) if published_str else None
            dated = pub_date is not None
        except Exception:
            pub_date = None
        if pub_date is None:
            pub_date = datetime.now(timezone.utc)
        elif pub_date.tzinfo is None:
            pub_date = pub_date.replace(tzinfo=timezone.utc)
        if url:
            articles.append({"title": title, "description": summary, "url": url,
                             "published": pub_date, "dated": dated})

    return articles


# ── ARTICLE PROCESSOR ──────────────────────────────────────────────────────────

async def _process_article(
    title: str,
    description: str,
    url: str,
    published: datetime,
    source_name: str,
    reliability: int,
):
    """Run one article through classify → geocode → save pipeline."""
    # Build the text we'll classify (title + description)
    full_text = f"{title}\n\n{description}".strip()
    if len(full_text) < 20:
        return

    # Already seen this URL? Then this article has been all the way through the
    # pipeline before — a restart empties _seen_hashes, so every stored URL
    # comes round again. Checked BEFORE classifying, because the old order
    # classified first and threw the result away further down: one GPU
    # inference per stored article per restart (~105 measured), and worse, the
    # re-seen article reached the semantic-duplicate check first and could merge
    # into some other row, inflating its report_count for an article already in
    # the table.
    #
    # That last sentence was a prediction when it was written, and the archive
    # confirms it: 17,450 of the 19,027 reports merge destroyed are RSS, and
    # the tail is 214 / 167 / 130 report_counts on single articles. It survived
    # the first fix because the guard read `events.source_url`, and a MERGED
    # article writes no events row — so exactly the case the comment describes
    # was the one case the query could not see. Reading event_reports closes it:
    # there is a row per report now, merged ones included.
    async with async_session() as session:
        stored = await session.execute(
            select(EventReport.id).where(EventReport.source_url == url).limit(1)
        )
        if stored.scalar_one_or_none() is not None:
            return

    # Classify. severity may be None: the classifier no longer invents one.
    result = await classify_message(full_text)
    severity = result.get("severity")

    # Classifier flagged this as noise/commentary
    if result.get("is_noise"):
        logger.debug("RSS: noise/commentary dropped: %s", title[:60])
        return

    # An unmeasured severity is not a low severity. Dropping on None would
    # silently discard an otherwise-fine article for a number we never got.
    if severity is not None and severity < MIN_SEVERITY:
        logger.debug("RSS: low severity (%s) — skipping: %s", severity, title[:60])
        return

    # Geocode
    location_name = result.get("location_name", "Unknown")
    geo = await geocode(location_name)
    if geo:
        lat, lon = geo.lat, geo.lon
        geometry = from_shape(Point(lon, lat), srid=4326)
        is_geolocated = True
    else:
        # Unresolvable. Write NULL, not a coordinate.
        lat = lon = geometry = None
        is_geolocated = False

    # Built before the duplicate check, because it is a fact about THIS article
    # whichever event it turns out to belong to.
    report = EventReport(
        source=f"rss_{source_name}",
        channel=source_name,
        raw_text=full_text,
        summary=result.get("summary", full_text[:200]),
        source_url=url,
        telegram_message_id=None,
        killed_reported=result.get("killed_reported"),
        # Beside the count, from the same result, so a NULL count on this row
        # can be read as "the article stated none" or "the classifier never
        # produced one" — see models.py.
        extraction_status=result.get("extraction_status"),
        reported_at=published,
    )

    async with async_session() as session:
        # Check for semantic duplicates
        existing = await check_duplicate(
            session,
            result.get("summary", ""),
            result.get("event_type", "military"),
            lat, lon,
            published,
        )
        if existing:
            # Only a row whose own classification succeeded may take a severity
            # from another report. Otherwise the surviving row reads
            # "extraction_status=api_400, extraction_model=NULL" while carrying
            # a qwen3 number — a measurement its own provenance says nothing
            # produced. Those rows are healed by re-classifying them, not by
            # having a number quietly appear on them.
            if severity is not None and existing.extraction_status != "ok":
                logger.info(
                    "Not raising severity of #%s (extraction_status=%s) from a %s report",
                    existing.id, existing.extraction_status,
                    result.get("extraction_status"),
                )
                severity = None
            # The whole article is kept: merge_duplicate stores it as a report
            # row on this event, in the same transaction as the counter it
            # bumps. The incoming killed_reported rides on that row, beside the
            # article text that stated it; existing.killed_reported is still
            # never written.
            await merge_duplicate(session, existing, report, severity)
            ws_payload = EventWS(type="new_event", event=EventRead.model_validate(existing))
            await broadcaster.broadcast(ws_payload.model_dump_json())
            return

        db_event = Event(
            source=f"rss_{source_name}",
            channel_name=source_name,
            raw_text=full_text,
            summary=result.get("summary", full_text[:200]),
            event_type=result.get("event_type", "military"),
            severity=severity,
            killed_reported=result.get("killed_reported"),
            lat=lat,
            lon=lon,
            geometry=geometry,
            timestamp=published,
            source_reliability=reliability,
            location_name=location_name,
            telegram_message_id=None,
            source_url=url,
            reporting_channels=source_name,
            extraction_status=result.get("extraction_status"),
            extraction_model=result.get("extraction_model"),
            is_geolocated=is_geolocated,
            geo_precision=geo.precision if geo else None,
            geo_uncertainty_m=geo.uncertainty_m if geo else None,
            geo_method=geo.method if geo else None,
            # From this row's own text. `full_text` is what was classified and
            # is what raw_text stores above, so the span is a slice of the
            # column it sits beside.
            evidence_span=evidence_span(full_text, location_name),
        )
        session.add(db_event)
        # flush, not commit: db_event.id does not exist until the INSERT runs,
        # and the report needs it. One commit still covers both, so an event is
        # never on disk without its first report.
        await session.flush()
        report.event_id = db_event.id
        session.add(report)
        await session.commit()
        await session.refresh(db_event)

        ws_payload = EventWS(type="new_event", event=EventRead.model_validate(db_event))
        await broadcaster.broadcast(ws_payload.model_dump_json())
        logger.info(
            "RSS event #%d | sev=%s | loc='%s' -> %s | geo=%s | %s",
            db_event.id, severity, location_name,
            f"({lat:.2f},{lon:.2f}) {geo.precision} ±{geo.uncertainty_m}m" if geo else "NULL",
            is_geolocated, url[:80],
        )


# ── FEED POLLER ────────────────────────────────────────────────────────────────

async def _fetch_feed(feed: dict, http_client) -> tuple[list[dict] | None, ErrorKind, str | None]:
    """Fetch and parse one RSS feed. Returns (articles or None, outcome, detail).

    A well-formed feed with no items is EMPTY, a success; a 200 that is not a
    feed is PARSE_ERROR, never "no articles".
    """
    source = feed["source"]
    try:
        resp = await http_client.get(feed["url"], timeout=15, follow_redirects=True)
    except httpx.TimeoutException as e:
        logger.warning("RSS fetch timed out for %s: %s", source, e)
        return None, ErrorKind.TIMEOUT, type(e).__name__
    except Exception as e:
        logger.warning("RSS fetch failed for %s: %s", source, e)
        return None, ErrorKind.FETCH_ERROR, f"{type(e).__name__}: {e}"
    if resp.status_code != 200:
        logger.warning("RSS %s returned HTTP %d", source, resp.status_code)
        kind = ErrorKind.RATE_LIMITED if resp.status_code == 429 else ErrorKind.HTTP_ERROR
        return None, kind, f"HTTP {resp.status_code}"
    articles = _parse_rss(resp.text)
    if articles is None:
        return None, ErrorKind.PARSE_ERROR, "reply is not an RSS or Atom feed"
    if not articles:
        logger.debug("RSS %s: no articles parsed", source)
        return articles, ErrorKind.EMPTY, None
    return articles, ErrorKind.OK, None


def report_cycle(now: float, outcomes: dict[str, tuple[list[dict] | None, ErrorKind, str | None]]) -> None:
    """One fetch pass over every feed to the "rss" tracker. Any feed answering
    is a success - there is no partial state - and `parts` names each feed's
    outcome so a dead feed URL stays visible behind a live roll-up."""
    tracker = feeds.tracker("rss")
    parts = {src: (kind.value if detail is None else f"{kind.value}: {detail}")
             for src, (_, kind, detail) in outcomes.items()}
    answered = [arts for arts, kind, _ in outcomes.values() if kind in feeds.SUCCESS_KINDS]
    if answered:
        epochs = [a["published"].timestamp() for arts in answered for a in arts if a.get("dated")]
        tracker.succeeded(now, count=len(answered), source="rss",
                          source_epoch=max(epochs) if epochs else None,
                          kind=ErrorKind.OK if any(answered) else ErrorKind.EMPTY,
                          parts=parts)
        return
    kinds = [kind for _, kind, _ in outcomes.values()]
    # All failed: name the most common way they failed.
    kind = max(sorted(set(kinds)), key=kinds.count) if kinds else ErrorKind.FETCH_ERROR
    failed = "; ".join(f"{src} {p}" for src, p in parts.items())
    tracker.failed(now, kind, f"all {len(outcomes)} feeds failed: {failed}", parts=parts)


async def _process_articles(feed: dict, articles: list[dict]):
    """Filter and classify one feed's articles."""
    source = feed["source"]
    reliability = feed["reliability"]
    new_count = 0
    for article in articles:
        art_url = article["url"]
        if not art_url or _already_seen(art_url):
            continue
        if not _is_conflict_relevant(article["title"], article["description"]):
            continue
        await _process_article(
            title=article["title"],
            description=article["description"],
            url=art_url,
            published=article["published"],
            source_name=source,
            reliability=reliability,
        )
        new_count += 1
        # Small delay between classifier calls to avoid rate limits
        await asyncio.sleep(0.5)

    if new_count:
        logger.info("RSS %s: processed %d relevant articles", source, new_count)


# ── BACKGROUND TASK ENTRYPOINT ─────────────────────────────────────────────────

async def start_news_feed_poller(sleep=asyncio.sleep, clock=time.time, client=None):
    """Continuously poll all RSS feeds. Runs forever as a background task.

    Each cycle fetches every feed first, reports that pass to the "rss"
    tracker, then classifies: the feed's health is whether the outlets
    answered, not how long the classifier took over what they said.
    """
    logger.info("News feed poller starting — monitoring %d feeds", len(FEEDS))

    headers = {
        "User-Agent": "ConflictMonitor/1.0 (research; contact: admin@localhost)",
        "Accept": "application/rss+xml, application/xml, text/xml, */*",
    }

    async with (client or httpx.AsyncClient(headers=headers)) as http_client:
        while True:
            outcomes = {}
            for feed in FEEDS:
                try:
                    outcomes[feed["source"]] = await _fetch_feed(feed, http_client)
                except Exception as e:
                    logger.exception("Unhandled error fetching %s: %s", feed["source"], e)
                    outcomes[feed["source"]] = (None, ErrorKind.FETCH_ERROR, f"{type(e).__name__}: {e}")
                # Small gap between feeds
                await sleep(2)
            report_cycle(clock(), outcomes)

            for feed in FEEDS:
                articles = outcomes[feed["source"]][0]
                if not articles:
                    continue
                try:
                    await _process_articles(feed, articles)
                except Exception as e:
                    logger.exception("Unhandled error processing %s: %s", feed["source"], e)

            logger.debug("News feed poll cycle complete — sleeping %ds", POLL_INTERVAL)
            await sleep(POLL_INTERVAL)
