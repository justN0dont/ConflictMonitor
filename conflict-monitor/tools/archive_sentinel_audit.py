# What would the Indian Ocean sentinel migration actually do to the archive?
#
# backend/app/main.py ships an idempotent startup migration that logs
# "Retired Indian Ocean sentinel on %d event(s)". Read the real statement, not
# a paraphrase of it:
#
#     UPDATE events SET lat = NULL, lon = NULL, geometry = NULL,
#                       is_geolocated = false, geo_precision = NULL,
#                       geo_uncertainty_m = NULL, geo_method = NULL
#     WHERE lat = -25.0 AND lon = 80.0
#
# Two things about that WHERE are worth saying out loud before any number is
# printed, because both are places the migration can look complete and not be:
#
#   * It keys on lat/lon ONLY. `geometry` and `is_geolocated` appear in the SET
#     and not in the WHERE. A row whose geometry points at the sentinel while
#     its lat/lon say something else — or nothing — is not selected, and the
#     new definition of "located" is `geometry IS NOT NULL`. So geometry is
#     audited here separately from lat/lon rather than assumed to agree.
#   * It is an EQUALITY on a double precision column. -25.0 and 80.0 are both
#     exactly representable, so a row written from the old `_UNKNOWN_LAT` /
#     `_UNKNOWN_LON` constants matches exactly. A row written at -25.0000001,
#     or from some other constant the production code used before the v3
#     lineage was recovered, does not — and would survive the migration
#     looking like a measured position. That is why this script sweeps a
#     radius instead of only counting the exact hit.
#
# A third thing, about the number this replaces. docs/FINDINGS.md reports the
# sentinel population as 39,981. That is not this measurement taken badly: it is
# a MODAL-COORDINATE PROXY (archive_locations.py picks one coordinate per
# location_name, geocoder_vs_archive.py charges every row of a name whose modal
# coordinate is near the sentinel). Section 5 replays that proxy from the same
# read as the direct count, so the two are compared as estimators instead of one
# being called the other's typo.
#
# The archive is ONE irreplaceable .sql.gz and this script does not mutate it:
# it is the read-only audit that makes the run decidable, not the run.
#
# Runs on the VPS, read-only, like the other archive_* scripts:
#   scp tools/archive_sentinel_audit.py truthevades:/tmp/
#   ssh truthevades 'python3 /tmp/archive_sentinel_audit.py'
import collections, gzip, io, math, struct

ARCHIVE = "/root/archive/conflict_monitor-20260818.sql.gz"
COORD_FILE = "/tmp/sentinel_audit_coords.tsv"

SENT_LAT, SENT_LON = -25.0, 80.0
# Degree bands, coarsest last. A hit in any band but the first is a row the
# equality predicate misses.
BANDS = [0.000001, 0.0001, 0.01, 0.1, 1.0, 5.0]
TOP_N = 15

# PostGIS EWKB for a 2-D POINT in SRID 4326, little-endian: byte order 01,
# type 01000020 (point + SRID flag), SRID E6100000 (4326 LE), then X then Y as
# 8-byte doubles. Anything else is left undecoded and counted, not guessed at.
EWKB_PREFIX = "0101000020E6100000"

# geocoder.py refuses these location strings outright; geocoder_vs_archive.py
# calls them the "rejected" bucket. They are replayed here for one reason only:
# a register row is built by subtracting that bucket from the proxy above, and
# section 5 cannot show the derivation without measuring both terms.
REJECT_NAMES = ("unknown", "n/a", "", "various", "multiple")


def parse_geom(hexstr):
    """(lon, lat) from the dump's geometry hex, or None if not a 4326 point."""
    if not hexstr.upper().startswith(EWKB_PREFIX):
        return None
    body = hexstr[len(EWKB_PREFIX):]
    if len(body) < 32:
        return None
    try:
        x = struct.unpack("<d", bytes.fromhex(body[0:16]))[0]
        y = struct.unpack("<d", bytes.fromhex(body[16:32]))[0]
    except (ValueError, struct.error):
        return None
    return (x, y)


def fnum(v):
    """Float from a COPY field, or None for \\N / unparseable."""
    if v == "\\N" or v == "":
        return None
    try:
        return float(v)
    except ValueError:
        return None


