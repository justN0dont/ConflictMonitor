"""The noise gate. Protects 89c6f54: unmeasured is not low.

severity became nullable in 89c6f54, and every comparison against it had to
learn that None is not a number. This one decides whether an event is written
at all, so getting it wrong deletes the row: state 3 ("I looked and could not
tell") silently filed as state 1 ("nothing happened").
"""

from app.services.telegram import MIN_SEVERITY, _is_noise

# Long enough and wordy enough to clear the text rules, so the only variable
# below is the severity argument.
_REAL_EVENT = (
    "Israeli aircraft struck the Natanz enrichment facility overnight, "
    "according to two separate channels reporting from the area."
)


def test_an_unmeasured_severity_does_not_drop_a_real_event():
    """89c6f54. severity=None means it was never measured. That is not a low
    score, so it must not trip the MIN_SEVERITY gate — an otherwise-fine
    event is never dropped for a number we failed to obtain."""
    assert _is_noise(_REAL_EVENT, None) is False


def test_a_measured_low_severity_still_drops():
    """89c6f54, the other half: the gate still works on real numbers, so the
    test above is about None and not about the gate being dead."""
    assert _is_noise(_REAL_EVENT, MIN_SEVERITY - 1) is True
    assert _is_noise(_REAL_EVENT, MIN_SEVERITY) is False


def test_the_text_rules_still_apply_when_severity_is_unmeasured():
    """89c6f54. None disables the severity gate only. A two-word reaction is
    still noise on its own text."""
    assert _is_noise("lol", None) is True
    assert _is_noise("Wait I thought the war was over already, what happened?", None) is True
