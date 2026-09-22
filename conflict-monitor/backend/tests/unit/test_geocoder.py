"""Geocoder contracts: an unresolvable place is None, and a bound nobody
measured is None. Protects 075ce6f and 89c6f54."""

import pytest

from app.services import geocoder
from app.services.geocoder import (
    PRECISION_ADMIN1,
    PRECISION_CITY,
    PRECISION_COUNTRY,
    PRECISION_FACILITY,
)


@pytest.fixture
def nominatim_calls(monkeypatch):
    """Replace the banned _query_nominatim with a recorder that resolves nothing.

    Every word-anchoring test asserts the fall-through was REACHED, not merely
    that the answer was not Arak — otherwise a future bug that returned None
    early would pass these tests.
    """
    calls: list[str] = []

    async def fake(location_name):
        calls.append(location_name)
        return None

    monkeypatch.setattr(geocoder, "_query_nominatim", fake)
    return calls


# ── 075ce6f: word-anchored matching ──────────────────────────────────────────
# All three keys are real table entries and none of the three inputs is, so
# each one genuinely reaches the partial-match loop where the substring test
# used to fire.


async def test_maarakeh_is_not_arak(nominatim_calls):
    """075ce6f. 'arak' (34.0975, 49.1947) sits inside 'Maarakeh' as a
    substring: south Lebanon resolved to central Iran, 1,100km away."""
    assert await geocoder.geocode("Maarakeh") is None
    assert nominatim_calls == ["Maarakeh"], "partial match swallowed it again"


async def test_kiryat_shemona_is_not_kirya(nominatim_calls):
    """075ce6f. 'kirya' is IDF HQ in Tel Aviv; Kiryat Shemona is on the
    Lebanese border. The substring test put border shelling in Tel Aviv."""
    assert await geocoder.geocode("Kiryat Shemona") is None
    assert nominatim_calls == ["Kiryat Shemona"]


async def test_romania_is_not_oman(nominatim_calls):
    """075ce6f. 'oman' (22.0, 57.0) sits inside 'Romania'."""
    assert await geocoder.geocode("Romania") is None
    assert nominatim_calls == ["Romania"]


@pytest.mark.parametrize("actor", ["IDF", "IRGC", "Hezbollah", "CENTCOM"])
async def test_an_actor_is_not_a_place_and_is_never_looked_up(actor, nominatim_calls):
    """075ce6f. A bare acronym fell through to Nominatim, which answers
    confidently and wrongly — 'IDF' resolved to (40.18, 44.51), in Armenia.
    The honest answer is None, and it must not cost a lookup to get there."""
    assert await geocoder.geocode(actor) is None
    assert nominatim_calls == [], "an actor reached the geocoding API"


@pytest.mark.parametrize("empty", ["", "   ", "Unknown", "various", "multiple"])
async def test_a_non_place_resolves_to_none_not_a_coordinate(empty, nominatim_calls):
    """89c6f54. None means 'not a place, or unresolved'. There is no
    placeholder coordinate for it any more — the old (-25, 80) sentinel put
    47.6% of the archive on a real point in open water."""
    assert await geocoder.geocode(empty) is None
    assert nominatim_calls == []


# ── 89c6f54: an unmeasured bound is None, never a borrowed number ────────────


@pytest.mark.parametrize(
    "bbox",
    [None, [], ["1", "2", "3"], ["a", "b", "c", "d"], [1, 2, 3, 4, 5]],
    ids=["missing", "empty", "too-short", "non-numeric", "too-long"],
)
def test_a_reply_with_no_usable_bbox_reports_uncertainty_as_none(bbox):
    """89c6f54. This used to borrow 500km from the table-tier estimate map —
    a number nobody measured, in the one field whose purpose is to carry a
    measured bound. A measured US bbox is +/-18,267km, 36x larger."""
    precision, uncertainty = geocoder._precision_from_bbox(bbox)
    assert precision == PRECISION_COUNTRY
    assert uncertainty is None


def test_a_measured_bbox_produces_a_measured_bound():
    """89c6f54, the other half: when the extent IS present the number is
    computed from it, so the None above is a statement and not a default."""
    precision, uncertainty = geocoder._precision_from_bbox(
        ["32.0", "32.1", "34.7", "34.8"]
    )
    assert uncertainty is not None and uncertainty > 0
    assert precision in (PRECISION_CITY, PRECISION_ADMIN1)


# ── 89c6f54: a declared area tier beats one inferred from a shared point ─────


@pytest.mark.parametrize("key", ["nineveh", "baalbek district"])
def test_a_governorate_does_not_inherit_its_city_tier(key):
    """89c6f54. 'nineveh' is tabled on Mosul's coordinate and 'baalbek
    district' on Baalbek's, so the most-precise-vote rule handed a
    governorate the city's +/-10km. An _AREA_KEYS entry is a statement about
    what the NAME covers and wins over an inference."""
    coords = geocoder.KNOWN_LOCATIONS[key]
    assert geocoder._precision_for_key(key, coords) == PRECISION_ADMIN1


def test_a_partial_match_drops_exactly_one_tier():
    """075ce6f. 'strike near Natanz' names a place the event was NEAR, not
    the place itself, so it may not claim the facility tier an exact hit
    claims."""
    exact = geocoder._precision_for_key("natanz", geocoder.KNOWN_LOCATIONS["natanz"])
    assert exact == PRECISION_FACILITY
    assert geocoder._COARSER[PRECISION_FACILITY] == PRECISION_CITY


async def test_a_partial_match_says_so_in_its_method_and_tier():
    """075ce6f, end to end: the coarsening above is actually applied, and the
    row records that it was a partial match rather than an exact one."""
    result = await geocoder.geocode("airstrike near Natanz overnight")
    assert result is not None
    assert (result.lat, result.lon) == geocoder.KNOWN_LOCATIONS["natanz"]
    assert result.precision == PRECISION_CITY
    assert result.method == "partial"