def km(lat, lon):
    """Great-circle distance from the sentinel, for a human-readable radius."""
    p1, p2 = math.radians(SENT_LAT), math.radians(lat)
    dp, dl = p2 - p1, math.radians(lon - SENT_LON)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371.0 * 2 * math.asin(min(1.0, math.sqrt(a)))


rows = short = 0
lat_null = lon_null = 0

# Geometry is counted in three disjoint buckets, never two. NULL, non-NULL and
# readable, non-NULL and NOT readable by parse_geom (3-D, another SRID,
# big-endian, anything else). Collapsing the third into the first is exactly the
# bug this project is named after: "I could not decode it" written into the slot
# that means "it is not there". The archive happens to be uniform, so the third
# bucket is 0 -- which is a measurement, and is printed as one.
geom_null = geom_notnull = geom_decoded = geom_undecodable = 0

exact = 0                      # matches the migration's WHERE exactly
exact_geom_agrees = 0          # ...and its geometry is the same point
exact_geom_null = 0            # ...and it has no geometry at all
exact_geom_undecodable = 0     # ...and its geometry is non-NULL but unreadable
exact_geom_elsewhere = 0       # ...and its geometry points somewhere else

geom_sentinel = 0              # geometry at the sentinel, whatever lat/lon says
geom_sentinel_not_selected = 0 # ...and the WHERE does NOT select the row

near = collections.Counter()   # (lat, lon) -> rows, within the widest band
band_counts = collections.Counter()
half_lat = collections.Counter()   # lat = -25.0, lon something else
half_lon = collections.Counter()   # lon = 80.0, lat something else

loc = collections.Counter()
sev = collections.Counter()
src = collections.Counter()
etype = collections.Counter()
loc_sev5 = 0
dedup_merged = 0
sev5_all = 0
unknown_all = 0
rejected_all = 0                     # location_name geocoder.py refuses outright

coord_freq = collections.Counter()   # every distinct located coordinate
located_latlon = 0

# location_name -> raw (lat, lon) string pair -> rows. Deliberately the SAME
# structure archive_locations.py builds, off the same columns and keyed on the
# unparsed strings, so section 5 can replay the register's proxy exactly rather
# than describe it. It also answers the question the proxy cannot: for a name
# sitting on the sentinel, does that name geocode successfully anywhere else in
# the archive?
by_name = collections.defaultdict(collections.Counter)

