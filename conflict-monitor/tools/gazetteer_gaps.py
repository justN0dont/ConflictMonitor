"""Rank the location names the gazetteer misses, by EVENT volume.

geocoder_vs_archive.py answers "how much of the archive resolves offline"; it
replays the geocoder as it stood before word boundaries shipped, and its reject
list is the five-item tuple that _NOT_A_PLACE replaced. This script answers a
different question — WHICH NAMES SHOULD THE TABLE LEARN NEXT — and so it has to
replay geocode() as it is today, or the shortlist would be picked against code
that no longer runs.

Distinct strings are the wrong unit. Forty spellings on one event each are worth
less than one name on 4,970, and the table costs the same either way, so every
number printed here is an event count.

It measures and does not judge. Deciding that "Iran" is a country centroid the
table must NOT learn (C6) and that "Unknown" is not a place at all is a reading
of this output, not an output of this script — there is no list of countries
hardcoded below, because a wrong one would quietly drop a real town.

Needs locations.tsv from archive_locations.py:
    python tools/archive_locations.py <archive.sql.gz> locations.tsv
    python tools/gazetteer_gaps.py locations.tsv
"""
import ast, io, os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
GEO = os.path.join(HERE, "..", "backend", "app", "services", "geocoder.py")

# The coordinate the pre-migration fallback wrote when it had no answer. A name
# stored here did NOT resolve in production, whatever path it took.
SENTINEL = (-25.0, 80.0)

# _VIEWBOX from geocoder.py, as lat/lon bounds. Nominatim is passed this box
# with bounded=0, so it is a preference and not a filter, and an answer can land
# anywhere on earth. "outside" below is the weakest honest claim available
# without a second network call: the point production stored is not in the
# theatre this monitor covers. It is evidence a name resolved WRONG, not proof —
# "London" is outside the box and correctly so.
LAT_MIN, LAT_MAX, LON_MIN, LON_MAX = 8.0, 42.0, 25.0, 70.0


def grab(src, varname):
    """Pull a literal out of the source without importing the module.

    Brace-matches from the first '{' after the name, so it reads both the plain
    dict tables and the frozenset({...}) call that wraps _NOT_A_PLACE.
    """
    m = re.search(re.escape(varname) + r"\s*(:[^=]*)?=", src)
    if not m:
        raise SystemExit("%s not found in %s" % (varname, GEO))
    i = src.index("{", m.end())
    depth, j = 0, i
    while True:
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
        if depth == 0:
            break
        j += 1
    return ast.literal_eval(src[i:j + 1])


src = io.open(GEO, encoding="utf-8", errors="replace").read()
KNOWN = grab(src, "KNOWN_LOCATIONS")
DIRECT = grab(src, "_DIRECTIONAL_REGIONS")
NOT_A_PLACE = grab(src, "_NOT_A_PLACE")

# Same construction as _KNOWN_PATTERNS / _DIRECTIONAL_PATTERNS in geocoder.py:
# whole-word, compiled once, longest key wins.
KNOWN_PAT = [(k, re.compile(r"\b" + re.escape(k) + r"\b")) for k in KNOWN]
DIRECT_PAT = [(k, re.compile(r"\b" + re.escape(k) + r"\b")) for k in DIRECT]


def resolve(name):
    """Replay geocode() steps 2-4 and report which one answers.

    Stops at step 5: Nominatim is a network call and this phase makes none.
    "nominatim" below therefore means "the table hands this name to the network",
    not "the network answers it" — those are different claims and only the first
    one is measured here.
    """
    if not name or name.strip().lower() in NOT_A_PLACE:
        return "not-a-place", None
    n = name.strip().lower()
    if n in DIRECT:
        return "directional-exact", n
    if n in KNOWN:
        return "table-exact", n
    best, blen = None, 0
    for table in (KNOWN_PAT, DIRECT_PAT):
        for k, pat in table:
            if len(k) > blen and pat.search(n):
                best, blen = k, len(k)
        if best:
            break
    if best:
        return "partial", best
    return "nominatim", None


def main(tsv, depth):
    rows = []
    with io.open(tsv, encoding="utf-8") as fh:
        next(fh, None)
        for line in fh:
            p = line.rstrip("\n").split("\t")
            if len(p) < 5:
                continue
            cnt, loc, lat, lon, ndist = int(p[0]), p[1], p[2], p[3], int(p[4])
            try:
                prod = (float(lat), float(lon))
            except ValueError:
                prod = None
            if prod is None:
                stored = "NULL"
            elif abs(prod[0] - SENTINEL[0]) < .01 and abs(prod[1] - SENTINEL[1]) < .01:
                stored = "sentinel"
            elif not (LAT_MIN <= prod[0] <= LAT_MAX and LON_MIN <= prod[1] <= LON_MAX):
                stored = "%.3f,%.3f OUT" % prod
            else:
                stored = "%.3f,%.3f" % prod
            how, via = resolve(loc)
            rows.append((cnt, loc, how, via, stored, ndist))

    tot = sum(r[0] for r in rows)
    print("KNOWN_LOCATIONS: %d   _DIRECTIONAL_REGIONS: %d   _NOT_A_PLACE: %d"
          % (len(KNOWN), len(DIRECT), len(NOT_A_PLACE)))
    print("distinct location_name values: %d   events: %d\n" % (len(rows), tot))

    print("CURRENT resolution path, by event volume:")
    buckets = {}
    for cnt, _, how, _, _, _ in rows:
        buckets[how] = buckets.get(how, 0) + cnt
    for k, v in sorted(buckets.items(), key=lambda kv: -kv[1]):
        print("   %-18s %7d (%4.1f%%)   [%d distinct strings]"
              % (k, v, 100.0 * v / tot, sum(1 for r in rows if r[2] == k)))

    for label, want in (
        ("MISSES THE TABLE ENTIRELY -> goes to Nominatim, every event, forever", "nominatim"),
        ("RESOLVES ONLY BY PARTIAL MATCH -> answers with a DIFFERENT name's point", "partial"),
    ):
        sel = sorted([r for r in rows if r[2] == want], reverse=True)
        sub = sum(r[0] for r in sel)
        print("\n%s\n%s" % (label, "=" * len(label)))
        print("  %d strings, %d events (%.1f%% of archive)\n" % (len(sel), sub, 100.0 * sub / tot))
        # cum% is against this bucket, not the archive: it answers "how far down
        # the list is worth typing", which is where the cut has to fall.
        print("  %4s %7s %6s  %-40s %-20s %-19s %s"
              % ("#", "events", "cum%", "location_name", "matched", "prod stored", "coords"))
        run = 0
        for i, (cnt, loc, how, via, stored, ndist) in enumerate(sel[:depth], 1):
            run += cnt
            print("  %4d %7d %5.1f%%  %-40s %-20s %-19s %d"
                  % (i, cnt, 100.0 * run / sub, loc[:40], (via or "-")[:20], stored, ndist))
        if len(sel) > depth:
            print("  ... %d more strings, %d further events (mean %.1f events/string)"
                  % (len(sel) - depth, sub - run, (sub - run) / float(len(sel) - depth)))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "locations.tsv",
         int(sys.argv[2]) if len(sys.argv) > 2 else 60)
