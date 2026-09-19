import gzip, collections
src = collections.Counter(); sev5_by_src = collections.Counter()
inb = False
with gzip.open("/root/archive/conflict_monitor-20260818.sql.gz", "rt", encoding="utf-8", errors="replace") as fh:
    for line in fh:
        if not inb:
            if line.startswith("COPY public.events "): inb = True
            continue
        if line.startswith("\."): break
        f = line.rstrip("\n").split("\t")
        if len(f) < 17: continue
        src[f[1]] += 1
        if f[6] == "5": sev5_by_src[f[1]] += 1
tot = sum(src.values())
print("events by `source` column:")
for k, v in src.most_common():
    print("  %-16s %7d (%5.1f%%)   of which severity=5: %d (%.1f%%)" % (k, v, 100*v/tot, sev5_by_src[k], 100*sev5_by_src[k]/v))
print("total:", tot)