# Column positions come from the COPY header, not from a hardcoded index. The
# archive was dumped on 2026-08-18, BEFORE the Phase 0/1b columns existed, so
# guessing offsets here would silently read the wrong field — and which columns
# are absent is itself one of this script's findings.
idx = None
cols = []
with gzip.open(ARCHIVE, "rt", encoding="utf-8", errors="replace") as fh:
    for line in fh:
        if idx is None:
            if line.startswith("COPY public.events "):
                cols = [c.strip().strip('"') for c in
                        line[line.index("(") + 1:line.rindex(")")].split(",")]
                idx = {c: i for i, c in enumerate(cols)}
                need = ("lat", "lon", "geometry", "location_name", "severity",
                        "source", "event_type", "report_count")
                missing = [c for c in need if c not in idx]
                if missing:
                    raise SystemExit("columns not in dump: %s" % ", ".join(missing))
            continue
        if line.startswith("\\."):
            break
        f = line.rstrip("\n").split("\t")
        if len(f) < len(cols):
            short += 1
            continue
        rows += 1

        lat = fnum(f[idx["lat"]])
        lon = fnum(f[idx["lon"]])
        graw = f[idx["geometry"]]
        g = None
        geom_is_null = (graw == "\\N" or not graw)
        if geom_is_null:
            geom_null += 1
        else:
            geom_notnull += 1
            g = parse_geom(graw)
            if g is None:
                geom_undecodable += 1
            else:
                geom_decoded += 1
        if lat is None:
            lat_null += 1
        if lon is None:
            lon_null += 1

        if lat is not None and lon is not None:
            located_latlon += 1
            coord_freq[(lat, lon)] += 1

        sev_v = f[idx["severity"]]
        loc_v = f[idx["location_name"]]
        if sev_v == "5":
            sev5_all += 1
        if loc_v == "Unknown":
            unknown_all += 1
        if loc_v.strip().lower() in REJECT_NAMES:
            rejected_all += 1
        by_name[loc_v][(f[idx["lat"]], f[idx["lon"]])] += 1

        is_exact = (lat == SENT_LAT and lon == SENT_LON)
        geom_is_sentinel = (g is not None
                            and g[0] == SENT_LON and g[1] == SENT_LAT)
        if geom_is_sentinel:
            geom_sentinel += 1
            if not is_exact:
                geom_sentinel_not_selected += 1

        if is_exact:
            exact += 1
            if geom_is_null:
                exact_geom_null += 1
            elif g is None:
                exact_geom_undecodable += 1
            elif geom_is_sentinel:
                exact_geom_agrees += 1
            else:
                exact_geom_elsewhere += 1
            loc[loc_v] += 1
            sev[sev_v] += 1
            src[f[idx["source"]] or "(empty)"] += 1
            etype[f[idx["event_type"]] or "(empty)"] += 1
            if sev_v == "5" and loc_v == "Unknown":
                loc_sev5 += 1
            rc = f[idx["report_count"]]
            if rc not in ("\\N", "", "0", "1"):
                dedup_merged += 1
        elif lat is not None and lon is not None:
            # Half-matches: one coordinate exactly on the sentinel, the other
            # not. The WHERE is an AND, so these survive it. They also explain
            # or refute a looser earlier count, which is why they are separated
            # out rather than folded into the radius bands below -- a row at
            # (-25, 100) is nowhere near the point but is the same defect.
            if lat == SENT_LAT:
                half_lat[(lat, lon)] += 1
            elif lon == SENT_LON:
                half_lon[(lat, lon)] += 1
            d = max(abs(lat - SENT_LAT), abs(lon - SENT_LON))
            if d <= BANDS[-1]:
                near[(lat, lon)] += 1
                for b in BANDS:
                    if d <= b:
                        band_counts[b] += 1
                        break


def pct(a, b):
    return 100.0 * a / b if b else 0.0


# The register's estimator, replayed from the same read. geocoder_vs_archive.py's
# rule verbatim: take each name's MODAL coordinate and, if it is within 0.01
# degrees of the sentinel, charge that name's ENTIRE event count to the sentinel.
# Replayed rather than described, so section 5 compares two measurements instead
# of a measurement and a recollection.
PROXY_TOL = 0.01
proxy_total = proxy_charged_not_on = proxy_missed = 0
# Names sitting on the sentinel that ALSO resolve to a real point elsewhere in
# the same archive. This is the evidence for whether the UPDATE destroys
# coordinates or enables their repair, and it cuts AGAINST the alarming reading
# of section 1 -- which is exactly why it gets counted here instead of omitted.
repairable_rows = repairable_names = 0
repairable_top = []
for _name, _coords in by_name.items():
    _tot = sum(_coords.values())
    (_mla, _mlo), _ = _coords.most_common(1)[0]
    _mod = (fnum(_mla), fnum(_mlo))
    _on = _real = 0
    for (_la, _lo), _c in _coords.items():
        _p = (fnum(_la), fnum(_lo))
        if _p == (SENT_LAT, SENT_LON):
            _on += _c
        elif _p[0] is not None and _p[1] is not None:
            _real += _c
    if (_mod[0] is not None and _mod[1] is not None
            and abs(_mod[0] - SENT_LAT) < PROXY_TOL
            and abs(_mod[1] - SENT_LON) < PROXY_TOL):
        proxy_total += _tot
        proxy_charged_not_on += _tot - _on   # charged, but carries a real point
    else:
        proxy_missed += _on                  # on the sentinel, never charged
    if _name != "Unknown" and _on and _real:
        repairable_rows += _on
        repairable_names += 1
        repairable_top.append((_on, _real, _name))
repairable_top.sort(reverse=True)
named_at_sentinel = exact - loc.get("Unknown", 0)


