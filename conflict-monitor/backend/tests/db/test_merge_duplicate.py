"""Link, don't merge. Protects ce5d994 (C74) and 8523961 (C10, C11).

The headline is test_a_death_toll_never_moves_onto_the_event. events.
killed_reported is "people THIS row's raw_text said were killed" — a quantity
a reader can check against that text in one second. A merged count lands on a
row whose own text does not contain it, so the check comes back NEGATIVE on a
row that is not wrong, and nothing afterwards can tell a merged 17 from a
hallucinated one.

Every merge below goes through check_duplicate first, because "at sim 0.44"
is a claim about the real matcher and not about a number passed by hand.
"""

import pytest
from sqlalchemy import func, select

from app.models import Event, EventReport
from app.services.dedup import check_duplicate, merge_duplicate
from tests.db.conftest import NOW, make_event

TLV = (32.0853, 34.7818)

# Scores 4/9 = 0.444 — C74's measured margin, just over the `> 0.4` bar.
NEAR_A = "Natanz enrichment facility struck overnight"
NEAR_B = "Natanz enrichment facility overnight blast, casualties reported unclear"


def incoming_report(summary, **kw):
    """The report the caller has built from the message it arrived in, not yet
    attached to anything — exactly what merge_duplicate is handed."""
    return EventReport(
        source=kw.pop("source", "telegram"),
        channel=kw.pop("channel", "some_channel"),
        raw_text=kw.pop("raw_text", "Second channel's own wording: " + summary),
        summary=summary,
        source_url=kw.pop("source_url", "https://t.me/some_channel/4821"),
        telegram_message_id=kw.pop("telegram_message_id", 4821),
        **kw,
    )


# ── ce5d994 / C74: the death toll stops moving between events ────────────────


@pytest.mark.parametrize("located", [True, False], ids=["located", "unlocated"])
@pytest.mark.parametrize(
    "existing_summary,incoming_summary",
    [(NEAR_A, NEAR_B), (NEAR_A, NEAR_A)],
    ids=["sim-0.44", "sim-1.0"],
)
async def test_a_death_toll_never_moves_onto_the_event(
    session, located, existing_summary, incoming_summary
):
    """ce5d994 / C74. merge_duplicate writes killed_reported on the EVENT at
    no similarity, on neither branch. A filled count would sit on a row whose
    own raw_text does not contain it — and C74's false positive fired at
    exactly 0.44, so "only on a bad match" was never the protection."""
    lat, lon = TLV if located else (None, None)
    existing = make_event(existing_summary, lat=lat, lon=lon)
    session.add(existing)
    await session.flush()
    assert existing.killed_reported is None

    found = await check_duplicate(
        session, incoming_summary, "military", lat, lon, NOW
    )
    assert found is not None, "the matcher did not fire — this test proves nothing"

    report = incoming_report(incoming_summary, killed_reported=17, extraction_status="ok")
    await merge_duplicate(session, found, report, new_severity=6)

    assert found.killed_reported is None, "a death toll moved between events"
    assert report.killed_reported == 17, "the count was dropped instead of stored"


async def test_a_measured_count_on_the_event_is_not_overwritten_either(session):
    """ce5d994 / C74. The event's own classifier result is the only writer of
    that column. A merge does not touch it in either direction."""
    existing = make_event(NEAR_A, killed_reported=3, extraction_status="ok")
    session.add(existing)
    await session.flush()

    await merge_duplicate(
        session, existing, incoming_report(NEAR_A, killed_reported=17), new_severity=None
    )
    assert existing.killed_reported == 3


# ── 8523961 / C10: the report keeps its text, identity and toll ──────────────


async def test_the_report_row_keeps_everything_the_merge_used_to_destroy(session):
    """8523961 / C10. A merge kept the channel name and the higher severity
    and nothing else — 19,027 reports' wording, permalinks, message ids and
    stated tolls were destroyed at the moment they arrived, and that text is
    not recoverable."""
    existing = make_event(NEAR_A)
    session.add(existing)
    await session.flush()

    report = incoming_report(
        NEAR_B,
        raw_text="BREAKING: huge blast at Natanz, at least 17 dead",
        source_url="https://t.me/other_channel/991",
        telegram_message_id=991,
        killed_reported=17,
        extraction_status="ok",
        channel="other_channel",
    )
    await merge_duplicate(session, existing, report, new_severity=None)

    stored = (
        await session.execute(
            select(EventReport).where(EventReport.event_id == existing.id)
        )
    ).scalar_one()
    assert stored.raw_text == "BREAKING: huge blast at Natanz, at least 17 dead"
    assert stored.source_url == "https://t.me/other_channel/991"
    assert stored.telegram_message_id == 991
    assert stored.killed_reported == 17
    assert stored.extraction_status == "ok"
    assert stored.summary == NEAR_B, "without it the merge decision is unreproducible"


async def test_the_report_row_lands_in_the_same_transaction_as_the_counter(session):
    """8523961. A report row added by the caller AFTER merge_duplicate
    returned would commit separately, and a crash in between leaves
    report_count claiming a report with no row under it — a corroboration
    claim with no evidence."""
    existing = make_event(NEAR_A)
    session.add(existing)
    await session.flush()

    report = incoming_report(NEAR_B)
    await merge_duplicate(session, existing, report, new_severity=None)

    rows = (
        await session.execute(
            select(func.count()).select_from(EventReport).where(
                EventReport.event_id == existing.id
            )
        )
    ).scalar_one()
    assert existing.report_count == 2
    assert rows == 1
    assert report.event_id == existing.id


