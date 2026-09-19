import gzip, collections, io
path = "/root/archive/conflict_monitor-20260818.sql.gz"
agg = collections.defaultdict(lambda: collections.Counter())
n = 0
inev = False
with gzip.open(path, "rt", encoding="utf-8", errors="replace") as fh:
    for line in fh:
        if not inev:
            if line.startswith("COPY public.events "):
                inev = True
            continue
        if line.startswith("\."):
            break
        f = line.rstrip("\n").split("\t")
        if len(f) < 17:
            continue
        n += 1
        loc, lat, lon = f[15], f[7], f[8]
        agg[loc][(lat, lon)] += 1
out = io.open("/tmp/locations.tsv", "w", encoding="utf-8")
out.write("count\tlocation_name\tlat\tlon\tdistinct_coords\n")
rows = sorted(agg.items(), key=lambda kv: -sum(kv[1].values()))
for loc, coords in rows:
    tot = sum(coords.values())
    (lat, lon), _ = coords.most_common(1)[0]
    out.write("%d\t%s\t%s\t%s\t%d\n" % (tot, loc.replace("\t", " "), lat, lon, len(coords)))
out.close()
print("events scanned:", n)
print("distinct location_name values:", len(agg))