print("archive:", ARCHIVE)
print("event rows: %d   (short/unsplittable lines skipped: %d)" % (rows, short))
print()

print("COLUMNS THE MIGRATION WRITES THAT THE ARCHIVE DOES NOT HAVE")
# The SET clause in backend/app/main.py, read off the statement itself:
#   lat, lon, geometry, is_geolocated, geo_precision, geo_uncertainty_m,
#   geo_method
# and nothing else. extraction_status is NOT one of them -- it appears in the
# ADD COLUMN IF NOT EXISTS list a few lines above, which is a different
# operation, and listing it here as something the UPDATE writes was itself a
# claim about a statement nobody had re-read.
SET_COLUMNS = ("lat", "lon", "geometry", "is_geolocated", "geo_precision",
               "geo_uncertainty_m", "geo_method")
absent = [c for c in SET_COLUMNS if c not in idx]
if absent:
    print("  " + ", ".join(absent))
    print("  The dump predates the Phase 0/1b columns, so what the UPDATE would")
    print("  write into them here is UNANSWERABLE -- not 'false', not")
    print("  'unknown-but-present': the fields do not exist. The backend's")
    print("  ADD COLUMN IF NOT EXISTS step creates them on a restored copy")
    print("  before the UPDATE runs, so the UPDATE is executable there; its")
    print("  effect on those columns just cannot be evidenced from this dump.")
else:
    print("  (none -- every SET column is present)")
print("  present in the dump, so every count below is about real columns: "
      + ", ".join(c for c in SET_COLUMNS if c in idx))
other_absent = [c for c in ("extraction_status", "extraction_model",
                            "killed_reported") if c not in idx]
if other_absent:
    print("  ALSO absent, but NOT written by this migration (they come from the")
    print("  same ADD COLUMN step, not the UPDATE):")
    print("    " + ", ".join(other_absent))
    print("  Named because the archive cannot answer questions about them either,")
    print("  and 'unanswerable' has to stay distinguishable from 'false'.")
print()

print("1. THE MIGRATION'S WHERE CLAUSE, COUNTED EXACTLY")
print("   WHERE lat = -25.0 AND lon = 80.0")
print("   rows selected: %d  (%.1f%% of %d)" % (exact, pct(exact, rows), rows))
print()

print("2. WHAT THOSE ROWS CARRY")
print("   location_name (top %d of %d distinct):" % (TOP_N, len(loc)))
for k, v in loc.most_common(TOP_N):
    print("     %-28s %7d (%5.1f%%)" % ((k or "(empty)")[:28], v, pct(v, exact)))
print("   location_name = 'Unknown':      %7d (%5.1f%% of selected)"
      % (loc.get("Unknown", 0), pct(loc.get("Unknown", 0), exact)))
print("   ...AND severity = 5:            %7d (%5.1f%% of selected)"
      % (loc_sev5, pct(loc_sev5, exact)))
print("   a real place name, not Unknown: %7d (%5.1f%%)  <- geocoding failed on"
      % (named_at_sentinel, pct(named_at_sentinel, exact)))
print("                                                     a name that exists")
print("   ...and of those, rows whose name resolves to a REAL coordinate")
print("   elsewhere in this same archive:  %7d (%5.1f%% of the named rows,"
      % (repairable_rows, pct(repairable_rows, named_at_sentinel)))
print("                                                 %d distinct names)"
      % repairable_names)
print("     name                            at sentinel   at a real point")
for _on, _real, _name in repairable_top[:TOP_N]:
    print("     %-30s %7d %14d" % (_name[:30], _on, _real))
print("   This is the same geocoder succeeding and failing on the SAME string on")
print("   different days -- a transient failure, not an unknown place. It means")
print("   the UPDATE is repair-ENABLING for these rows: nulled coordinates are")
print("   what _fix_null_coords_task selects, and it can re-resolve a name the")
print("   archive already proves resolvable. It argues FOR running the migration,")
print("   which is why it is reported here rather than left in the counters.")
print("   severity:")
for k, v in sev.most_common():
    print("     %-28s %7d (%5.1f%%)" % (k if k != "\\N" else "NULL", v, pct(v, exact)))
