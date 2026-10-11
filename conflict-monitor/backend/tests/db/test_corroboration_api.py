"""The corroboration served with every event, and the reports under it.
Protects C84.

The chip used to print `report_count` as "N sources": no unit, no evidence
under it, and no way to read the reports the count claimed. Now every event
payload carries reports · channels · outlets · unrecorded, computed from the
rows, and /events/{id}/reports serves the rows.
"""

import datetime

import pytest
from fastapi import HTTPException

from app.models import EventReport
from app.routes.events import event_reports, get_event, list_events

from tests.db.conftest import NOW, make_event

H = datetime.timedelta(hours=1)


async def _list(session):
    # Every Query() default spelled out: called directly, FastAPI's defaults
    # would arrive as Query objects.
    return await list_events(
        limit=50, event_type=None, after=None, before=None, min_reliability=None, session=session
    )


async def test_every_listed_event_carries_counts_the_rows_can_back(session):
    one = make_event("one report", report_count=1)
    many = make_event("three rows, one outlet", report_count=3, when=NOW + H)
    session.add_all([one, many])
    await session.flush()
    session.add_all([
        EventReport(event_id=one.id, channel="chan_a"),
        # The outlet's Telegram post, its X mirror, and a forward of it: three
        # rows, three channel names, one outlet.
        EventReport(event_id=many.id, channel="OSINTWarfare", forwarded_from=""),
        EventReport(event_id=many.id, channel="x_osintwarfare"),
        EventReport(event_id=many.id, channel="reposter", forwarded_from="OSINTWarfare"),
    ])
    await session.flush()

    out = await _list(session)
    by_id = {e.id: e.corroboration.model_dump() for e in out}
    assert by_id[one.id] == {"reports": 1, "channels": 1, "outlets": 1, "unrecorded": 0, "linked": 0}
    assert by_id[many.id] == {"reports": 3, "channels": 3, "outlets": 1, "unrecorded": 0, "linked": 0}


async def test_unrecorded_is_the_archive_s_destroyed_reports(session):
    """The one archive row that claims 214 has one backfilled row under it:
    213 reports whose text is gone, stated as such rather than as 214
    confirmations."""
    archive_row = make_event("archive row", report_count=214)
    session.add(archive_row)
    await session.flush()
    session.add(EventReport(event_id=archive_row.id, channel="chan"))
    await session.flush()

    out = await get_event(archive_row.id, session=session)
    assert out.corroboration.model_dump() == {"reports": 1, "channels": 1, "outlets": 1, "linked": 0, "unrecorded": 213}


async def test_an_event_with_no_report_rows_says_so(session):
    """A demo row written before demo wrote report rows, or any writer that
    stops recording its report: zero rows under a count of one is shown as
    one unrecorded, never as one report."""
    bare = make_event("no rows")
    session.add(bare)
    await session.flush()
    (out,) = await _list(session)
    assert out.corroboration.model_dump() == {"reports": 0, "channels": 0, "outlets": 0, "linked": 0, "unrecorded": 1}


async def test_the_reports_route_serves_every_row_oldest_first_with_its_outlet(session):
    event = make_event("x")
    session.add(event)
    await session.flush()
    session.add_all([
        EventReport(
            event_id=event.id, channel="reposter", forwarded_from="OSINTWarfare",
            raw_text="fwd text", killed_reported=17, reported_at=NOW + H,
        ),
        EventReport(event_id=event.id, channel="x_osintwarfare", raw_text="first text", reported_at=NOW),
    ])
    await session.flush()

    rows = await event_reports(event.id, session=session)
    assert [r.raw_text for r in rows] == ["first text", "fwd text"]
    # The outlet of a forward is its origin's, so the list can group by it.
    assert [r.outlet for r in rows] == ["osintwarfare", "osintwarfare"]
    assert rows[1].forwarded_from == "OSINTWarfare"
    # The merged report's death toll lives here and nowhere else.
    assert rows[1].killed_reported == 17


async def test_the_reports_route_404s_on_an_unknown_event_and_is_empty_on_a_bare_one(session):
    with pytest.raises(HTTPException) as exc:
        await event_reports(999_999, session=session)
    assert exc.value.status_code == 404

    bare = make_event("no rows yet")
    session.add(bare)
    await session.flush()
    assert await event_reports(bare.id, session=session) == []
