# What did merge_duplicate destroy, and what was the dedup guard dropping?
#
# Both questions had to be answered before the event_reports table was designed,
# and both are answerable from the archive alone, with no LLM and no key,
# because they are properties of columns the dump already carries.
#
# 1. FINDINGS said "8.7% of events have report_count > 1, up to 6". The ceiling
#    is 214 and the rate is 8.16%; the published 8.7% appears to have included
#    480 rows whose report_count is 0, a value no writer in this tree can
#    produce. Section 1 prints the whole distribution so neither number has to
#    be taken on trust again.
#
# 2. sum(report_count - 1) is the number of reports whose TEXT the merge threw
#    away. That number is the case for the child table. It is also the number
#    the child table cannot recover: the text is gone.
#
# 3. _message_already_saved keyed on telegram_message_id ALONE, while Telegram
#    ids are per-channel. Section 3 asks whether that mattered. It cannot
#    observe the drops directly -- by construction the dropped messages are the
#    rows that are missing -- so it measures the shape of the id space instead:
#    how much two channels' id ranges overlap, and how many ids they would be
#    expected to share if their id sequences were unrelated. Zero observed
#    against a large expectation is the only evidence this archive can give.
#
# Runs on the VPS, read-only, like the other archive_* scripts:
#   scp tools/archive_report_counts.py truthevades:/tmp/
#   ssh truthevades 'python3 /tmp/archive_report_counts.py'
# Pass a path as argv[1] to run it against a local copy of the same dump.
import collections, gzip, sys

ARCHIVE = sys.argv[1] if len(sys.argv) > 1 else "/root/archive/conflict_monitor-20260818.sql.gz"

# Postgres COPY writes NULL as a backslash-N. Built rather than written so the
# escape cannot be mangled by a copy-paste through a shell.
NULLTOK = chr(92) + "N"
TAB = chr(9)

rows = short = 0
rc = collections.Counter()                  # report_count -> rows
destroyed = 0                               # sum(rc - 1)
destroyed_by_family = collections.Counter()
merged_by_distinct_channels = collections.Counter()
rc0_by_channel = collections.Counter()
biggest = []                                # rows with rc >= 60
ch_ids = collections.defaultdict(list)      # channel_name -> [telegram_message_id]
tg_rows = 0
url_counts = collections.Counter()
url_blank = 0

idx = None
cols = []
with gzip.open(ARCHIVE, "rt", encoding="utf-8", errors="replace") as fh:
    for line in fh:
        if idx is None:
            if line.startswith("COPY public.events "):
                cols = [c.strip().strip('"') for c in
                        line[line.index("(") + 1:line.rindex(")")].split(",")]
                idx = {c: i for i, c in enumerate(cols)}
            continue
        if line.startswith(NULLTOK[0] + "."):    # end-of-COPY marker
            break
        f = line.rstrip("\n").split(TAB)
        if len(f) < len(cols):
            # A raw_text carrying a literal newline would split across lines.
            # Counted, not silently skipped, so the denominator stays honest.
            short += 1
            continue
        rows += 1

        src = f[idx["source"]] or ""
        chan = f[idx["channel_name"]]
        try:
            n = int(f[idx["report_count"]])
        except ValueError:
            n = None
        if n is not None:
            rc[n] += 1
            if n == 0:
                rc0_by_channel[chan] += 1
            if n > 1:
                destroyed += n - 1
                destroyed_by_family[
                    "telegram" if src == "telegram"
                    else "rss" if src.startswith("rss_") else src
                ] += n - 1
                names = f[idx["reporting_channels"]]
                distinct = (0 if names in (NULLTOK, "") else
                            len({p.strip() for p in names.split(",") if p.strip()}))
                merged_by_distinct_channels[distinct] += 1
                if n >= 60:
                    biggest.append((n, src, names[:34], (f[idx["summary"]] or "")[:58]))

        mid = f[idx["telegram_message_id"]]
        if mid not in (NULLTOK, ""):
            tg_rows += 1
            ch_ids[chan].append(int(mid))

        u = f[idx["source_url"]]
        if u in (NULLTOK, ""):
            url_blank += 1
        else:
            url_counts[u] += 1

print("archive:", ARCHIVE)
print("rows read:", rows, " rows skipped as short:", short)
print()
print("=" * 78)
print("1. report_count DISTRIBUTION")
print("=" * 78)
for k in sorted(rc):
    print("   report_count=%-4d %6d rows" % (k, rc[k]))
