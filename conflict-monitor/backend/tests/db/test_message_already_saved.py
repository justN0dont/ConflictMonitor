"""The ingest guard. Protects 8523961.

Two defects, one rewrite. The query read `events`, so a MERGED message — which
writes no events row at all — was invisible to it: on every restart the
message came round again, was re-classified on the GPU, and was re-merged,
incrementing report_count for a report already counted. And it keyed on the
message id alone, but Telegram ids are per-channel, so message 4821 from one
channel blocked 4821 from another: a genuinely new message dropped with
"already saved" in the log. Absent written as present.
"""

from app.models import EventReport
from app.services.telegram import _message_already_saved
from tests.db.conftest import make_event


async def test_the_same_id_on_a_different_channel_is_not_already_saved(session):
    """8523961. The archive cannot show the drops directly — by construction
    the dropped ones are the rows that are missing. What it shows is five
    channel pairs whose id ranges overlap heavily and hold ZERO ids in
    common, where independence predicts on the order of 2,200 collisions:
    ~4.5% of 49,369 Telegram messages."""
    event = make_event("Strike reported")
    session.add(event)
    await session.flush()
    session.add(
        EventReport(event_id=event.id, channel="channel_a", telegram_message_id=4821)
    )
    await session.flush()

    assert await _message_already_saved(session, "channel_a", 4821) is True
    assert await _message_already_saved(session, "channel_b", 4821) is False


async def test_a_merged_message_is_found_even_though_it_has_no_event_row(session):
    """8523961 / C10. This is the case the old query could not see. The
    report row belongs to an event it was merged INTO; there is no events row
    carrying this message id anywhere."""
    host = make_event("Strike reported")
    session.add(host)
    await session.flush()
    # The host's own report, plus a second report merged onto it. Only the
    # second one's id is under test, and no events row carries it.
    session.add(
        EventReport(event_id=host.id, channel="channel_a", telegram_message_id=100)
    )
    session.add(
        EventReport(event_id=host.id, channel="channel_b", telegram_message_id=777)
    )
    await session.flush()

    assert await _message_already_saved(session, "channel_b", 777) is True


async def test_an_unseen_message_is_not_already_saved(session):
    """8523961. The positive control for the guard being a guard: with no
    matching row it returns False, so the two tests above are about the key
    and not about a function that always answers True."""
    assert await _message_already_saved(session, "channel_a", 4821) is False
