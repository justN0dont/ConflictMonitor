"""Event deduplication — prevents duplicate markers when multiple channels report the same event.

Algorithm:
  1. Query PostGIS for events within ±15 minutes with matching event_type
  2. Located incoming event  -> candidates must be located AND within 50km
     Unlocated incoming event -> candidates must also be unlocated
     (never "no spatial constraint" — see the C8 comment in check_duplicate)
  3. Compute Jaccard similarity on summary word sets
  4. If similarity > 0.4, it's a duplicate — update existing instead of creating new
"""

import logging
import re
from datetime import timedelta

from sqlalchemy import and_, cast, select
from sqlalchemy.ext.asyncio import AsyncSession
from geoalchemy2 import Geography
from geoalchemy2.functions import ST_DWithin
from geoalchemy2.shape import from_shape
from shapely.geometry import Point

from app.models import Event
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
    new_channel: str,
    new_severity: int | None,
    new_killed: int | None,
):
    """Update an existing event with info from a duplicate report."""
    # Increment report count
    new_count = (existing.report_count or 1) + 1
    existing.report_count = new_count

    # Append channel
    channels = existing.reporting_channels or existing.channel_name
    if new_channel and new_channel not in channels:
        channels = f"{channels}, {new_channel}"
    existing.reporting_channels = channels

    # Take higher severity. None is "not measured", which is neither higher
    # nor lower than a number: an unmeasured report must not overwrite a
    # measured one, and it must not be compared as 0 or as 10. It can only
    # fill a slot that is itself still empty (i.e. it cannot — nothing to do).
    if new_severity is not None and (
        existing.severity is None or new_severity > existing.severity
    ):
        existing.severity = new_severity

    # killed_reported: fill an empty slot, never overwrite a stated count.
    #
    # NULL in this column is not "unknown" — it is the claim "no report behind
    # this row stated a toll". So a merge that drops an incoming count does not
    # merely lose information, it writes a falsehood: channel A posts a strike
    # with no toll, channel B posts "17 killed" eleven minutes later, and the
    # surviving row positively asserts that nobody gave a number. `is not None`
    # rather than truthiness, because a stated 0 ("nobody was killed") is a
    # measurement and fills the slot like any other count.
    #
    # It deliberately does NOT take-the-higher the way severity does above.
    # Severity is a 1-10 grade where "higher" is a defensible worst case; this
    # is a number copied out of one message, and a maximum over sources is a
    # figure no source stated — a consensus this merge is in no position to
    # compute. Tolls climb as reports come in, but they are also revised down,
    # and two channels simply disagreeing looks identical from here. One
    # integer cannot say "A said 3, B said 17": whatever it holds reads as THE
    # count. So the column keeps one statable meaning — the count given by the
    # first contributing report to give one — and THIS merge never changes a
    # count already on the row.
    #
    # "Written at most once" is a property of this function, not of the column.
    # Two admin tasks overwrite killed_reported outright, each writing whatever
    # the row's own raw_text re-classifies to, None included: events.py:148 in
    # _fix_null_coords_task, and events.py:253 in
    # _reclassify_vague_locations_task, whose
    # `db_ev.killed_reported = classified.get("killed_reported")` is
    # unconditional. A number a reader already saw can therefore be replaced or
    # cleared later — just not by a second report arriving here.
    #
    # The costs, logged rather than hidden:
    #
    #   - A later, larger or corrected toll never reaches the row, and in the
    #     worst case the row keeps 0 while a second channel reported 17. Only
    #     the log says so; nothing in the row does.
    #
    #   - A dedup FALSE POSITIVE now manufactures a measurement — the one
    #     failure mode a copied-out-of-the-text number was supposed to be
    #     immune to. Read off check_duplicate above: a match needs the same
    #     event_type, a timestamp inside ±15 minutes, and Jaccard over summary
    #     word sets > 0.4; a located incoming event additionally needs a 50km
    #     ST_DWithin against a located row, while an unlocated one gets no
    #     distance test at all (geometry IS NULL partitions the pool, it does
    #     not place it) and matches on text + time + type alone across
    #     _UNLOCATED_CANDIDATES = 200 rows. Two distinct strikes on the same
    #     city a quarter-hour apart share the type, share the window and share
    #     most of their words: that bar is cleared routinely in this domain.
    #     Before this column existed a bad match cost report_count, severity
    #     and source_reliability — all of them judgments about the event. Now
    #     it also stamps one report's death toll onto a row whose own raw_text
    #     never contained a number, and the row then states that toll as its
    #     own. reporting_channels lists the channel it came from but cannot say
    #     the number came from that channel rather than this row's own text.
    #
    #   - On a LOCATED row whose own extraction failed, the fill is permanent.
    #     The call sites exempt killed_reported from the extraction_status
    #     guard they apply to severity (see telegram.py / news_feeds.py), so
    #     such a row can render "17 KILLED" beside "CLASSIFY FAILED: api_400"
    #     in one card with nothing to reconcile them. On an unlocated row that
    #     at least heals: _fix_null_coords_task re-classifies rows with NULL
    #     geometry and a blank/unknown location_name and writes that row's own
    #     text's answer, None included, back over this field. A located row is
    #     outside it — events.py:80 selects `Event.geometry.is_(None)` only —
    #     and the vague-location task reaches a located row solely when its
    #     location_name is still a country or other _VAGUE_LOCATIONS term,
    #     which a row precise enough to have geocoded usually is not. For those
    #     rows the merged number stays, next to a status saying the row's own
    #     extraction produced nothing.
    #
    # Both figures can only coexist under Phase 4's link-don't-merge, which
    # keeps each report's text. This policy computes no number no source stated
    # — no maximum, no sum — but, per the false-positive cost above, it can
    # still attach a stated number to the wrong event.
    if new_killed is not None and existing.killed_reported is None:
        existing.killed_reported = new_killed
        logger.info(
            "Event #%d: killed_reported NULL -> %d, stated by %s",
            existing.id, new_killed, new_channel,
        )
    elif new_killed is not None and new_killed != existing.killed_reported:
        # A disagreement between two sources is itself a finding. It cannot be
        # stored in this column, so it goes where it can still be read.
        logger.warning(
            "Death toll disagreement on event #%d: row holds %d, %s reports %d — keeping %d",
            existing.id, existing.killed_reported, new_channel, new_killed,
            existing.killed_reported,
        )

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
    # Bonus: +1 if 3+ independent sources (capped at 5).
    new_channel_rel = _channel_reliability(new_channel) or 1
    current_rel = existing.source_reliability or 1
    combined = max(current_rel, new_channel_rel)
    if new_count >= 3:
        combined = min(5, combined + 1)
    if combined != existing.source_reliability:
        existing.source_reliability = combined
        logger.debug(
            "Reliability boosted to %d for event #%d (%d sources)",
            combined, existing.id, new_count,
        )

    await session.commit()
    await session.refresh(existing)
    logger.info(
        "Merged into event #%d (now %d reports | reliability=%d | sources: %s)",
        existing.id, new_count, existing.source_reliability or 0, existing.reporting_channels,
    )