async def test_report_count_is_stored_not_derived(session):
    """8523961 / C10. report_count and reporting_channels are the ONLY
    surviving record of the reports merge destroyed, so
    `report_count - count(reports)` is the per-row size of that loss — 213 on
    the one archive row that claims 214. Deriving the counter from this table
    would erase the evidence of what is missing."""
    existing = make_event(NEAR_A, report_count=214)
    session.add(existing)
    await session.flush()
    session.add(EventReport(event_id=existing.id, channel="backfilled", summary=NEAR_A))
    await session.flush()

    await merge_duplicate(session, existing, incoming_report(NEAR_B), new_severity=None)

    rows = (
        await session.execute(
            select(func.count()).select_from(EventReport).where(
                EventReport.event_id == existing.id
            )
        )
    ).scalar_one()
    assert existing.report_count == 215
    assert rows == 2


# ── ce5d994: severity, where None is not a number ────────────────────────────


@pytest.mark.parametrize(
    "current,incoming,expected",
    [(7, None, 7), (7, 9, 9), (7, 5, 7), (None, None, None), (None, 4, 4)],
)
async def test_an_unmeasured_severity_never_overwrites_a_measured_one(
    session, current, incoming, expected
):
    """ce5d994 / 89c6f54. None is "not measured", which is neither higher nor
    lower than a number: it must not overwrite a measured value and must not
    be compared as 0 or as 10."""
    existing = make_event(NEAR_A, severity=current)
    session.add(existing)
    await session.flush()

    await merge_duplicate(session, existing, incoming_report(NEAR_B), incoming)
    assert existing.severity == expected


# ── 8523961 / C11: the boost gates on distinct channels ──────────────────────


async def _seed_reports(session, event, channels):
    for i, ch in enumerate(channels):
        session.add(
            EventReport(event_id=event.id, channel=ch, summary=NEAR_A, telegram_message_id=100 + i)
        )
    await session.flush()


async def test_one_channel_repeating_itself_earns_no_boost(session):
    """8523961 / C11. The gate used to count merge EVENTS, and the pipeline
    generates those by itself — a restart re-ingests a stored RSS article and
    merges it again, so three self-merges of one article raised confidence
    for one source repeating itself.

    report_count=2 is load-bearing, not decoration: it makes this the THIRD
    self-merge, so `new_count` reaches 3 and the old gate would fire. Left at
    the default of 1 the merge counter only reaches 2, the old gate misses on
    arithmetic rather than on principle, and this test passes against the very
    defect it names."""
    existing = make_event(NEAR_A, source_reliability=4, report_count=2)
    session.add(existing)
    await session.flush()
    await _seed_reports(session, existing, ["same_channel", "same_channel"])

    await merge_duplicate(
        session, existing, incoming_report(NEAR_B, channel="same_channel"), None
    )
    assert existing.source_reliability == 4


async def test_three_distinct_channels_earn_the_boost(session):
    """8523961 / C11. The positive control: independent corroboration still
    raises reliability, which is what makes the test above a narrowing and
    not a deletion."""
    existing = make_event(NEAR_A, source_reliability=4)
    session.add(existing)
    await session.flush()
    await _seed_reports(session, existing, ["chan_a", "chan_b"])

    await merge_duplicate(session, existing, incoming_report(NEAR_B, channel="chan_c"), None)
    assert existing.source_reliability == 5


async def test_the_boost_is_capped_at_five(session):
    existing = make_event(NEAR_A, source_reliability=5)
    session.add(existing)
    await session.flush()
    await _seed_reports(session, existing, ["chan_a", "chan_b"])

    await merge_duplicate(session, existing, incoming_report(NEAR_B, channel="chan_c"), None)
    assert existing.source_reliability == 5


async def test_unnamed_channels_do_not_count_as_distinct(session):
    """8523961 / models.py. telegram.py writes "" when a chat has neither
    username nor title. Two unnamed channels cannot be shown to be distinct
    from each other, so they are excluded from the count."""
    existing = make_event(NEAR_A, source_reliability=4)
    session.add(existing)
    await session.flush()
    await _seed_reports(session, existing, ["", ""])

    await merge_duplicate(session, existing, incoming_report(NEAR_B, channel="chan_c"), None)
    assert existing.source_reliability == 4


async def test_an_unknown_source_name_does_not_collapse_a_stored_reliability(session):
    """8523961 / C11. The base stays max(current, incoming) and is
    deliberately NOT recomputed from the report rows: get_reliability knows
    the Telegram registry only and returns None — hence 1 — for every RSS
    source name, so recomputing would collapse a feed that news_feeds scored
    4 at insert down to 1 on its first merge."""
    existing = make_event(NEAR_A, source_reliability=4)
    session.add(existing)
    await session.flush()

    await merge_duplicate(
        session, existing, incoming_report(NEAR_B, channel="Al Jazeera English"), None
    )
    assert existing.source_reliability == 4


async def test_the_merge_leaves_the_event_row_otherwise_alone(session):
    """89c6f54. There is no coordinate backfill in merge_duplicate any more.
    The removed branch wrote lat/lon while leaving geometry, is_geolocated and
    the geo_* columns untouched — a located row claiming nothing about how it
    was located, breaking the `geometry IS NULL <=> lat IS NULL` partition
    check_duplicate now relies on."""
    existing = make_event(NEAR_A)
    session.add(existing)
    await session.flush()

    await merge_duplicate(session, existing, incoming_report(NEAR_B), None)

    refreshed = (
        await session.execute(select(Event).where(Event.id == existing.id))
    ).scalar_one()
    assert refreshed.lat is None and refreshed.lon is None
    assert refreshed.geometry is None
