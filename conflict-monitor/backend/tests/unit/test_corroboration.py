"""Who said it, counted once per outlet. Protects C84 and the C11 narrowing.

Pure: rows are (channel, forwarded_from) pairs as event_reports holds them, and
forward_origin is fed the shape Telethon gives a message, not Telethon.
"""

from app.services.corroboration import corroboration, origin, outlet, outlets
from app.services.telegram import forward_origin


# ── outlets ──────────────────────────────────────────────────────────────────


def test_one_outlet_on_two_transports_is_one_outlet():
    """C11 / C78: the archive's own pairs, which the distinct-channel count
    scored as two each."""
    assert outlet("x_osintwarfare") == outlet("OSINTWarfare") == "osintwarfare"
    assert outlet("x_osint613") == outlet("osint613")
    assert outlet("x_geoconfirmed") == outlet("GeoConfirmed")


def test_two_outlets_stay_two():
    assert outlet("osint613") != outlet("OSINTWarfare")
    assert outlets([("osint613", None), ("OSINTWarfare", None)]) == 2


def test_a_forward_is_its_origin_s_words():
    assert origin("reposter", "OSINTWarfare") == "OSINTWarfare"
    # "" is Telegram saying "not a forward"; NULL is a row that never recorded
    # the header. Both are the channel's own words.
    assert origin("reposter", "") == "reposter"
    assert origin("reposter", None) == "reposter"


def test_fifteen_channels_forwarding_one_post_are_one_outlet():
    """C11: '15 channels reposting one text still read as 15 confirmations'.
    The forward header is where Telegram says fourteen of them did not."""
    rows = [("origin_ch", "")] + [(f"reposter_{i:02d}", "origin_ch") for i in range(14)]
    assert outlets(rows) == 1


def test_forwards_from_a_channel_the_monitor_does_not_read_count_once():
    assert outlets([("reposter_a", "elsewhere"), ("reposter_b", "elsewhere")]) == 1


def test_a_forward_and_its_origin_s_mirror_collapse_together():
    assert outlets([("x_osintwarfare", None), ("reposter", "OSINTWarfare")]) == 1


def test_unnamed_origins_are_not_distinct():
    assert outlets([("", None), ("", "")]) == 0


# ── corroboration ────────────────────────────────────────────────────────────


def test_every_count_is_a_floor_over_the_rows():
    c = corroboration([("OSINTWarfare", ""), ("x_osintwarfare", None), ("other", "")], report_count=3)
    assert (c.reports, c.channels, c.outlets, c.unrecorded) == (3, 3, 2, 0)


def test_unrecorded_is_what_the_merge_destroyed_and_never_negative():
    # The archive row that claims 214 with one backfilled row under it.
    assert corroboration([("a", None)], report_count=214).unrecorded == 213
    # The 480 archive rows whose report_count is 0: a value no writer produces.
    assert corroboration([("a", None)], report_count=0).unrecorded == 0
    # A row with no report rows at all: the count is all there is.
    assert corroboration([], report_count=1).unrecorded == 1
    assert corroboration([], report_count=None).unrecorded == 0


# ── forward_origin: Telegram's header as a name ──────────────────────────────


class _Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _fwd(**kw):
    header = dict(chat=None, chat_id=None, sender_id=None, from_name=None)
    header.update(kw)
    return _Obj(forward=_Obj(**header))


def test_not_a_forward_is_the_empty_string_not_null():
    """The empty string is Telegram's own statement; NULL is reserved for rows
    that never recorded the header."""
    assert forward_origin(_Obj(forward=None)) == ""


def test_a_channel_forward_is_named_by_username_then_title_then_peer_id():
    assert forward_origin(_fwd(chat=_Obj(username="OSINTWarfare", title="OSINT Warfare"), chat_id=-100123)) == "OSINTWarfare"
    assert forward_origin(_fwd(chat=_Obj(username=None, title="OSINT Warfare"), chat_id=-100123)) == "OSINT Warfare"
    assert forward_origin(_fwd(chat=None, chat_id=-100123)) == "id:-100123"


def test_a_forward_from_a_person_is_recorded_but_not_named():
    """The header is public, the person is not an outlet. Every such row
    collapses onto one origin, which is the under-count."""
    assert forward_origin(_fwd(sender_id=42)) == "user"
    assert forward_origin(_fwd(from_name="Some Person")) == "user"


def test_a_header_that_names_nothing_is_a_question_mark():
    assert forward_origin(_fwd()) == "?"
