"""evidence_span's contract: it is a SEARCH, never an argument, and its three
values are three different statements. Protects *this commit*.

killed_reported earned its place by being "a quantity a reader can check against
raw_text in one second". A location had no such handle: `location_name` is
whatever the classifier said, and a model naming a plausible town that appears
nowhere in the message produced a row indistinguishable from a quoted one. These
tests pin the property that makes the difference visible.
"""

import pytest

from app.services.classifier import (
    _extract_flags,
    _regex_location_fallback,
    evidence_span,
)


# ── the invariant: whatever comes out is IN raw_text ─────────────────────────


QUOTED = [
    ("An Israeli airstrike on Zrariyeh killed 17 people.", "Zrariyeh"),
    ("Explosions reported in Tehran tonight", "Tehran"),
    ("Strike on the Natanz enrichment facility", "Natanz"),
    ("Sirens across Tel Aviv and Ramat Gan", "Tel Aviv"),
]


@pytest.mark.parametrize("raw,loc", QUOTED)
def test_a_quoted_location_yields_the_words_from_the_text(raw, loc):
    """*this commit*. The whole point of the column: the value is a slice of
    the raw_text sitting beside it, so checking it costs one `in`."""
    span = evidence_span(raw, loc)
    assert span, "a location plainly in the text produced no span"
    assert span in raw


@pytest.mark.parametrize(
    "raw,loc",
    QUOTED
    + [
        # Not quoted, for one honest reason each. All must still satisfy the
        # invariant, because "" is in every string.
        ("UPDATE: Over 200 Israelis injured in last 24 hours", "Israel"),
        ("\U0001F1EE\U0001F1F7 Oil prices drop after the remarks.", "Iran"),
        ("Israel issues evacuation threats for Beirut's southern suburb", "Dahieh"),
        ("", "Tehran"),
        ("Explosions reported", ""),
    ],
)
def test_the_value_is_always_a_substring_of_the_text_it_came_from(raw, loc):
    """*this commit*. Stated as a property over both branches rather than as
    two examples: there is no input for which this column may hold text that
    is not in raw_text. That is what stops it becoming somewhere a guess can
    be written."""
    assert evidence_span(raw, loc) in raw


# ── the length-drift bug this implementation exists to avoid ─────────────────


def test_the_slice_is_taken_from_the_original_text_not_a_lowered_copy():
    """*this commit*. `'\\u0130'.lower()` is TWO characters, so a lowered copy
    of a message containing a Turkish dotted-I is one character longer than
    the original and every offset past it is shifted. An implementation that
    searched the copy and sliced the original would store 'rariyeh ' here — a
    genuine substring of raw_text that is not the place, which is precisely
    the lie this column exists to prevent, committed by the column itself.

    One message in the 2026-08-18 archive drifts this way, which is why this
    is a regression test and not a hypothetical."""
    raw = "İzmir desk: strike on Zrariyeh overnight"
    assert len(raw.lower()) == len(raw) + 1, "the premise of this test is gone"
    assert evidence_span(raw, "Zrariyeh") == "Zrariyeh"


def test_the_span_carries_the_source_s_own_capitalisation():
    """*this commit*. The match is case-insensitive, so the quote can differ
    from location_name — ~3,864 archive rows, 10.9% of quotes, do. The column
    reports what the source wrote, not what the classifier normalised it to."""
    raw = "Rockets fell across SOUTHERN LEBANON overnight"
    assert evidence_span(raw, "southern Lebanon") == "SOUTHERN LEBANON"


# ── word-anchored, for the reason the rest of the file already is ────────────


def test_a_name_inside_a_longer_word_is_not_a_quote():
    """*this commit*, and the same defect as 075ce6f. 'Kirya' is IDF HQ in Tel
    Aviv and sits inside 'Kiryat Shemona' on the Lebanese border. A substring
    test would call the border town evidence for Tel Aviv."""
    assert evidence_span("Shelling near Kiryat Shemona", "Kirya") == ""


# ── the three values are three statements ───────────────────────────────────


def test_an_unquoted_location_is_empty_string_and_not_none():
    """*this commit*. "" means something looked and this text does not spell
    this place; NULL means nothing looked. The function never returns None,
    so a NULL in the column can only come from a writer that never called it
    — which is what makes NULL a bug detector rather than a data state."""
    span = evidence_span("Over 200 Israelis injured in last 24 hours", "Israel")
    assert span == ""
    assert span is not None


# ── the provenances the roadmap listed, measured rather than declared ────────