print("   source:")
for k, v in src.most_common(TOP_N):
    print("     %-28s %7d (%5.1f%%)" % (k[:28], v, pct(v, exact)))
print("   event_type (top %d):" % TOP_N)
for k, v in etype.most_common(TOP_N):
    print("     %-28s %7d (%5.1f%%)" % (k[:28], v, pct(v, exact)))
print("   report_count > 1 (a dedup merge): %d (%.1f%%)"
      % (dedup_merged, pct(dedup_merged, exact)))
print()
print("   whole-archive baselines, for contrast:")
print("     severity = 5 everywhere:       %7d (%5.1f%%)" % (sev5_all, pct(sev5_all, rows)))
print("     location 'Unknown' everywhere: %7d (%5.1f%%)" % (unknown_all, pct(unknown_all, rows)))
print("     of which selected by the WHERE: %7d (%5.1f%%)   <- if this is 100%%,"
      % (loc.get("Unknown", 0), pct(loc.get("Unknown", 0), unknown_all)))
print("        every 'Unknown' row in the archive is parked on the sentinel, and")
print("        the two failures -- could not classify, could not place -- are the")
print("        same rows rather than two overlapping populations.")
print()

print("3. NEAR BUT NOT ON -- what an equality predicate misses")
print("   rows with a coordinate inside each band around (-25, 80),")
print("   EXCLUDING the exact match counted above:")
for b in BANDS:
    print("     within %-9s degrees: %6d" % (b, band_counts[b]))
print("   distinct near coordinates (all, widest band = %s deg):" % BANDS[-1])
if near:
    for (la, lo), c in near.most_common(30):
        print("     %-12s %-12s %6d rows   %8.1f km from the sentinel"
              % (la, lo, c, km(la, lo)))
else:
    print("     (none -- every sentinel-adjacent row is exactly on the point)")
print("   half-matches (the WHERE is an AND, so these survive it):")
print("     lat = -25.0 with some other lon: %d rows, %d distinct coords  %s"
      % (sum(half_lat.values()), len(half_lat),
         half_lat.most_common(5) if half_lat else ""))
print("     lon = 80.0 with some other lat:  %d rows, %d distinct coords  %s"
      % (sum(half_lon.values()), len(half_lon),
         half_lon.most_common(5) if half_lon else ""))
print()
print("   geometry checked independently of lat/lon, because the WHERE does not")
print("   look at it:")
print("     geometry decodes to the sentinel point:        %7d" % geom_sentinel)
print("     ...of those, NOT selected by the WHERE:        %7d   <- survivors"
      % geom_sentinel_not_selected)
print("     selected rows whose geometry agrees:           %7d" % exact_geom_agrees)
print("     selected rows with NULL geometry:              %7d" % exact_geom_null)
print("     selected rows whose geometry is somewhere else:%7d" % exact_geom_elsewhere)
print("     selected rows with a geometry this script could")
print("       not decode (non-NULL, unread):               %7d" % exact_geom_undecodable)
print("     undecodable geometries anywhere in the archive:%7d" % geom_undecodable)
print("       Only a little-endian 2-D SRID-4326 point is read here. A 3-D point,")
print("       another SRID or a big-endian value lands in this line and NOWHERE")
print("       else: it is not silently counted as absent, and the sentinel sweep")
print("       above cannot see inside it. Zero means zero, measured.")
print()
print("   most repeated coordinates in the whole archive -- a SECOND constant")
print("   the old code parked rows on would show up here as a spike:")
for (la, lo), c in coord_freq.most_common(TOP_N):
    tag = "  <- THE SENTINEL" if (la, lo) == (SENT_LAT, SENT_LON) else ""
    print("     %-12s %-12s %6d rows%s" % (la, lo, c, tag))
print()

