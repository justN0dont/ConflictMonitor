"""Event deduplication — prevents duplicate markers when multiple channels report the same event.

Algorithm:
  1. Query PostGIS for events within ±15 minutes with matching event_type
  2. Located incoming event  -> candidates must be located AND within 50km
     Unlocated incoming event -> candidates must also be unlocated
     (never "no spatial constraint" — see the C8 comment in check_duplicate)
  3. Compute Jaccard similarity on summary word sets
  4. If similarity > 0.4, it's a duplicate — link instead of creating new: the
     incoming report is stored whole as an EventReport row on the existing
     event, and the event row itself takes report_count, channels, severity
     and reliability. events.killed_reported is still never written here; the
     incoming count rides on the report row, beside the text that stated it.
"""

import logging
import re
from datetime import timedelta

from sqlalchemy import and_, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from geoalchemy2 import Geography
from geoalchemy2.functions import ST_DWithin
from geoalchemy2.shape import from_shape
from shapely.geometry import Point

from app.models import Event, EventReport
from app.seed_channels import get_reliability as _channel_reliability

logger = logging.getLogger(__name__)


def _normalize(text: str) -> set[str]:
    """Lowercase, strip punctuation, return word set."""
    words = re.sub(r"[^\w\s]", "", text.lower()).split()
    # Remove very short words
    return {w for w in words if len(w) > 2}


# How many rows the text comparison may look at, per branch.
#
# These are not the same number because the two branches arrive with different
# amounts of narrowing already done. A located event has been cut down by the
# 50km ST_DWithin before the cap is reached, so 20 is a generous safety net on
# an already-small pool. An unlocated event has had NO spatial narrowing at
# all — the time window and the event type are the whole filter — so the same
# cap would be the only thing standing between it and every unlocated row in
# the window. Measured on this archive, the densest ±15min same-event_type
# window holds 234 rows; at that density a cap of 20 sees the newest 8.5% and
# a genuine duplicate sitting just outside it is silently re-created.
#
# 200 covers the observed peak. It is still a cap, not a guarantee: a window
# denser than this can still hide a duplicate past the cut, and that is a
# bound on work, not a claim that no duplicate exists.
_LOCATED_CANDIDATES = 20
_UNLOCATED_CANDIDATES = 200


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    intersection = len(a & b)
    union = len(a | b)
    return intersection / union if union > 0 else 0.0


async def check_duplicate(
    session: AsyncSession,
    summary: str,
    event_type: str,
    lat: float | None,
    lon: float | None,
    timestamp,
) -> Event | None:
    """Check if a similar event already exists. Returns the existing Event if duplicate, None if new."""
    time_window = timedelta(minutes=15)
    ts_min = timestamp - time_window
    ts_max = timestamp + time_window

    # Base query: same event type, within time window
    stmt = select(Event).where(
        and_(
            Event.event_type == event_type,
            Event.timestamp >= ts_min,
            Event.timestamp <= ts_max,
        )
    )

    # ── Geometry guard (C8) ────────────────────────────────────────────────────
    # Unlocated rows now carry NULL geometry instead of the old Indian Ocean
    # sentinel. That is the right storage, but it removes the accidental
    # protection the sentinel gave this query: every unlocated row used to sit
    # on one point, so the 50km spatial filter still partitioned located from
    # unlocated traffic. With NULL geometry the spatial filter simply does not
    # apply, and an unlocated event would match on time-window + event_type
    # alone — "military, within ±15 minutes" against the entire table. Both
    # branches below must therefore be explicit about geometry.
    if lat is not None and lon is not None:
        new_geom = from_shape(Point(lon, lat), srid=4326)
        # A located event may only match a located row. ST_DWithin against a
        # NULL geometry yields NULL rather than TRUE, so this is belt and
        # braces — but stating it keeps the rule visible and survives any
        # future change to the spatial predicate.
        stmt = stmt.where(
            Event.geometry.isnot(None),
            # Cast to geography for meter-based distance (50km)
            ST_DWithin(
                cast(Event.geometry, Geography),
                cast(new_geom, Geography),
                50000,
            ),
        )
        candidate_limit = _LOCATED_CANDIDATES
    else:
        # No coordinates on the incoming event. Do NOT fall through with no
        # spatial constraint: restrict the pool to rows that are also
        # unlocated, so the unlocated pool dedups against itself on text
        # similarity alone. Two consequences, both deliberate:
        #   - a located event can never be absorbed into an event we could not
        #     place, which would destroy a known coordinate,
        #   - an unlocated event is never merged into a located one on text
        #     alone; it stays a separate, honestly unlocated row until
        #     something geocodes it.
        stmt = stmt.where(Event.geometry.is_(None))
        candidate_limit = _UNLOCATED_CANDIDATES

    # Deterministic order: without it Postgres may hand back any candidates,
    # so the real duplicate can fall outside the window and dedup stops being
    # reproducible (and therefore stops being measurable).
    stmt = stmt.order_by(Event.timestamp.desc(), Event.id.desc())

    result = await session.execute(stmt.limit(candidate_limit))
    candidates = result.scalars().all()

    if not candidates:
        return None

    new_words = _normalize(summary)

    for candidate in candidates:
        candidate_words = _normalize(candidate.summary)
        similarity = _jaccard(new_words, candidate_words)
        if similarity > 0.4:
            logger.info(
                "Duplicate detected: existing #%d (sim=%.2f) '%s' ≈ '%s'",
                candidate.id, similarity,
                candidate.summary[:50], summary[:50],
            )
            return candidate

    return None


