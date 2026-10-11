"""Who said it, counted once per outlet (C84).

A report row is one message about an event (models.EventReport). Rows are not
confirmations. Two of them are one report twice when the second is a Telegram
forward of the first, and one outlet twice when the same outlet posts on two
transports — `x_osintwarfare` and `OSINTWarfare` are the archive's own pair
(C11, C78). This module is the one place that collapses rows onto the people
who actually said something, and every count it returns is a FLOOR:

  reports     rows under the event in event_reports
  channels    distinct non-empty channel names among them
  outlets     distinct origins once forwards and transport mirrors collapse
  unrecorded  reports the merge destroyed before event_reports existed:
              max(report_count - reports, 0); 0 on anything ingested since
  linked      other events the link rule reads as this incident
              (services/links.py): rows kept apart, never merged

What it does NOT see: a channel that pastes another's text and posts it as its
own. A pasted repost carries no forward header, so it counts as an outlet
here. Reading that needs text containment over raw_text (C76), which needs the
gold pairs, and this module will take a third collapse when they exist. Until
then "3 outlets" means "three names that did not tell us they were repeating
each other", and the chip's title says so.

Pure above the line; `read_events` below it is the one database read, shared
by every place an EventRead leaves the process (both routes, every WebSocket
push), so no payload shows a count the rows cannot back.
"""

from typing import Iterable, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Event, EventReport
from app.schemas import Corroboration, EventRead
from app.services.links import link_counts

# The transport prefix the archive's channel names carry for an outlet's X
# mirror. Stripping it is the whole "family" rule for mirrors: the register
# names three such pairs and no counter-example.
X_PREFIX = "x_"

Row = tuple[str, str | None]  # (channel, forwarded_from) as event_reports holds them


def outlet(channel: str) -> str:
    """The outlet behind a channel name: case-folded, transport prefix off."""
    key = channel.strip().casefold()
    if key.startswith(X_PREFIX):
        key = key[len(X_PREFIX):]
    return key


def origin(channel: str, forwarded_from: str | None) -> str:
    """Whose words a report is.

    The forwarded-from name when Telegram said the message was a forward, else
    the channel. "" and NULL in forwarded_from both mean the channel's own
    words: "" is Telegram saying "not a forward", NULL is a row that never
    recorded the header (RSS, the archive, demo) — see models.EventReport.
    """
    return forwarded_from if forwarded_from else channel


def outlets(rows: Iterable[Row]) -> int:
    """Distinct outlets among the rows. Unnamed origins are excluded, as the
    channel count excludes unnamed channels: two of them cannot be shown to
    be distinct from each other."""
    return len({outlet(o) for o in (origin(c, f) for c, f in rows) if o.strip()})


def corroboration(rows: Sequence[Row], report_count: int | None, linked: int = 0) -> Corroboration:
    rows = list(rows)
    return Corroboration(
        reports=len(rows),
        channels=len({c for c, _ in rows if c}),
        outlets=outlets(rows),
        unrecorded=max((report_count or 0) - len(rows), 0),
        linked=linked,
    )


# ── The one database read ────────────────────────────────────────────────────


async def report_rows(session: AsyncSession, event_ids: Iterable[int]) -> dict[int, list[Row]]:
    """(channel, forwarded_from) per event, one query for the whole page."""
    ids = list(event_ids)
    by_event: dict[int, list[Row]] = {i: [] for i in ids}
    if not ids:
        return by_event
    result = await session.execute(
        select(EventReport.event_id, EventReport.channel, EventReport.forwarded_from).where(
            EventReport.event_id.in_(ids)
        )
    )
    for event_id, channel, forwarded_from in result:
        by_event[event_id].append((channel, forwarded_from))
    return by_event


async def read_events(session: AsyncSession, events: Sequence[Event]) -> list[EventRead]:
    """EventRead for each event with its corroboration attached."""
    ids = [e.id for e in events]
    rows = await report_rows(session, ids)
    links = await link_counts(session, ids)
    return [
        EventRead.model_validate(e).model_copy(
            update={"corroboration": corroboration(rows[e.id], e.report_count, links[e.id])}
        )
        for e in events
    ]


async def read_event(session: AsyncSession, event: Event) -> EventRead:
    (out,) = await read_events(session, [event])
    return out
