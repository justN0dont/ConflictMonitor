"""Simulate geocoder.py against every location string in the production archive.

Reads the tables straight out of ../backend/app/services/geocoder.py (no import, so no deps),
replays the resolution order offline, and reports:
  - coverage: how much of the archive resolves without an API call
  - the effect of adding word boundaries to the partial match

Needs locations.tsv from archive_locations.py:
    scp tools/archive_locations.py truthevades:/tmp/
    ssh truthevades 'python3 /tmp/archive_locations.py'
    scp truthevades:/tmp/locations.tsv .
    python tools/geocoder_vs_archive.py locations.tsv
"""
import ast, io, math, os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
GEO = os.path.join(HERE, "..", "backend", "app", "services", "geocoder.py")
SENTINEL = (-25.0, 80.0)


def grab(src, varname):
    """Pull a dict literal out of the source without importing the module."""
    m = re.search(varname + r"[^=]*=\s*\{", src)
    if not m:
        raise SystemExit(f"{varname} not found in {GEO}")
    i = src.index("{", m.start())
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
WORD = {k: re.compile(r"\b" + re.escape(k) + r"\b") for k in list(KNOWN) + list(DIRECT)}
REJECT = ("unknown", "n/a", "", "various", "multiple")


def resolve(name, anchored=False):
    """Replay geocode() steps 2-4. anchored=True applies the proposed word-boundary fix."""
    if not name or name.strip().lower() in REJECT:
        return None, "rejected"
    n = name.strip().lower()
    if n in DIRECT:
        return DIRECT[n], "directional-exact"
    if n in KNOWN:
        return KNOWN[n], "table-exact"
    hit = (lambda k: WORD[k].search(n)) if anchored else (lambda k: k in n)
    best, blen, bkey = None, 0, None
    for table in (KNOWN, DIRECT):
        for k, v in table.items():
            if len(k) > blen and hit(k):
                best, blen, bkey = v, len(k), k
        if best:
            break
    return (best, "partial:" + bkey) if best else (None, "nominatim-needed")


def km(a, b):
    (la1, lo1), (la2, lo2) = a, b
    p = math.pi / 180
    h = (0.5 - math.cos((la2 - la1) * p) / 2
         + math.cos(la1 * p) * math.cos(la2 * p) * (1 - math.cos((lo2 - lo1) * p)) / 2)
    return 12742 * math.asin(math.sqrt(max(0.0, h)))


def main(tsv):
    print("KNOWN_LOCATIONS: %d   _DIRECTIONAL_REGIONS: %d\n" % (len(KNOWN), len(DIRECT)))
    tot = res = sent = 0
    buckets, changed, unresolved = {}, [], []
    with io.open(tsv, encoding="utf-8") as fh:
        next(fh, None)
        for line in fh:
            p = line.rstrip("\n").split("\t")
            if len(p) < 4:
                continue
            cnt, loc, lat, lon = int(p[0]), p[1], p[2], p[3]
            tot += cnt
            try:
                prod = (float(lat), float(lon))
            except ValueError:
                prod = None
            if prod and abs(prod[0] - SENTINEL[0]) < .01 and abs(prod[1] - SENTINEL[1]) < .01:
                sent += cnt
            coords, how = resolve(loc)
            buckets[how.split(":")[0]] = buckets.get(how.split(":")[0], 0) + cnt
            if coords:
                res += cnt
            else:
                unresolved.append((cnt, loc))
            fixed, howf = resolve(loc, anchored=True)
            if coords != fixed:
                changed.append((cnt, loc, how, coords, howf, fixed))

    print("EVENTS: %d" % tot)
    print("  pinned to the (-25,80) sentinel : %6d (%4.1f%%)" % (sent, 100 * sent / tot))
    print("  resolves offline, no API call   : %6d (%4.1f%%)\n" % (res, 100 * res / tot))
    print("resolution path by event volume:")
    for k, v in sorted(buckets.items(), key=lambda kv: -kv[1]):
        print("   %-18s %7d (%4.1f%%)" % (k, v, 100 * v / tot))

    print("\nTOP UNRESOLVED (would hit Nominatim every time):")
    for cnt, loc in sorted(unresolved, reverse=True)[:20]:
        print("  %6d  %s" % (cnt, loc))

    print("\nEFFECT OF WORD BOUNDARIES: %d strings change (%d events)"
          % (len(changed), sum(c[0] for c in changed)))
    for cnt, loc, how, a, howf, b in sorted(changed, reverse=True)[:25]:
        d = ("  [%dkm apart]" % round(km(a, b))) if (a and b) else ""
        print("  %4d  %-34s %-22s -> %s%s"
              % (cnt, loc[:34], how[:22], (howf[:26] if b else "NONE (Nominatim)"), d))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "locations.tsv")