print("4. WHAT THE ARCHIVE LOOKS LIKE AFTER THE MIGRATION")
print("   'located' under the CURRENT definition is geometry IS NOT NULL.")
print("   before:")
# The label is the predicate: geometry IS NOT NULL is a test on the column, not
# on whether this script could read the value. Counting only the rows that
# decoded and printing them under this label would be the project's cardinal bug
# in miniature -- an undecodable geometry would vanish from a count whose name
# promises it. The decoded subtotal is printed underneath, separately.
print("     geometry IS NOT NULL:  %7d (%5.1f%%)" % (geom_notnull, pct(geom_notnull, rows)))
print("       ...decoded as a 4326 2-D point here: %7d" % geom_decoded)
print("       ...non-NULL but undecodable:         %7d" % geom_undecodable)
print("     lat IS NOT NULL:       %7d (%5.1f%%)" % (located_latlon, pct(located_latlon, rows)))
# Every selected row gets geometry = NULL, whether or not its geometry decoded
# and whether or not it pointed at the sentinel. So the rows that stop counting
# are the selected rows that had a geometry at all -- no decode involved.
selected_with_geom = exact - exact_geom_null
after_geom = geom_notnull - selected_with_geom
after_latlon = located_latlon - exact
print("   after (the UPDATE nulls lat, lon and geometry on every selected row):")
print("     geometry IS NOT NULL:  %7d (%5.1f%%)" % (after_geom, pct(after_geom, rows)))
print("     lat IS NOT NULL:       %7d (%5.1f%%)" % (after_latlon, pct(after_latlon, rows)))
print("   rows the UPDATE touches: %d; of those, %d counted as located under the"
      % (exact, selected_with_geom))
print("     geometry definition and stop counting: %.1f points of geolocation rate"
      % (pct(geom_notnull, rows) - pct(after_geom, rows)))
print("   rows already NULL-coordinate in the dump: lat %d, lon %d, geometry %d"
      % (lat_null, lon_null, geom_null))
print()

# The two numbers in docs/FINDINGS.md's register that this script re-derives,
# quoted so the comparison below names what it is arguing with. These are what
# the register SAID before this audit corrected it; they are kept as the thing
# being argued with, not as current values. No line numbers cited on purpose --
# they moved the moment the register was edited, which is the whole failure mode
# fc0c989 exists to correct.
#   | Pinned to the sentinel `(-25, 80)` | 47.6% (39,981) ... |
#   | Events with a real name that still failed geocoding | 1,667 (1.99% ...) |
REGISTER_PINNED = 39981
REGISTER_NAMED_FAILURES = 1667

print("5. THE REGISTER'S %d IS A DIFFERENT ESTIMATOR, NOT THIS COUNT WRONG"
      % REGISTER_PINNED)
print("   Two ways to ask 'how many rows are on the sentinel', replayed from the")
print("   same read of the same dump:")
print("     modal-coordinate proxy (the register): %7d" % proxy_total)
print("       archive_locations.py reduces the archive to ONE coordinate per")
print("       location_name -- the most common one -- and geocoder_vs_archive.py")
print("       then adds that name's ENTIRE event count if the modal coordinate is")
print("       within %s deg of the sentinel. A name is charged as a unit: every" % PROXY_TOL)
print("       'Israel' row counts as pinned because most 'Israel' rows are,")
print("       including the ones carrying a real coordinate.")
print("     direct row count (this script):        %7d" % exact)
print("       one row, one test, the migration's own predicate.")
print("     difference:                            %+7d" % (proxy_total - exact))
print()
print("   The difference is not the size of the error. The proxy is wrong in BOTH")
print("   directions and the two nearly cancel:")
print("     rows charged to the sentinel that are NOT on it: %7d" % proxy_charged_not_on)
print("     rows on the sentinel that were never charged:    %7d" % proxy_missed)
print("     rows misattributed in total:                     %7d"
      % (proxy_charged_not_on + proxy_missed))
print("     net effect on the headline:                      %+7d"
      % (proxy_charged_not_on - proxy_missed))
print("   So %d rows are placed wrongly to produce a %+d discrepancy. Calling the"
      % (proxy_charged_not_on + proxy_missed, proxy_total - exact))
print("   register 'a miscount, out by %d' would repair the digits and leave the"
      % abs(proxy_total - exact))
print("   proxy's credibility intact -- and a near-cancelling error is the more")
print("   dangerous kind precisely because it makes a name-level guess look like a")
print("   row-level census. The register row should say which estimator it is.")
print()
print("   THE SAME PROXY IS INSIDE A SECOND REGISTER ROW, which no audit run has")
print("   mentioned until now:")
print("     'Events with a real name that still failed geocoding | %d (1.99%%)'"
      % REGISTER_NAMED_FAILURES)