# ── *this commit*: the 2026-09-21 gazetteer batch ────────────────────────────
# Each entry was picked by event volume from the archive and resolved through
# _query_nominatim on 2026-09-21; the block above KNOWN_LOCATIONS records the
# query sent and the anchor it was checked against. These tests pin the two
# things a new row can get wrong: the tier it claims, and what else it starts
# capturing.


@pytest.mark.parametrize(
    "name,coords,precision",
    [
        ("Karaj", (35.8225, 50.9905), PRECISION_CITY),
        ("Arad", (31.2612, 35.2146), PRECISION_CITY),
        ("Beit Shemesh", (31.7462, 34.9887), PRECISION_CITY),
        ("Beersheba", (31.2518, 34.7913), PRECISION_CITY),
        ("Al-Khiyam", (33.3440, 35.5980), PRECISION_CITY),
        ("Galilee", (32.8008, 35.5890), PRECISION_ADMIN1),
        ("Upper Galilee", (32.8008, 35.5890), PRECISION_ADMIN1),
        ("Fujairah", (25.4147, 56.2314), PRECISION_ADMIN1),
        ("Al-Aqsa Mosque", (31.7763, 35.2356), PRECISION_FACILITY),
        ("Ben Gurion Airport", (32.0027, 34.8809), PRECISION_FACILITY),
        ("Prince Sultan Air Base", (24.0620, 47.5810), PRECISION_FACILITY),
    ],
)
async def test_a_new_entry_resolves_offline_at_the_tier_it_earned(
    name, coords, precision, nominatim_calls
):
    """*this commit*. 13,424 archive events (16.0%) miss the table and reach
    Nominatim on every one of them, forever. These names are the ones carrying
    the most events, and the assertion on `nominatim_calls` is the half that
    matters: an entry that resolves but still costs a lookup has bought
    nothing."""
    result = await geocoder.geocode(name)
    assert result is not None
    assert (result.lat, result.lon) == coords
    assert result.precision == precision
    assert result.method == "table-exact"
    assert nominatim_calls == [], "a tabled name still reached the network"


@pytest.mark.parametrize(
    "name,precision,uncertainty_m",
    [
        # 138,000 km2 governorate: measured half-diagonal 352km against
        # admin1's 100km estimate, 3.5x narrow.
        ("Anbar", PRECISION_ADMIN1, 352_000),
        # A built facility, measured at 3,265m against the tier's 500m.
        ("Ben Gurion Airport", PRECISION_FACILITY, 3_265),
    ],
)
async def test_a_measured_extent_beats_its_tier_estimate_without_moving_the_tier(
    name, precision, uncertainty_m
):
    """*this commit*, and it replaces a test that pinned the defect in place.

    _TIER_UNCERTAINTY_M holds estimates, so where a table hit replaces a
    measured bound the estimate must err WIDE — and for these two it errs
    narrow. The first fix was to give Anbar the country tier and borrow its
    500km, which bought the number by lying about the category: geo_precision
    is rendered to the reader as "country-level", so every Anbar event then
    reported that an Iraqi governorate had been resolved only as far as a
    country, and inflated the country-level rollup.

    Both assertions together are the point. The tier answers WHAT KIND of place
    was named, the bound answers HOW WIDE the answer is, and neither may be
    moved to pay for the other."""
    result = await geocoder.geocode(name)
    assert result is not None
    assert result.precision == precision
    assert result.uncertainty_m == uncertainty_m


@pytest.mark.parametrize(
    "name",
    ["Muradiye", "Anbarabad", "Ben Gurion University", "Al-Aqsa Martyrs Brigades"],
)
async def test_a_new_key_does_not_capture_a_name_it_was_never_meant_for(
    name, nominatim_calls
):
    """*this commit*. The risk every short key carries, and the one word
    boundaries narrow without removing: 'arad' sits inside 'Muradiye' and
    'anbar' inside 'Anbarabad'. The other two are the reason bare 'ben gurion'
    and bare 'al-aqsa' are deliberately NOT keys — Ben-Gurion University is in
    Beer Sheva, 70km from the runway, and the Al-Aqsa Martyrs Brigades are an
    actor, which the foot of KNOWN_LOCATIONS explains at length.

    Verified against the archive before it shipped as well: replaying the old
    and new tables over all 3,665 distinct location_name values changed 89 of
    them, 472 events, with no name captured by a key that did not mean it."""
    assert await geocoder.geocode(name) is None
    assert nominatim_calls == [name], "a new key swallowed it"


@pytest.mark.parametrize(
    "name,coords",
    [
        ("Ben Gurion Airport, Tel Aviv", (32.0027, 34.8809)),
        ("Al-Aqsa Mosque, Jerusalem", (31.7763, 35.2356)),
    ],
)
async def test_a_qualified_facility_no_longer_answers_as_its_city(name, coords):
    """*this commit*. Measured on the archive: with no key of its own, "Ben
    Gurion Airport, Tel Aviv" partial-matched 'tel aviv' and answered with the
    city centre 13km from the runway, and 43 of the 59 "Al-Aqsa Mosque" events
    answered as 'jerusalem'. Longest-key-wins does the rest once the facility
    is in the table — these were never Nominatim failures, and a table entry is
    what stops the hijack."""
    result = await geocoder.geocode(name)
    assert result is not None
    assert (result.lat, result.lon) == coords
    # Still a partial match, so it still drops a tier: the event was near the
    # named facility, not necessarily at it.
    assert result.method == "partial"
    assert result.precision == PRECISION_CITY
