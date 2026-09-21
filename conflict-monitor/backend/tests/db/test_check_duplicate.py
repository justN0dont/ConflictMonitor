"""The geometry guard. Protects 89c6f54 / C8.

Unlocated rows carry NULL geometry now. That is the right storage, but it
removed the accidental protection the (-25, 80) sentinel gave this query:
every unlocated row used to sit on one point, so the 50km spatial filter still
partitioned located from unlocated traffic. With NULL geometry the filter
simply does not apply, and an unlocated event would match on time-window and
event_type alone — "military, within +/-15 minutes" against the entire table.
"""

import datetime

import pytest

from app.services.dedup import check_duplicate
from tests.db.conftest import NOW, make_event
from tests.unit.test_dedup_pure import BAR_A, BAR_B

_SUMMARY = "Israeli airstrike hit the Natanz enrichment facility overnight"

# Tel Aviv, and a point ~180km away. Both well inside the +/-15min window.
TLV = (32.0853, 34.7818)
FAR = (33.6, 36.3)  # Damascus-ish


async def test_a_located_event_never_matches_an_unlocated_row(session):
    """89c6f54 / C8. Identical text, same type, same minute — and it must
    still be None, or a known coordinate is absorbed into an event we could
    not place."""
    session.add(make_event(_SUMMARY))  # unlocated
    await session.flush()

    found = await check_duplicate(
        session, _SUMMARY, "military", TLV[0], TLV[1], NOW
    )
    assert found is None


async def test_an_unlocated_event_never_matches_a_located_row(session):
    """89c6f54 / C8, the other direction. An unlocated event stays a
    separate, honestly unlocated row until something geocodes it, rather
    than being merged into a located one on text alone."""
    session.add(make_event(_SUMMARY, lat=TLV[0], lon=TLV[1]))
    await session.flush()

    found = await check_duplicate(session, _SUMMARY, "military", None, None, NOW)
    assert found is None


async def test_located_matches_located_inside_fifty_km(session):
    """89c6f54 / C8. The positive control: the guard narrows the pool, it
    does not empty it."""
    session.add(make_event(_SUMMARY, lat=TLV[0], lon=TLV[1]))
    await session.flush()

    found = await check_duplicate(
        session, _SUMMARY, "military", TLV[0] + 0.05, TLV[1] + 0.05, NOW
    )
    assert found is not None


async def test_located_does_not_match_beyond_fifty_km(session):
    """89c6f54 / C8. ST_DWithin is in metres on the geography cast; a
    degrees-based comparison would have matched here."""
    session.add(make_event(_SUMMARY, lat=TLV[0], lon=TLV[1]))
    await session.flush()

    found = await check_duplicate(
        session, _SUMMARY, "military", FAR[0], FAR[1], NOW
    )
    assert found is None


async def test_unlocated_matches_unlocated_on_text_alone(session):
    """89c6f54 / C8. The unlocated pool dedups against itself. This is the
    branch C74 fired on, and it is deliberately still reachable — what
    89c6f54 removed is its reach into located rows."""
    session.add(make_event(_SUMMARY))
    await session.flush()

    found = await check_duplicate(session, _SUMMARY, "military", None, None, NOW)
    assert found is not None


async def test_a_different_event_type_never_matches(session):
    session.add(make_event(_SUMMARY, event_type="military"))
    await session.flush()

    found = await check_duplicate(session, _SUMMARY, "diplomatic", None, None, NOW)
    assert found is None


@pytest.mark.parametrize("minutes,expected", [(14, True), (16, False), (-14, True), (-16, False)])
async def test_the_fifteen_minute_window_holds_on_both_sides(session, minutes, expected):
    session.add(make_event(_SUMMARY))
    await session.flush()

    found = await check_duplicate(
        session,
        _SUMMARY,
        "military",
        None,
        None,
        NOW + datetime.timedelta(minutes=minutes),
    )
    assert (found is not None) is expected


async def test_exactly_the_threshold_is_not_a_duplicate(session):
    """ce5d994 / C74. The bar is `> 0.4`, strict. BAR_A/BAR_B score exactly
    0.4 — pinned in tests/unit/test_dedup_pure.py — so this is the boundary
    and not an approximation of it."""
    session.add(make_event(BAR_A))
    await session.flush()

    found = await check_duplicate(session, BAR_B, "military", None, None, NOW)
    assert found is None
