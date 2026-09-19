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