async def merge_duplicate(
    session: AsyncSession,
    existing: Event,
    report: EventReport,
    new_severity: int | None,
):
    """Link a duplicate report onto an existing event, keeping the report whole.

    `report` is the incoming report, already built by the caller from the
    message it arrived in and not yet attached to anything. It is stored here,
    inside the same transaction as the counter it increments: a report row
    added by the caller after this function returned would commit separately,
    and a crash in between would leave report_count claiming a report with no
    row under it — a corroboration claim with no evidence, which is the exact
    thing this change exists to stop.
    """
    report.event_id = existing.id
    session.add(report)
    await session.flush()

    # Increment report count
    new_count = (existing.report_count or 1) + 1
    existing.report_count = new_count

    # Append channel
    channels = existing.reporting_channels or existing.channel_name
    if report.channel and report.channel not in channels:
        channels = f"{channels}, {report.channel}"
    existing.reporting_channels = channels

    # Take higher severity. None is "not measured", which is neither higher
    # nor lower than a number: an unmeasured report must not overwrite a
    # measured one, and it must not be compared as 0 or as 10. It can only
    # fill a slot that is itself still empty (i.e. it cannot — nothing to do).
    if new_severity is not None and (
        existing.severity is None or new_severity > existing.severity
    ):
        existing.severity = new_severity

    # existing.killed_reported is NOT written here, at any similarity, on
    # either branch. The incoming count is not dropped any more — it is on the
    # report row added at the top of this function, beside the text that
    # stated it — but it does not move onto the event.
    #
    # This is not a policy choice about which of two counts is better — the
    # column's contract forecloses the question. models.py:43: "People the
    # classified message SAID were killed, copied from its text ... a quantity
    # a reader can check against raw_text in one second." The text a merged
    # count was copied from is the OTHER report's, and this row's raw_text is
    # not it. So a filled count lands on a row whose own text does not contain
    # it, and the one-second check that is this column's entire justification
    # over severity comes back NEGATIVE on a row that is not wrong. A reader
    # running it cannot tell a merged 17 from a hallucinated 17;
    # reporting_channels names the channels but cannot say which of them gave
    # the number. Worse, the check is not currently reachable at all from the
    # UI that prints the number: raw_text appears nowhere in frontend/src
    # outside the type declaration, so LiveFeed renders "17 KILLED" under a
    # tooltip reading "copied from its text" with the text nowhere on screen.
    #
    # All of that is true of a CORRECT merge. C74's false positive — sim=0.44
    # against a > 0.4 bar, and no distance test of any kind on the unlocated
    # branch — is the loud case, not the reason. A contract broken on the good
    # path is not a threshold problem, and raising the bar would make it break
    # less often rather than hold: the score is Jaccard over `summary`, the
    # LLM's paraphrase, so it measures how similarly the model worded two
    # things. That is not evidence about whose text a number was copied from,
    # at 0.4 or at 0.8.
    #
    # What has changed since that was first written is only where the count
    # goes instead. It used to be logged and dropped — and a log line is not a
    # row: it is not queryable, it rotates, and nothing in the product could
    # surface it. It is now `report.killed_reported`, on the row inserted at
    # the top of this function, in the same tuple as the raw_text it was copied
    # from. The contract does not weaken in the move, it gets stricter: the
    # one-second check stops being merely true and becomes local. One integer
    # on one event row cannot say "A stated nothing, B stated 17", or "A said 3
    # and B said 17"; two report rows say exactly that, each answerable from
    # its own text.
    #
    # So killed_reported still has exactly one kind of writer on the EVENT
    # row: the classifier, on that row's own raw_text — at insert
    # (telegram.py, news_feeds.py) and on re-classification (events.py, both
    # tasks writing whatever this row's own text produces, None included).
    # `existing.killed_reported` appears nowhere on the left of an assignment
    # in this file, and that invariant is still checkable with one grep.

    # There is deliberately NO coordinate backfill here any more.
    #
    # It used to read "if existing.lat is None and new_lat is not None: ...".
    # The geometry guard in check_duplicate makes that unreachable: a located
    # incoming event only ever matches rows with geometry IS NOT NULL (so
    # existing.lat is never None), and an unlocated one always arrives with
    # new_lat None. Keeping it would have been dead code carrying a comment
    # that implied it worked — and it was unsafe if ever revived, because it
    # wrote lat/lon while leaving geometry, is_geolocated, geo_precision,
    # geo_uncertainty_m and geo_method untouched: a located row claiming
    # nothing about how it was located, and a lat-without-geometry row that
    # breaks the "geometry IS NULL <=> lat IS NULL" partition check_duplicate
    # now relies on.
    #
    # The consequence is real and accepted: the same event reported twice,
    # once geocodable and once not, stays two rows, and the unlocated one is
    # rescued only by _fix_null_coords_task re-geocoding its own
    # location_name. That is the honest outcome — the alternative is matching
    # a located event against the entire unlocated pool on text alone, which
    # is exactly what the guard exists to prevent. Restoring enrichment needs
    # a second, separately-bounded pass, not a resurrected branch here.

    # ── Multi-source confidence boost ──────────────────────────────────────────
    # When multiple independent channels confirm the same event, raise reliability.
    # Base: take the higher of the two channels' scores.
    # Bonus: +1 if 3+ DISTINCT channels have reported it (capped at 5).
    #
    # The bonus used to gate on `new_count`, which counts merge EVENTS (`C11`).
    # The pipeline generates merge events by itself — a restart re-ingests a
    # stored RSS article and merges it again — so three self-merges of one
    # article raised confidence for one source repeating itself. Counting the
    # distinct channels on the report rows is the fix, and it is only possible
    # now that there are report rows to count.
    #
    # Measured on the archive, this is not a corner case. The OLD gate was
    # `new_count >= 3`, so the rows that ever fired it are the 2,794 with
    # report_count >= 3 — NOT all 6,848 merged rows, since a row that stopped
    # at 2 never boosted at all. Of those 2,794, only 138 (4.9%) name three or
    # more distinct channels, so 2,656 boosts — 95.1% of every boost this
    # system has awarded — would not qualify under this gate. (Across all 6,848
    # merged rows, 5,174 (75.6%) name a single channel and 141 name three or
    # more; three of those 141 never reached report_count 3.)
    #
    # The count is a FLOOR, bounded by what this table holds, and on
    # pre-cutover rows a low one. Every event ingested before the table existed
    # has exactly ONE report row — the startup backfill's — however many
    # channels reported it, so an archive row whose `reporting_channels` names
    # four counts as one channel here. That is not only a statement about old
    # scores: it suppresses FUTURE boosts on those rows too, because the gate
    # needs three distinct channels among rows in THIS table. A pre-cutover row
    # starts with one report row however many channels reported it, so it needs
    # two further reports from two genuinely new channels before it can boost,
    # where a post-cutover row needs the same three counted from scratch.
    #
    # It does NOT have to wait for `report_count - count(reports)` to reach 0,
    # and saying so would be doubly wrong: that difference is INVARIANT after
    # cutover — every later merge increments both sides — so it never comes
    # down, and reaching the gate does not depend on it. The difference is not
    # a countdown. It is the count of reports whose text was destroyed before
    # this table existed, and it stays fixed as a permanent marker of how much
    # of that row's corroboration cannot be re-read. The 138 archive rows that
    # genuinely name three or more channels lose a boost they should keep. This is a different claim from "scores either side of this
    # commit are not comparable" (`C11` in FINDINGS), and both are true.
    #
    # Taking the higher of this count and the channels named in
    # `reporting_channels` would restore those 141, and is refused for the same
    # reason this commit refuses to recompute history from that column:
    # `reporting_channels` is a display string whose append is a substring test
    # (`C78`), so it silently omits a distinct channel whose name is contained
    # in one already listed. Deriving a corroboration claim from a string this
    # tree documents as under-counted is fabrication with extra steps. An
    # under-claim a reader can see stated is the honest failure of the two.
    #
    # The BASE stays max(current, incoming) and is deliberately not recomputed
    # from the report rows. _channel_reliability is seed_channels.get_reliability,
    # which knows the Telegram registry only and returns None — hence 1 — for
    # every RSS source name, so recomputing would collapse a feed that
    # news_feeds scored 4 at insert down to 1 on its first merge.
    #
    # Empty channel names are excluded from the count: telegram.py writes ""
    # when a chat has neither username nor title, and two unnamed channels
    # cannot be shown to be distinct from each other.
    distinct_channels = (
        await session.execute(
            select(func.count(func.distinct(EventReport.channel))).where(
                EventReport.event_id == existing.id,
                EventReport.channel != "",
            )
        )
    ).scalar_one()

    new_channel_rel = _channel_reliability(report.channel) or 1
    current_rel = existing.source_reliability or 1
    combined = max(current_rel, new_channel_rel)
    if distinct_channels >= 3:
        combined = min(5, combined + 1)
    if combined != existing.source_reliability:
        existing.source_reliability = combined
        logger.debug(
            "Reliability boosted to %d for event #%d (%d distinct channels)",
            combined, existing.id, distinct_channels,
        )

    await session.commit()
    await session.refresh(existing)
    logger.info(
        "Merged into event #%d (report #%d kept | now %d reports, %d distinct "
        "channel(s) | reliability=%d | sources: %s)",
        existing.id, report.id, new_count, distinct_channels,
        existing.source_reliability or 0, existing.reporting_channels,
    )
