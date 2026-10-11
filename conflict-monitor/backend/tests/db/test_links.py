"""The link between events, against the database (services/links.py).

Link, don't merge, between rows: two reports of one incident that dedup kept
apart — later than 15 minutes, further than 50 km, or one of them unplaced —
get one event_links row with the score and what the rule saw, and neither
event changes. Every test here is a claim the register makes.
"""

import datetime

import pytest
from fastapi import HTTPException
from sqlalchemy import select, text

from app.models import EventLink
from app.routes.events import event_links, get_event
from app.services import linkrule
from app.services.dedup import check_duplicate
from app.services.links import link_event
from tests.db.conftest import NOW, make_event

MIN = datetime.timedelta(minutes=1)
TYRE = (33.2726, 35.2039)
NABATIEH = (33.3772, 35.4836)  # ~28 km from Tyre
DAMASCUS = (33.5138, 36.2765)  # ~100 km from Tyre
TEHRAN = (35.6892, 51.3890)  # ~1,500 km from Tyre

STRIKE_A = "Breaking | Israeli aircraft carried out an airstrike on a building in the town of Tebnine, southern Lebanon"
STRIKE_B = "Israeli aircraft carried out an airstrike on a building in the town of Tebnine, southern Lebanon, killing two"


async def test_a_second_report_beyond_dedup_s_window_is_linked_not_merged(session):
    """40 minutes apart and 28 km apart: dedup never sees the pair; the link
    rule reads one incident. Both rows stand exactly as they were."""
    a = make_event("airstrike on Tebnine", raw_text=STRIKE_A, lat=TYRE[0], lon=TYRE[1], location_name="Tebnine", severity=4)
    b = make_event("Tebnine strike kills two", raw_text=STRIKE_B, lat=NABATIEH[0], lon=NABATIEH[1], when=NOW + 40 * MIN,
                   location_name="Tebnine", killed_reported=2)
    session.add(a)
    await session.flush()
    # Asked as ingest asks it, before the second row exists: dedup sees nothing.
    assert await check_duplicate(session, b.summary, "military", b.lat, b.lon, b.timestamp) is None
    session.add(b)
    await session.flush()

    written = await link_event(session, b)
    assert len(written) == 1
    link = written[0]
    assert (link.a_id, link.b_id) == (a.id, b.id)
    assert link.rule == linkrule.RULE and link.score > linkrule.BAR
    assert link.place == "same" and link.towns == "same"
    assert link.dt_min == 40.0 and 27 < link.km < 30
    await session.refresh(a)
    await session.refresh(b)
    assert (a.report_count, a.severity, a.killed_reported) == (1, 4, None)
    assert (b.report_count, b.severity, b.killed_reported) == (1, None, 2)


async def test_an_unplaced_report_links_to_a_placed_one(session):
    """One side unlocated is where dedup refuses to look at all; the link
    reads the texts and the names and nothing else."""
    a = make_event("x", raw_text=STRIKE_A, lat=TYRE[0], lon=TYRE[1], location_name="Tebnine")
    b = make_event("y", raw_text=STRIKE_B, when=NOW + 90 * MIN, location_name="Unknown")
    session.add_all([a, b])
    await session.flush()
    (link,) = await link_event(session, b)
    assert link.km is None and link.place == "unknown" and link.towns == "same"


async def test_the_same_template_with_a_different_town_is_not_linked(session):
    beita = "Israeli occupation forces storm the town of Beita, south of Nablus, in the occupied West Bank"
    sarra = "Israeli occupation forces storm the town of Sarra, west of Nablus, in the occupied West Bank"
    a = make_event("beita", raw_text=beita, location_name="West Bank")
    b = make_event("sarra", raw_text=sarra, location_name="West Bank", when=NOW + 20 * MIN)
    session.add_all([a, b])
    await session.flush()
    assert await link_event(session, b) == []


@pytest.mark.parametrize(
    "kw",
    [
        {"event_type": "diplomatic"},
        {"when": NOW + 181 * MIN},
        {"lat": TEHRAN[0], "lon": TEHRAN[1]},
    ],
    ids=["other-type", "outside-the-window", "beyond-500km"],
)
async def test_the_candidates_are_the_gold_pairs_population(session, kw):
    a = make_event("x", raw_text=STRIKE_A, lat=TYRE[0], lon=TYRE[1], location_name="Tebnine")
    args = {"raw_text": STRIKE_B, "lat": DAMASCUS[0], "lon": DAMASCUS[1], "when": NOW + 60 * MIN, "location_name": "Tebnine"}
    args.update(kw)
    b = make_event("y", **args)
    session.add_all([a, b])
    await session.flush()
    assert await link_event(session, b) == []


async def test_a_pair_is_one_row_however_often_and_from_whichever_side(session):
    a = make_event("x", raw_text=STRIKE_A, location_name="Tebnine")
    b = make_event("y", raw_text=STRIKE_B, location_name="Tebnine", when=NOW + 30 * MIN)
    session.add_all([a, b])
    await session.flush()
    assert len(await link_event(session, b)) == 1
    assert await link_event(session, b) == []
    assert await link_event(session, a) == []
    rows = (await session.execute(select(EventLink))).scalars().all()
    assert [(r.a_id, r.b_id) for r in rows] == [(a.id, b.id)]


async def test_the_payload_counts_links_on_both_sides_and_the_route_lists_them(session):
    a = make_event("x", raw_text=STRIKE_A, lat=TYRE[0], lon=TYRE[1], location_name="Tebnine")
    b = make_event("y", raw_text=STRIKE_B, lat=NABATIEH[0], lon=NABATIEH[1], when=NOW + 40 * MIN, location_name="Tebnine")
    session.add_all([a, b])
    await session.flush()
    await link_event(session, b)

    assert (await get_event(a.id, session)).corroboration.linked == 1
    assert (await get_event(b.id, session)).corroboration.linked == 1

    from_a = await event_links(a.id, session)
    from_b = await event_links(b.id, session)
    assert [l.event.id for l in from_a] == [b.id] and [l.event.id for l in from_b] == [a.id]
    # Signed from the side asked about: b came 40 minutes after a.
    assert from_a[0].dt_min == 40.0 and from_b[0].dt_min == -40.0
    assert from_a[0].score == from_b[0].score > linkrule.BAR
    assert from_a[0].event.corroboration.linked == 1

    with pytest.raises(HTTPException) as err:
        await event_links(999_999, session)
    assert err.value.status_code == 404


async def test_deleting_an_event_deletes_its_links(session):
    """The admin routes delete events outside the ORM; a link to a row that
    is gone would be a claim about nothing."""
    a = make_event("x", raw_text=STRIKE_A, location_name="Tebnine")
    b = make_event("y", raw_text=STRIKE_B, location_name="Tebnine", when=NOW + 30 * MIN)
    session.add_all([a, b])
    await session.flush()
    await link_event(session, b)
    await session.execute(text("DELETE FROM events WHERE id = :id"), {"id": a.id})
    assert (await session.execute(select(EventLink))).scalars().all() == []
    assert (await get_event(b.id, session)).corroboration.linked == 0
