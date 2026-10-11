"""Writing and reading the links between events. The rule is linkrule.py; this
is the one place that applies it to the database.

A link is a row (models.EventLink), never a merge: both events keep their text,
their place, their counts and their scores exactly as they were. link_event
runs once per new event, after that event is on disk, and never undoes the
insert — a failure here is logged and the event stands. The archive is not
back-linked by anything yet (FINDINGS.md, "Link between events — decisions").
"""
from __future__ import annotations

import logging
from datetime import timedelta

from geoalchemy2 import Geography
from geoalchemy2.functions import ST_DWithin
from sqlalchemy import and_, cast, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Event, EventLink
from app.services import linkrule

logger = logging.getLogger(__name__)

# A bound on work, not a claim that no partner lies beyond it. dedup's densest
# measured ±15 min same-type window held 234 rows (dedup.py); this window is
# twelve times wider, newest first, so 2,000 covers that density with margin
# and a denser hour can still hide a partner past the cut.
_CANDIDATES = 2000


async def candidates(session: AsyncSession, event: Event) -> list[Event]:
    """The population the rule is measured on: same event_type, within
    linkrule.WINDOW_MIN either side, and within linkrule.MAX_KM when both are
    located; an unlocated side is never a reason to exclude. Not the event
    itself. Deterministic order, so a run is reproducible."""
    window = timedelta(minutes=linkrule.WINDOW_MIN)
    stmt = select(Event).where(
        and_(
            Event.id != event.id,
            Event.event_type == event.event_type,
            Event.timestamp >= event.timestamp - window,
            Event.timestamp <= event.timestamp + window,
        )
    )
    if event.geometry is not None:
        stmt = stmt.where(
            or_(
                Event.geometry.is_(None),
                ST_DWithin(
                    cast(Event.geometry, Geography),
                    cast(event.geometry, Geography),
                    linkrule.MAX_KM * 1000,
                ),
            )
        )
    stmt = stmt.order_by(Event.timestamp.desc(), Event.id.desc()).limit(_CANDIDATES)
    return list((await session.execute(stmt)).scalars().all())


async def partners(session: AsyncSession, event_id: int) -> set[int]:
    """The ids already linked to an event, either side."""
    rows = await session.execute(
        select(EventLink.a_id, EventLink.b_id).where(
            or_(EventLink.a_id == event_id, EventLink.b_id == event_id)
        )
    )
    return {b if a == event_id else a for a, b in rows}


async def link_event(session: AsyncSession, event: Event) -> list[EventLink]:
    """Assess every candidate against `event` and write one row per linked
    pair that has none yet. Commits. Returns the rows written. Never raises:
    the event is already on disk and a link is a claim about it, not part
    of it."""
    try:
        done = await partners(session, event.id)
        written: list[EventLink] = []
        for other in await candidates(session, event):
            if other.id in done:
                continue
            km = None
            if event.lat is not None and event.lon is not None and other.lat is not None and other.lon is not None:
                km = linkrule.haversine_km(event.lat, event.lon, other.lat, other.lon)
            dt_min = (other.timestamp - event.timestamp).total_seconds() / 60.0
            if not linkrule.within_window(dt_min, km):
                continue
            a = linkrule.assess(event.raw_text, event.location_name, other.raw_text, other.location_name)
            if not a.linked:
                continue
            lo, hi = (event, other) if event.id < other.id else (other, event)
            written.append(
                EventLink(
                    a_id=lo.id,
                    b_id=hi.id,
                    rule=linkrule.RULE,
                    score=a.score,
                    place=a.place,
                    towns=a.towns,
                    # Signed, b after a: the reader on either side flips it.
                    dt_min=round((hi.timestamp - lo.timestamp).total_seconds() / 60.0, 1),
                    km=None if km is None else round(km, 1),
                )
            )
        if written:
            session.add_all(written)
            await session.commit()
            logger.info(
                "Linked event #%d to %s (rule %s)",
                event.id, [(w.b_id if w.a_id == event.id else w.a_id, w.score) for w in written], linkrule.RULE,
            )
        return written
    except Exception:
        logger.exception("Linking event #%s failed; the event stands unlinked", event.id)
        await session.rollback()
        return []


async def link_counts(session: AsyncSession, event_ids: list[int]) -> dict[int, int]:
    """Links touching each event, one query for the whole page."""
    counts = {i: 0 for i in event_ids}
    if not event_ids:
        return counts
    rows = await session.execute(
        select(EventLink.a_id, EventLink.b_id).where(
            or_(EventLink.a_id.in_(event_ids), EventLink.b_id.in_(event_ids))
        )
    )
    for a, b in rows:
        if a in counts:
            counts[a] += 1
        if b in counts:
            counts[b] += 1
    return counts


async def linked_events(session: AsyncSession, event_id: int) -> list[tuple[EventLink, Event]]:
    """Every link touching an event with the event on the other side of it,
    strongest first."""
    rows = await session.execute(
        select(EventLink).where(or_(EventLink.a_id == event_id, EventLink.b_id == event_id))
        .order_by(EventLink.score.desc(), EventLink.id.asc())
    )
    links = list(rows.scalars().all())
    if not links:
        return []
    other_ids = [l.b_id if l.a_id == event_id else l.a_id for l in links]
    events = {
        e.id: e for e in (await session.execute(select(Event).where(Event.id.in_(other_ids)))).scalars().all()
    }
    return [(l, events[i]) for l, i in zip(links, other_ids) if i in events]