print("     is a subtraction across two different populations:")
print("       proxy sentinel total:                %7d" % proxy_total)
print("       minus events whose location_name geocoder.py rejects outright")
print("       ('Unknown', 'n/a', 'various', 'multiple', empty):  %7d" % rejected_all)
print("       = %7d  (%.2f%% of %d)   <- the register's number"
      % (proxy_total - rejected_all, pct(proxy_total - rejected_all, rows), rows))
print("     the same question asked of rows instead of names:")
print("       sentinel rows:                       %7d" % exact)
print("       minus those whose location_name is 'Unknown':      %7d"
      % loc.get("Unknown", 0))
print("       = %7d  (%.2f%% of %d)   <- CORRECTED"
      % (named_at_sentinel, pct(named_at_sentinel, rows), rows))
print("     The two subtrahends are both %d here, but not for the same reason:"
      % rejected_all)
print("     one counts every row geocoder.py would refuse to resolve anywhere in")
print("     the archive, the other counts 'Unknown' rows sitting on the sentinel.")
print("     They coincide only because every 'Unknown' row IS on the sentinel and")
print("     no other rejected name occurs -- both facts measured in section 2, and")
print("     neither one guaranteed. The whole error is in the minuend.")
print()

with io.open(COORD_FILE, "w", encoding="utf-8") as out:
    out.write("kind\tlat\tlon\trows\tkm_from_sentinel\n")
    out.write("exact\t%s\t%s\t%d\t0.0\n" % (SENT_LAT, SENT_LON, exact))
    for (la, lo), c in near.most_common():
        out.write("near\t%s\t%s\t%d\t%.3f\n" % (la, lo, c, km(la, lo)))
    for (la, lo), c in coord_freq.most_common(200):
        out.write("frequent\t%s\t%s\t%d\t%.3f\n" % (la, lo, c, km(la, lo)))
print("coordinates written to", COORD_FILE)
print()

print("WHAT THIS IS AND IS NOT")
print("  * This is the audit, NOT the run. Nothing was written to the archive;")
print("    the rowcount the migration would report is the number in section 1,")
print("    and until the UPDATE actually executes against a database, that is a")
print("    PREDICTED rowcount and must be quoted as one.")
print("  * It measures the dump as dumped on 2026-08-18. The production database")
print("    it came from was torn down, so nothing here says what a live table")
print("    would contain today.")
print("  * The near-miss sweep bounds ONE failure mode: a coordinate close to the")
print("    sentinel that equality skips. A fallback constant parked somewhere")
print("    else entirely is not found by the radius -- it would have to surface")
print("    in the most-repeated-coordinates table, which is a weaker instrument.")
print("  * is_geolocated -- which the UPDATE writes -- and extraction_status --")
print("    which it does NOT, it belongs to the ADD COLUMN step above it --")
print("    cannot be reported for these rows at all: the columns are absent from")
print("    the dump. Reporting 'false' or '0' for them would be inventing a")
print("    measurement, which is the bug this whole project is about.")
print("  * Severity and location_name here are describing the ROWS the predicate")
print("    selects, not proving the predicate is right. The fallback signature is")
print("    evidence that these rows are unlocated-with-a-fake-point; it is not a")
print("    second, independent test of the coordinate.")
print("  * Section 2's repair-enabling finding cuts AGAINST the headline: half the")
print("    named sentinel rows carry names this archive geocodes successfully on")
print("    other days, so the UPDATE mostly hands repairable rows to the repair")
print("    task rather than destroying positions. A number that argues for the")
print("    change is as much a finding as one that argues against it, and this")
print("    audit collected it before it reported it -- which is the more common")
print("    failure: not measuring the wrong thing, but not printing what was")
print("    measured because it complicated the story.")
print("  * Section 5 is a comparison of two estimators over ONE dump. It shows the")
print("    proxy and the direct count disagreeing and by how much; it does not")
print("    confirm either against a second source, because there is no second")
print("    copy of this data to confirm against.")