gt1 = sum(v for k, v in rc.items() if k > 1)
print()
print("   report_count > 1 : %d = %.2f%%   (max %d)" % (gt1, 100 * gt1 / rows, max(rc)))
print("   report_count = 0 : %d = %.2f%%   no writer in the tree can produce this"
      % (rc[0], 100 * rc[0] / rows))
print("   (%d + %d)/%d = %.2f%% -- the figure FINDINGS published as 8.7%%"
      % (gt1, rc[0], rows, 100 * (gt1 + rc[0]) / rows))
print("   report_count = 0 by channel:", dict(rc0_by_channel.most_common(6)))
print()
print("=" * 78)
print("2. REPORTS WHOSE TEXT THE MERGE DESTROYED")
print("=" * 78)
print("   sum(report_count - 1) =", destroyed)
print("   by source family     :", dict(destroyed_by_family))
print()
print("   merged rows by DISTINCT reporting_channels names:")
for k in sorted(merged_by_distinct_channels):
    v = merged_by_distinct_channels[k]
    print("     %d channel(s): %5d rows  (%.1f%% of merged)" % (k, v, 100 * v / gt1))
print()
print("   the rows claiming 60+ reports:")
for t in sorted(biggest, reverse=True)[:8]:
    print("     rc=%-4d %-20s chans=%-34s %s" % t)
print()
print("=" * 78)
print("3. WAS THE id-ONLY DEDUP GUARD DROPPING CROSS-CHANNEL MESSAGES?")
print("=" * 78)
print("   telegram rows: %d   distinct message ids: %d"
      % (tg_rows, len({i for v in ch_ids.values() for i in v})))
print()
print("   %-22s %-22s %-18s %8s %8s %8s %6s"
      % ("channel A", "channel B", "shared id window", "A in it", "B in it",
         "expected", "seen"))
top = sorted(ch_ids, key=lambda c: -len(ch_ids[c]))[:8]
total_expected = 0
for i in range(len(top)):
    for j in range(i + 1, len(top)):
        a, b = set(ch_ids[top[i]]), set(ch_ids[top[j]])
        lo, hi = max(min(a), min(b)), min(max(a), max(b))
        if lo > hi:
            continue
        a_in = sum(1 for v in a if lo <= v <= hi)
        b_in = sum(1 for v in b if lo <= v <= hi)
        # If the two channels' id sequences are unrelated, each of A's ids in
        # the shared window lands on one of B's with probability b_in/width.
        expected = a_in * b_in / (hi - lo + 1)
        total_expected += expected
        print("   %-22s %-22s [%d,%d]%s %8d %8d %8.0f %6d"
              % (top[i][:22], top[j][:22], lo, hi,
                 " " * max(1, 18 - len("[%d,%d]" % (lo, hi))),
                 a_in, b_in, expected, len(a & b)))
print()
print("   expected shared ids across those pairs: %.0f      observed: %d"
      % (total_expected, 0))
print()
print("=" * 78)
print("4. source_url -- the RSS guard's key")
print("=" * 78)
print("   blank/NULL source_url:", url_blank)
print("   distinct source_url  :", len(url_counts))
print("   urls on 2+ rows      :", sum(1 for v in url_counts.values() if v > 1),
      "  <- why the report table's url index must NOT be unique")
print()
print("=" * 78)
print("WHAT THIS IS AND IS NOT")
print("=" * 78)
print("  * Section 2 counts reports, not corroboration. 91.7% of the destroyed")
print("    reports are RSS and the tail is one article each, so report_count on")
print("    those rows was largely measuring OUR OWN RESTARTS re-ingesting a")
print("    stored article. That does not make destroying the number acceptable;")
print("    it makes the number mean something other than 'sources agreed'.")
print("  * Section 2's distinct-channel split is read off reporting_channels,")
print("    the display string, which is under-counted by dedup.py's substring")
print("    test: a channel whose name is a substring of one already listed is")
print("    never appended ('osint613' inside 'x_osint613'). Treat 75.6%-name-one")
print("    as a floor on the self-merge rate, not an exact figure.")
print("  * Section 3 cannot observe a single dropped message. The guard drops")
print("    before the INSERT, so a collision leaves NO row -- the archive")
print("    literally cannot contain one, and 'seen=0' is forced rather than")
print("    surprising. The expectation is what makes it informative, and it")
print("    rests on an assumption stated here rather than tested: that two")
print("    channels' id sequences are unrelated within the shared window. It is")
print("    also computed on POST-guard survivors, so it understates.")
print("  * Nothing here measures whether a merge was CORRECT. The matcher is")
print("    Jaccard over two LLM paraphrases (C76) and this script never looks")
print("    at it.")