def test_the_regex_fallback_provenance_always_produces_a_quote():
    """*this commit*. _regex_location_fallback finds its answer BY searching
    raw_text, so a location from that path is grounded by construction and
    must always carry a span. The column is not told this — it measures it,
    which is why the two functions are tested against each other here."""
    raw = "Explosions reported in Tehran tonight"
    loc = _regex_location_fallback(raw)
    assert loc == "Tehran"
    assert evidence_span(raw, loc) == "Tehran"


def test_the_flag_fallback_provenance_never_produces_a_quote():
    """*this commit*. The flag path reads an emoji, not words: a message
    carrying \U0001F1EE\U0001F1F7 and never naming Iran yields "Iran" as the
    location and "" as the span. That is the honest pair — the place is not
    quotable from this text — and it is the contrast that makes the quoted
    case mean something."""
    raw = "\U0001F1EE\U0001F1F7 Oil prices are dropping following the remarks."
    assert _extract_flags(raw) == ["Iran"]
    assert evidence_span(raw, "Iran") == ""


def test_a_qualifier_the_text_does_not_carry_costs_the_whole_span():
    """*this commit*. One rule, not two: the span quotes location_name or it
    quotes nothing. "Qasamia Bridge, southern Lebanon" against a message
    reading "the Qasamia Bridge in southern Lebanon" is NOT quoted, and gets
    "". Measured at 2.3% of place-naming archive rows — the price of refusing
    to score partial support, which is a judgment no reader could check."""
    raw = "The army is evacuating positions around the Qasamia Bridge in southern Lebanon"
    assert evidence_span(raw, "Qasamia Bridge, southern Lebanon") == ""
    # The head alone is quoted, and the column deliberately does not say so.
    assert evidence_span(raw, "Qasamia Bridge") == "Qasamia Bridge"


def test_the_span_answers_for_location_name_not_for_the_coordinate():
    """*this commit*. geo_method already records which branch produced the
    COORDINATE. This column answers the link upstream of it — whether
    location_name is in the message at all — so when the geocoder resolves
    "airstrike near Natanz overnight" by partial-matching the key 'natanz',
    the span is still the whole location_name as the text spells it, not the
    table key the matcher happened to use."""
    raw = "Reports of an airstrike near Natanz overnight"
    assert evidence_span(raw, "airstrike near Natanz overnight") == (
        "airstrike near Natanz overnight"
    )


# ── a value that is not a place has nothing to quote ────────────────────────


@pytest.mark.parametrize(
    "raw,loc",
    [
        # Each sentinel is an ordinary word in conflict reporting, so each one
        # found itself in a real message and was written into the column as
        # though it were evidence of a location.
        ("Casualty figures remain unknown after the blast.", "Unknown"),
        ("The IDF confirmed the strike hit its target.", "IDF"),
        ("Multiple explosions were heard overnight.", "multiple"),
        ("Various reports are circulating about the site.", "various"),
        ("NATO said it was monitoring the situation.", "NATO"),
    ],
)
def test_a_could_not_tell_sentinel_is_never_quoted(raw, loc):
    """*this commit*, and this project's own bug class committed inside the new
    column. `location_name` "Unknown" is the classifier saying it could not
    tell; the word "unknown" appearing in the text is not evidence that it
    could. Writing state 3 into the field a reader reads as state 1 is the one
    thing this column exists to stop, and 60 rows of the 2026-08-18 archive did
    it — 55 on "unknown", 5 on "NATO" — before this test existed."""
    assert evidence_span(raw, loc) == ""


def test_the_sentinel_list_is_the_geocoder_s_own_one():
    """*this commit*. geocode() refuses these values at its front door; this
    refuses them at its own. One list, imported and not restated, because two
    copies would let a row carry a location claim quoted from its text that the
    geocoder will not resolve — the two columns disagreeing about whether a
    place was named at all."""
    from app.services.classifier import _NOT_A_PLACE as from_classifier
    from app.services.geocoder import _NOT_A_PLACE as from_geocoder

    assert from_classifier is from_geocoder


def test_a_real_place_still_quotes_when_the_text_also_says_unknown():
    """*this commit*. The guard is on location_name, not on raw_text: a message
    whose casualty count is unknown still names Zrariyeh, and the span is that
    name. Without this the fix above would be a mute button."""
    raw = "Strike on Zrariyeh; casualties remain unknown."
    assert evidence_span(raw, "Zrariyeh") == "Zrariyeh"
