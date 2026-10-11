"""The link rule, pure (services/linkrule.py). Protects the link between
events: the bar, the two vetoes, and the number the register states for them.

Every test is one claim the register makes about rule v1. The rule's measured
counts on the gold pairs are pinned by `tools/gold_set.py check` (a CI step),
which is the one reader of the gold set; the backend suite never opens it.
"""

from app.services import linkrule as lr
from app.services.dedup import _jaccard, _normalize

def test_the_rule_reads_the_same_words_as_dedup():
    """The two matchers must agree on what a word is, or a report can be a
    duplicate by one and a stranger by the other over punctuation."""
    for text in ("Israeli F-35s hit Natanz!", "a an of to", "Breaking | sirens in Eilat, 03:15"):
        assert lr.normalize(text) == _normalize(text)
    assert _jaccard(set(), set()) == 0.0  # the pin the other way: dedup is still there to agree with


def test_containment_is_the_shorter_text_s_share():
    assert lr.containment({"a", "b", "c"}, {"a", "b"}) == 1.0
    assert abs(lr.containment({"a", "b", "c"}, {"a", "x", "y"}) - 1 / 3) < 1e-9
    assert lr.containment(set(), {"a"}) == 0.0


def test_clean_text_strips_tags_and_urls():
    assert lr.clean_text("<p>Hi</p> https://x.co/abc there") == "Hi there"
    assert lr.clean_text(None) == ""


def test_unknown_is_not_a_name():
    """classifier.clean_location writes the literal "Unknown" for "I could not
    tell"; two of those are not agreement about a place."""
    assert lr.place_relation("Unknown", "Unknown") == "unknown"
    assert lr.place_relation("", "Beirut") == "unknown"
    assert lr.place_relation("unknown", "Beirut") == "unknown"
    assert lr.place_relation("Beirut", "southern Beirut") == "same"
    assert lr.place_relation("Al-Dahieh", "Dahieh") == "same"
    assert lr.place_relation("Lebanon", "Beirut") == "different"


def test_towns_reads_the_place_the_text_names_not_the_region():
    assert lr.towns("Israeli occupation forces storm the town of Beita, south of Nablus, in the occupied West Bank") == {"beita"}
    assert lr.towns("Breaking | Sirens sound in Eilat, southern occupied Palestine, following a drone") == {"eilat"}
    assert lr.towns("Mourners take part in the funeral procession in Khan Younis on Friday's events") == {"khan younis"}
    assert lr.towns("استهداف دبابة صهيونية في بلدة يحمر الشقيف جنوب لبنان.") == {"يحمر الشقيف"}
    # Nationalities, directions and the countries the stored name already
    # covers are not towns; a place word alone is nothing.
    assert lr.towns("Sirens sound across northern Israel after launches from Lebanon") == set()
    assert lr.towns("strike on Gaza City") == set()


def test_towns_relation_needs_both_sides_to_name_one():
    beita = "Israeli occupation forces storm the town of Beita, south of Nablus"
    sarra = "Israeli occupation forces storm a Palestinian home in the town of Sarra"
    assert lr.towns_relation(beita, sarra) == "different"
    assert lr.towns_relation(beita, "Israeli occupation forces storm Nablus") == "unknown"
    assert lr.towns_relation("shelling near Deir al-Balah", "an airstrike in Deir Al-Balah today") == "same"


# Two texts whose cleaned word sets give containment EXACTLY 0.4: the shorter
# holds five words, two of them shared. The bar is `> 0.4`, so this pair is
# not linked; one more shared word is.
AT_BAR_A = "alpha bravo charlie delta echo"
AT_BAR_B = "alpha bravo foxtrot golf hotel india juliet"
OVER_BAR_B = "alpha bravo charlie golf hotel india juliet"


def test_the_bar_is_strict():
    assert lr.containment(lr.normalize(AT_BAR_A), lr.normalize(AT_BAR_B)) == 0.4
    assert not lr.assess(AT_BAR_A, "", AT_BAR_B, "").linked
    over = lr.assess(AT_BAR_A, "", OVER_BAR_B, "")
    assert over.linked and over.score == 0.6 and over.place == "unknown" and over.towns == "unknown"


def test_a_different_stored_place_vetoes_whatever_the_text_says():
    a = lr.assess(AT_BAR_A, "Tyre", OVER_BAR_B, "Damascus")
    assert a.place == "different" and not a.linked
    assert lr.assess(AT_BAR_A, "Tyre", OVER_BAR_B, "southern Tyre").linked


def test_a_different_town_in_the_text_vetoes_the_same_template():
    """The false positive a text score cannot see: one outlet's template with
    another town in it, under one region label."""
    beita = "Israeli occupation forces storm the town of Beita, south of Nablus, in the occupied West Bank"
    sarra = "Israeli occupation forces storm the town of Sarra, west of Nablus, in the occupied West Bank"
    a = lr.assess(beita, "West Bank", sarra, "West Bank")
    assert a.score > lr.BAR and a.place == "same" and a.towns == "different" and not a.linked


def test_the_window_is_the_gold_pairs_population():
    assert lr.within_window(180.0, None) and lr.within_window(-180.0, 500.0)
    assert not lr.within_window(180.1, None) and not lr.within_window(10.0, 500.1)

