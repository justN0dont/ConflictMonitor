"""The similarity score behind every merge decision. Protects ce5d994 / C74.

C74 was a merge at sim=0.44 against a `> 0.4` bar. The bar is strict, and one
line here says so; the DB half of that claim is in test_check_duplicate.py.
"""

from app.services.dedup import _jaccard, _normalize


def test_normalize_drops_short_words_and_punctuation():
    """ce5d994 / C74. The score is whatever this function leaves behind, so
    what it drops is part of the threshold's meaning."""
    assert _normalize("Israeli F-35s hit Natanz!") == {"israeli", "f35s", "hit", "natanz"}
    assert _normalize("a an of to") == set()


def test_jaccard_of_an_empty_side_is_zero_not_one():
    """ce5d994 / C74. Two messages with no scoreable words are not the same
    event; 1.0 here would merge every short message into the first one."""
    assert _jaccard(set(), set()) == 0.0
    assert _jaccard({"natanz"}, set()) == 0.0


def test_jaccard_of_identical_text_is_one():
    assert _jaccard({"natanz", "strike"}, {"strike", "natanz"}) == 1.0


# These two strings score EXACTLY 0.4 (2 shared words over a union of 5). The
# bar is `> 0.4`, so they are not a duplicate — the database half of this
# claim is test_exactly_the_threshold_is_not_a_duplicate.
BAR_A = "aaa bbb ccc"
BAR_B = "bbb ccc ddd eee"


def test_the_threshold_pair_really_scores_exactly_the_threshold():
    """ce5d994 / C74. Pins the fixture itself: if _normalize ever changes,
    this fails loudly instead of the DB test quietly stopping to test the
    boundary."""
    assert _jaccard(_normalize(BAR_A), _normalize(BAR_B)) == 0.4
