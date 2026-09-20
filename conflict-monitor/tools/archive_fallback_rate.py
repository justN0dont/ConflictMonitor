# What share of messages does the classifier fail to classify?
#
# Phase 0's last open measurement was written as "run for one week and record
# the real fallback rate". A week of wall clock is the wrong instrument: the
# live stack produces ~150 events a day, so a week is ~1,000 messages of
# whatever happened to be in the news that week, and the number arrives seven
# days late. The archive already holds 83,938 real messages spanning five
# months and 46 sources, and since de146f6 the classifier runs on a local
# Ollama model with no key and no spend. Same quantity, larger and more varied
# population, available now.
#
# It is not the same MODEL as the archive, and that is the one thing this
# script must not let a reader forget. The 83,938 rows were classified by
# claude-haiku-4-5; this re-classifies their text with whatever
# settings.ollama_model names at the moment of the run — qwen3:8b under de146f6,
# qwen3.8-27b:latest since 949aca8. The model is never written into this file's
# output as a literal: the tables print the name the ROWS carry in
# extraction_model, so a report cannot outlive the model it describes. The number
# answers "what is this system's fallback rate NOW". It does not describe Haiku,
# and it is not a week of live operation — see WHAT THIS IS AND IS NOT.
#
# FALLBACK is defined by classifier.py, not by this file: every return path
# sets extraction_status, and exactly one value means a classification actually
# happened — "ok", set in _handle_response. Everything else is a fallback:
#   ollama_timeout / ollama_unreachable / ollama_model_missing /
#   ollama_http_<code> / parse_failed  (the Ollama backend's five)
#   no_backend / bad_backend / no_api_key / rate_limited / api_<code> /
#   llm_failed                          (reachable on other backend settings)
# The set is not hardcoded here. Statuses are tallied as they come back, and
# anything that is not "ok" counts as a fallback, so a status added to
# classifier.py later is counted correctly by this script without being edited
# into it.
#
# FIDELITY: this calls the project's own classify_message(). Not a copy of the
# prompt, not a reimplementation of the parsing — the real function, so every
# failure-tagging branch that runs in production is the branch measured here.
# That is why the run has two halves on two machines:
#
#   1. `sample`   on the VPS      — the archive is there, and it is 919 MB raw
#   2. `classify` in the backend  — Ollama is at host.docker.internal:11434,
#      container                    which only resolves inside the container
#
# Run it with no argument to print the exact commands for both halves.
import collections
import gzip
import json
import math
import os
import random
import sys
import time
import zlib

ARCHIVE = "/root/archive/conflict_monitor-20260818.sql.gz"   # READ-ONLY. Never written.
SAMPLE_FILE = "/tmp/fallback_rate_sample.json"
RESULT_FILE = "/tmp/fallback_rate_results.json"

SEED = 20260818          # same seed as archive_killed_rate.py: the next run draws the same rows
TARGET_N = 500           # before the per-stratum floor is applied; the floor pushes it to ~570
FLOOR_PER_STRATUM = 20   # a stratum with 4 rows in it measures nothing
MIN_SHARE = 0.01         # a `source` below this share is pooled into the "other" stratum
POOLED = "other(<1%)"

CONTAINER = "conflict-monitor-backend-1"

USAGE = """archive_fallback_rate.py -- the Phase 0 fallback rate, measured on the archive.

Two halves, two machines. Run them in order.

  1. Draw the sample ON THE VPS (read-only; writes only %(sample)s):

       cd <repo>/conflict-monitor
       scp tools/archive_fallback_rate.py truthevades:/tmp/
       ssh truthevades 'python3 /tmp/archive_fallback_rate.py sample'
       scp truthevades:%(sample)s <repo>/conflict-monitor/../sample.json

  2. Classify it INSIDE the backend container (Ollama is only reachable from
     there). Do NOT copy the sample into backend/ -- that is a mounted repo
     directory; use docker cp:

       docker cp <path>/sample.json %(c)s:%(sample)s
       docker cp tools/archive_fallback_rate.py %(c)s:/tmp/
       docker compose exec -T backend python /tmp/archive_fallback_rate.py classify

     Per-row results land in %(result)s inside the container; docker cp them
     out if you want to audit individual classifications. Re-print the tables
     from that file without re-classifying anything:

       docker compose exec -T backend python /tmp/archive_fallback_rate.py report

  3. A rate of 0 is only as trustworthy as the counter that produced it, so
     prove the failure branches are still reachable and still tagged:

       docker compose exec -T backend python /tmp/archive_fallback_rate.py selftest

From Git Bash on Windows, prefix the docker commands with MSYS_NO_PATHCONV=1 --
otherwise /tmp/... is rewritten into a C:\\Users\\... path before docker sees it.
""" % {"sample": SAMPLE_FILE, "result": RESULT_FILE, "c": CONTAINER}


def unescape(v):
    """Undo pg_dump's COPY text escaping (\\n, \\t, \\r, \\\\).

    Same helper, same caveat, as tools/archive_killed_rate.py: a message stored
    as "killed\\n17" is two literal characters in the dump, and feeding those to
    the classifier would measure a string no message ever contained. Octal
    escapes are NOT decoded — the fallback drops the backslash and keeps the
    digits — but pg_dump emits \\nnn only for control bytes.
    """
    if "\\" not in v:
        return v
    out, i, n = [], 0, len(v)
    while i < n:
        c = v[i]
        if c == "\\" and i + 1 < n:
            nxt = v[i + 1]
            out.append({"n": "\n", "t": "\t", "r": "\r", "\\": "\\",
                        "b": "\b", "f": "\f", "v": "\v"}.get(nxt, nxt))
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


class Reservoir:
    """Fixed-size uniform sample of a stream of unknown length.

    One per stratum, each with its own seeded Random derived from the stratum
    name, so a stratum's draw does not depend on how the strata interleave in
    the dump. Re-running on the same file draws the same rows.
    """

    def __init__(self, k, label):
        self.k, self.items, self.n = k, [], 0
        self.rnd = random.Random(SEED ^ zlib.crc32(label.encode("utf-8")))

    def add(self, item):
        self.n += 1
        if len(self.items) < self.k:
            self.items.append(item)
        else:
            j = self.rnd.randrange(self.n)
            if j < self.k:
                self.items[j] = item


def _read_events(fh):
    """Yield (field list, column index map) for each row of the events COPY block.

    Column positions are read from the COPY header rather than hardcoded the
    way archive_source_stats.py hardcodes f[1]/f[6]: the whole measurement is
    the text column, and silently classifying `summary` instead of `raw_text`
    would produce a confident number about the wrong data.
    """
    idx, cols = None, []
    for line in fh:
        if idx is None:
            if line.startswith("COPY public.events "):
                cols = [c.strip().strip('"') for c in
                        line[line.index("(") + 1:line.rindex(")")].split(",")]
                idx = {c: i for i, c in enumerate(cols)}
                missing = [c for c in ("raw_text", "source", "channel_name")
                           if c not in idx]
                if missing:
                    raise SystemExit("columns not in dump: %s -- header was: %s"
                                     % (", ".join(missing), line.strip()[:200]))
            continue
        if line.startswith("\\."):
            break
        f = line.rstrip("\n").split("\t")
        if len(f) < len(cols):
            continue
        yield f, idx


# ── half 1: draw the sample (runs on the VPS) ────────────────────────────────

def cmd_sample():
    """Two passes over the dump. Pass 1 counts, pass 2 draws.

    Two passes and not one because the strata cannot be defined until the
    totals are known — which `source` values clear MIN_SHARE, and how many rows
    each stratum is allocated. Knowing n_h before pass 2 means each stratum's
    reservoir is sized to exactly its allocation, so the reservoir IS the
    sample rather than a pool that gets trimmed afterwards by some second rule
    nobody wrote down. Decompressing 193 MB twice costs a couple of minutes and
    touches nothing.
    """
    # Pass 1 — exact rows per source, and how many carry usable text.
    per_source = collections.Counter()
    empty_text = 0
    t0 = time.time()
    with gzip.open(ARCHIVE, "rt", encoding="utf-8", errors="replace") as fh:
        for f, idx in _read_events(fh):
            raw = f[idx["raw_text"]]
            if raw == "\\N" or not raw:
                empty_text += 1
                continue
            per_source[f[idx["source"]]] += 1
    total = sum(per_source.values())
    print("pass 1: %d rows with usable raw_text across %d sources "
          "(%d rows had none), %.0fs"
          % (total, len(per_source), empty_text, time.time() - t0))

    # Strata: every source at or above MIN_SHARE stands alone; the long tail is
    # pooled. STRATIFICATION IS NOT COSMETIC HERE — docs/FINDINGS.md records
    # severity-5 rates of 60.8% (iranintl) against 94.8% (jpost), and that
    # spread is what proved the classifier was working at all. A sample that
    # ignored source could hide the same kind of spread in the fallback rate.
    stratum_of = {}
    strat_total = collections.Counter()
    for src, n in per_source.items():
        label = src if n / total >= MIN_SHARE else POOLED
        stratum_of[src] = label
        strat_total[label] += n

    # Allocation: proportional to archive share, floored so a small stratum is
    # still measurable. The floor makes the sample NOT self-weighting — small
    # strata are over-represented in it — so the headline below is computed as
    # a weighted estimate using W_h = N_h / N, never as a raw sample average.
    alloc = {}
    for label, n in strat_total.items():
        want = max(FLOOR_PER_STRATUM, int(round(TARGET_N * n / total)))
        alloc[label] = min(want, n)
    print("strata: %d   allocated sample: %d rows (target %d before the floor of %d)"
          % (len(alloc), sum(alloc.values()), TARGET_N, FLOOR_PER_STRATUM))

    # Pass 2 — draw.
    res = {label: Reservoir(k, label) for label, k in alloc.items()}
    t0 = time.time()
    with gzip.open(ARCHIVE, "rt", encoding="utf-8", errors="replace") as fh:
        for f, idx in _read_events(fh):
            raw = f[idx["raw_text"]]
            if raw == "\\N" or not raw:
                continue
            src = f[idx["source"]]
            res[stratum_of[src]].add({
                "stratum": stratum_of[src],
                "source": src,
                "channel": f[idx["channel_name"]],
                # Full text, not truncated. classify_message() truncates to
                # 2000 chars itself, but _regex_location_fallback reads the
                # WHOLE raw_text, so truncating here would change which
                # location a fallback row reports.
                "raw_text": unescape(raw),
            })
    print("pass 2: drew %d rows, %.0fs" % (sum(len(r.items) for r in res.values()),
                                           time.time() - t0))

    rows = [it for label in sorted(res) for it in res[label].items]
    rows.sort(key=lambda r: (r["stratum"], r["source"], r["channel"], r["raw_text"]))
    meta = {
        "archive": ARCHIVE,
        "archive_rows_with_text": total,
        "archive_rows_without_text": empty_text,
        "seed": SEED,
        "drawn_by": "per-stratum reservoir sampling, one random.Random per "
                    "stratum seeded with SEED ^ crc32(stratum name); two passes "
                    "over the dump, pass 1 counts and pass 2 draws",
        "min_share": MIN_SHARE,
        "target_n": TARGET_N,
        "floor_per_stratum": FLOOR_PER_STRATUM,
        # N_h and n_h for every stratum: the classify half needs both to weight
        # the estimate, and carrying them in the file means it never has to
        # touch the archive.
        "strata": {label: {"N": strat_total[label], "n": len(res[label].items)}
                   for label in sorted(res)},
        "sources_in_pool": sorted(s for s, l in stratum_of.items() if l == POOLED),
    }
    with open(SAMPLE_FILE, "w", encoding="utf-8") as out:
        json.dump({"meta": meta, "rows": rows}, out, ensure_ascii=False)

    print()
    print("%-26s %8s %8s %8s %8s" % ("stratum", "N", "share", "n", "n/N"))
    for label in sorted(meta["strata"], key=lambda k: -meta["strata"][k]["N"]):
        s = meta["strata"][label]
        print("  %-24s %8d %7.2f%% %8d %7.3f%%"
              % (label, s["N"], 100 * s["N"] / total, s["n"],
                 100 * s["n"] / s["N"]))
    print()
    print("pooled into %s: %s" % (POOLED, ", ".join(meta["sources_in_pool"])))
    print()
    print("wrote %d rows to %s (%.1f KB)"
          % (len(rows), SAMPLE_FILE, os.path.getsize(SAMPLE_FILE) / 1024))


# ── statistics ───────────────────────────────────────────────────────────────

def wilson(k, n, z=1.96):
    """Wilson score interval for k/n. Behaves at k=0 and k=n, where the normal
    approximation collapses to a zero-width interval and lies."""
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, centre - half), min(1.0, centre + half))


def stratified(strata):
    """Weighted estimate over strata, each a dict with N (archive rows),
    n (sampled), k (hits). Returns (p_hat, half_width_95, ok).

    p = sum(W_h p_h); Var = sum(W_h^2 (1 - n_h/N_h) p_h(1-p_h)/(n_h - 1)),
    the usual stratified proportion variance with the finite-population
    correction. `ok` is False when every stratum is degenerate (k=0 everywhere
    or k=n everywhere), because the variance is then exactly 0 and the
    interval it produces has zero width — which would be this project's
    signature bug, an unmeasured certainty printed as a measurement.
    """
    N = sum(s["N"] for s in strata)
    p = var = 0.0
    any_var = False
    for s in strata:
        if s["n"] == 0:
            continue
        W = s["N"] / N
        ph = s["k"] / s["n"]
        p += W * ph
        if s["n"] > 1:
            fpc = 1 - s["n"] / s["N"]
            v = W * W * fpc * ph * (1 - ph) / (s["n"] - 1)
            var += v
            if v > 0:
                any_var = True
    return p, 1.96 * math.sqrt(var), any_var


# ── half 2: classify the sample (runs inside the backend container) ──────────

def cmd_classify():
    # /app in the container; `python /tmp/script.py` puts /tmp on sys.path, not
    # the working directory, so `app.services.classifier` would not import.
    sys.path.insert(0, os.getcwd())
    import asyncio
    from app.config import settings
    from app.services.classifier import classify_message

    with open(SAMPLE_FILE, encoding="utf-8") as fh:
        blob = json.load(fh)
    meta, rows = blob["meta"], blob["rows"]

    print("classifying %d rows with the project's own classify_message()" % len(rows))
    print("backend=%s  model=%s  url=%s"
          % (settings.llm_backend, settings.ollama_model, settings.ollama_url))
    print()

    async def run():
        out = []
        t0 = time.time()
        for i, row in enumerate(rows, 1):
            t = time.time()
            r = await classify_message(row["raw_text"])
            out.append({
                "stratum": row["stratum"],
                "source": row["source"],
                "channel": row["channel"],
                "chars": len(row["raw_text"]),
                "secs": round(time.time() - t, 3),
                "extraction_status": r.get("extraction_status"),
                "extraction_model": r.get("extraction_model"),
                "severity": r.get("severity"),
                "killed_reported": r.get("killed_reported"),
                "location_name": r.get("location_name"),
                "is_noise": r.get("is_noise"),
                "summary": (r.get("summary") or "")[:200],
                "raw_head": " ".join(row["raw_text"].split())[:160],
            })
            if i % 25 == 0 or i == len(rows):
                el = time.time() - t0
                print("  %4d/%d  %.0fs elapsed, %.2fs/msg, ~%.0fs left"
                      % (i, len(rows), el, el / i, el / i * (len(rows) - i)),
                      flush=True)
        return out

    results = asyncio.run(run())
    with open(RESULT_FILE, "w", encoding="utf-8") as out:
        json.dump({"meta": meta, "results": results}, out, ensure_ascii=False)
    print("per-row results written to %s" % RESULT_FILE)
    print()
    report(meta, results, settings)


def report(meta, results, settings):
    n = len(results)
    status = collections.Counter(r["extraction_status"] for r in results)
    models = collections.Counter(r["extraction_model"] for r in results)
    secs = sorted(r["secs"] for r in results)

    def is_fb(r):
        # Defined against classifier.py's ONE success value, not against a list
        # of failure names copied into this file. A status added there later is
        # counted here without editing this script.
        return r["extraction_status"] != "ok"

    # Per-stratum k/n, married to the N_h the sampler recorded.
    per = collections.defaultdict(lambda: {"n": 0, "k": 0})
    for r in results:
        d = per[r["stratum"]]
        d["n"] += 1
        d["k"] += 1 if is_fb(r) else 0
    strata = [{"N": meta["strata"][label]["N"], "n": d["n"], "k": d["k"]}
              for label, d in per.items()]
    p, half, has_var = stratified(strata)
    k = sum(1 for r in results if is_fb(r))
    lo, hi = wilson(k, n)

    # The model named in the title is the one the ROWS name, not the one the
    # settings object happens to hold now: `report` re-renders an old result
    # file, and settings.ollama_model can have moved on since it was written.
    # _build_fallback records no model on the branches where nothing reached a
    # model, hence the None filter and the honest label when nothing is left.
    named = [m for m in models if m]
    title_model = max(named, key=lambda m: models[m]) if named else \
        "model unrecorded (no row carries extraction_model)"

    print("=" * 78)
    print("PHASE 0 FALLBACK RATE -- %s over archive text, one pass" % title_model)
    print("=" * 78)
    print("archive:        %s" % meta["archive"])
    print("population:     %d stored events carrying raw_text" % meta["archive_rows_with_text"])
    print("sample:         %d rows, %d strata, seed %d" % (n, len(per), meta["seed"]))
    print("drawn by:       %s" % meta["drawn_by"])
    print("classified by:  %s"
          % ", ".join("%s x%d" % (m, c) for m, c in models.most_common()))
    print("       ^ read off extraction_model on the rows. THIS is what produced")
    print("         the numbers below.")
    print("settings now:   %s via %s (%s)"
          % (settings.ollama_model, settings.llm_backend, settings.ollama_url))
    print("       ^ the settings THIS process holds. Equal to the line above on a")
    print("         fresh `classify`; may differ under `report`, which re-renders a")
    print("         result file written earlier.")
    print("wall clock:     %.0fs total, median %.2fs/msg, p95 %.2fs, max %.2fs"
          % (sum(secs), secs[n // 2], secs[int(n * 0.95)], secs[-1]))
    print()

    print("EXTRACTION_STATUS DISTRIBUTION")
    for s, c in status.most_common():
        print("  %-24s %5d  (%5.1f%% of sample)   %s"
              % (s, c, 100 * c / n, "classified" if s == "ok" else "FALLBACK"))
    print()

    print("HEADLINE -- share of messages where classification did NOT return ok")
    print("  archive-weighted:  %.2f%%   95%% CI %.2f%% - %.2f%%   (stratified, FPC)"
          % (100 * p, 100 * max(0.0, p - half), 100 * min(1.0, p + half)))
    if not has_var:
        print("       ^ EVERY stratum is degenerate (all ok, or all fallback), so the")
        print("         stratified variance is exactly 0 and that interval has zero")
        print("         width. It is NOT a measurement of certainty. Read the pooled")
        print("         Wilson interval below, which is correct at k=0 and k=n.")
    print("  pooled sample:     %d/%d = %.2f%%   95%% CI %.2f%% - %.2f%%   (Wilson)"
          % (k, n, 100 * k / n, 100 * lo, 100 * hi))
    print("       ^ describes the sample AS DRAWN. It differs from the weighted")
    print("         figure because the per-stratum floor over-represents small")
    print("         sources; the weighted line is the archive-representative one.")
    if k == 0:
        print("  QUOTE THIS:        no fallback in %d messages -> at most %.2f%% of this"
              % (n, 100 * hi))
        print("                     population, 95% confidence. Not 'zero'. A rate this")
        print("                     low is bounded by the sample size, not measured by it,")
        print("                     and the selftest mode is what shows the counter can")
        print("                     still reach a non-zero value.")
    print()

    print("FALLBACK RATE BY STRATUM (n is small outside telegram -- read the CI)")
    print("  %-24s %8s %6s %5s %8s  %s" % ("stratum", "N", "n", "fb", "rate", "95% CI (Wilson)"))
    for label in sorted(per, key=lambda l: -meta["strata"][l]["N"]):
        d = per[label]
        slo, shi = wilson(d["k"], d["n"])
        print("  %-24s %8d %6d %5d %7.1f%%  %.1f%% - %.1f%%"
              % (label, meta["strata"][label]["N"], d["n"], d["k"],
                 100 * d["k"] / d["n"], 100 * slo, 100 * shi))
    print()

    # ── coverage: which feeds this sample can and cannot speak for ───────────
    # A stratum interval bounds the STRATUM. The pooled stratum is not a feed,
    # it is dozens of feeds sharing a few dozen rows, and reading its interval
    # as though every feed inside it carried that bound turns "I never looked at
    # this feed" into "I looked and found nothing" — states 2 and 1 of
    # docs/FINDINGS.md, in the table most likely to be quoted as reassurance.
    # So the counts below are printed every run, by name, and the per-feed
    # bound that does not exist is said not to exist.
    seen = collections.Counter(r["source"] for r in results)
    standalone = [l for l in meta["strata"] if l != POOLED]
    pooled_src = meta.get("sources_in_pool", [])
    n_sources = len(standalone) + len(pooled_src)
    at_floor = sum(1 for l in meta["strata"]
                   if meta["strata"][l]["n"] == meta["floor_per_stratum"])

    print("COVERAGE -- what this sample can and cannot speak for")
    print("  archive sources:  %d      with >=1 row here: %d      with NO row: %d"
          % (n_sources, len(seen), n_sources - len(seen)))
    print("  strata:           %d      sitting exactly at the floor of %d rows: %d"
          % (len(meta["strata"]), meta["floor_per_stratum"], at_floor))
    if POOLED in meta["strata"]:
        one = [s for s in pooled_src if seen.get(s, 0) == 1]
        none = sorted(s for s in pooled_src if not seen.get(s, 0))
        print("  the %s stratum pools %d feeds into %d rows:"
              % (POOLED, len(pooled_src), meta["strata"][POOLED]["n"]))
        print("      %d feeds got at least one row, %d got exactly one, %d got NONE"
              % (len(pooled_src) - len(none), len(one), len(none)))
        print("      Its interval in the tables above is a bound on the POOL, not")
        print("      on any feed in it.")
        if none:
            print("      These %d carry no bound of any width -- they were not"
                  % len(none))
            print("      measured, at all:")
            for i in range(0, len(none), 4):
                print("        %s" % ", ".join(none[i:i + 4]))
    print()

    # ── what a SUCCESSFUL classification actually contains ───────────────────
    # The number Phase 0 cares about is not only "did it answer" but "is the
    # answer a measurement". An ok row whose location is Unknown is state 3
    # wearing state 1's clothes — exactly the bug class docs/FINDINGS.md names.
    ok = [r for r in results if not is_fb(r)]
    noise = [r for r in ok if r["is_noise"]]
    real = [r for r in ok if not r["is_noise"]]
    fb = [r for r in results if is_fb(r)]

    # LIKE-FOR-LIKE. Every archive figure quoted below is a rate over STORED
    # EVENTS, and a stored event is not any ok row: the callers drop the noise
    # rows, and then drop anything under MIN_SEVERITY, before insert. Measuring
    # over rows production would have thrown away and setting that beside a rate
    # over rows production kept compares two different populations and reports
    # the difference as a model difference. `stored` is the sampled rows that
    # would actually have become events, and it is the denominator for every
    # archive comparison in this report.
    #
    # The gate is reproduced exactly as the callers write it — `severity is not
    # None and severity < MIN_SEVERITY` (telegram.py:176, news_feeds.py:347) —
    # so a row with no severity passes here because it passes there. That is
    # deliberate: an unmeasured severity is not a low severity.
    MIN_SEVERITY = 3            # telegram.py:29, news_feeds.py:66
    stored = [r for r in real
              if not (r["severity"] is not None and r["severity"] < MIN_SEVERITY)]

    def share(rows_, pred):
        if not rows_:
            return "n/a (0 rows)"
        c = sum(1 for r in rows_ if pred(r))
        l, h = wilson(c, len(rows_))
        return "%d/%d = %.1f%%  (95%% CI %.1f%% - %.1f%%)" % (
            c, len(rows_), 100 * c / len(rows_), 100 * l, 100 * h)

    unknown = lambda r: r["location_name"] == "Unknown"
    nosev = lambda r: r["severity"] is None

    print("WHAT THE SUCCESSFUL CLASSIFICATIONS CONTAIN")
    print("  ok rows:                       %d  of which [NOISE]/severity<=1: %d"
          % (len(ok), len(noise)))
    print("       ^ the noise rows are 'ok' AND are dropped before insert by the")
    print("         callers (telegram.py, news_feeds.py). They never become events,")
    print("         so they are counted separately below, not folded in.")
    print("  ok, non-noise:                 %d  of which severity < %d: %d"
          % (len(real), MIN_SEVERITY, len(real) - len(stored)))
    print("       ^ dropped before insert as well, by the MIN_SEVERITY gate.")
    print("  would have been stored:        %d  <- LIKE-FOR-LIKE denominator"
          % len(stored))
    print("       ^ the only subset the archive rates are comparable to, because")
    print("         every one of the 83,938 archived rows passed both gates.")
    print("  location 'Unknown', would-have-been-stored rows:  %s"
          % share(stored, unknown))
    print("       ^ THE comparable figure. Archive: 45.6% of 83,938 stored events")
    print("         carry location_name='Unknown' (measured on the dump, not quoted")
    print("         from a doc). classifier.py's regex + flag fallback has already")
    print("         run on these rows (_handle_response), so this is Unknown AFTER")
    print("         every rescue the production path attempts.")
    print("       ^ ONE CONFOUND STANDS EVEN SO, and it is on the archive's side:")
    print("         the dump has no extraction_status and no extraction_model")
    print("         column (both post-date it; check its COPY header), so its 45.6%")
    print("         is over every stored row -- the ones Haiku classified AND the")
    print("         ones where no classification happened and _build_fallback's")
    print("         regex supplied the location. The dump records neither which")
    print("         rows those were nor how many, so 45.6% is the rate for the")
    print("         ARCHIVE, not a measurement of Haiku, and the difference above")
    print("         is not yet a model-vs-model result in either direction.")
    print("         de146f6 recorded the opposite impression from live rows")
    print("         ('qwen3 returns Unknown noticeably more often than Haiku did'),")
    print("         unquantified; settling that needs the key and a fresh run over")
    print("         this same sample, which is what Phase 0 still owes.")
    print("  location 'Unknown', all ok non-noise rows:        %s" % share(real, unknown))
    print("       ^ the WIDER denominator: the same rows plus the severity-1/2 ones")
    print("         the callers would have dropped. It is what an earlier run")
    print("         quoted against 45.6%; it is not the like-for-like number, and")
    print("         the gap between these two lines is how much that mattered.")
    print("  location 'Unknown', ok NOISE rows:      %s" % share(noise, unknown))
    print("       ^ higher by construction: _handle_response returns on the noise")
    print("         branch BEFORE the location fallback runs.")
    print("  null severity, ok rows:                 %d/%d  (count only, no interval)"
          % (sum(1 for r in ok if nosev(r)), len(ok)))
    print("       ^ structurally zero, and that is the finding, not an omission:")
    print("         _handle_response raises when a reply carries no severity, so a")
    print("         missing severity is tagged parse_failed and appears in the")
    print("         FALLBACK bucket above instead of as a null inside an ok row.")
    print("         A null severity on an ok row would mean that guard had broken.")
    print("         NO CONFIDENCE INTERVAL IS PRINTED ON IT: a Wilson interval")
    print("         describes sampling uncertainty, and there is none here — a")
    print("         bigger sample would not move a quantity a code path forbids.")
    print("         Printing one would dress a structural certainty as a measurement.")
    print("  location 'Unknown', FALLBACK rows:      %s" % share(fb, unknown))
    print("  null severity, FALLBACK rows:           %s"
          % ("n/a (0 rows)" if not fb else
             "%d/%d  (count only, no interval)"
             % (sum(1 for r in fb if nosev(r)), len(fb))))
    print("       ^ expected to be all of them: _build_fallback emits no severity")
    print("         key at all. Structural again, so no interval again.")
    print()
    print("  location 'Unknown', ALL %d sampled rows:  %s" % (n, share(results, unknown)))
    print()

    print("'Unknown' RATE BY STRATUM, would-have-been-stored rows only (same")
    print("denominator as the like-for-like line above)")
    by = collections.defaultdict(lambda: [0, 0])
    for r in stored:
        by[r["stratum"]][0] += 1
        by[r["stratum"]][1] += 1 if unknown(r) else 0
    # Iterated over the SAMPLED strata, not over `by`, so a stratum whose rows
    # were all dropped by the two gates prints as "no rows" instead of silently
    # vanishing from the table. A stratum that disappears reads as one that was
    # never sampled, which is the state-2-as-state-1 confusion in miniature.
    for label in sorted(per, key=lambda l: -meta["strata"][l]["N"]):
        tot, unk = by[label]
        if not tot:
            print("  %-24s     0 rows   nothing survived the noise / MIN_SEVERITY"
                  " gates -- no rate" % label)
            continue
        ulo, uhi = wilson(unk, tot)
        print("  %-24s %5d rows   Unknown: %4d (%5.1f%%)   95%% CI %.1f%% - %.1f%%"
              % (label, tot, unk, 100 * unk / tot, 100 * ulo, 100 * uhi))
    print()

    # Free once the sample has been classified, and it lands on the loudest
    # number in docs/FINDINGS.md: 86.3% of the archive scored EXACTLY severity
    # 5, and no archived event ever scored 1 or 2. Both columns are printed,
    # because the 86.3% is a rate over STORED events and only one of them has
    # that denominator; and the "never 1 or 2" half is not a model fact at all.
    sev_all = collections.Counter(r["severity"] for r in ok)
    sev_ll = collections.Counter(r["severity"] for r in stored)
    s5_all, s5_ll = sev_all.get(5, 0), sev_ll.get(5, 0)
    print("SEVERITY DISTRIBUTION (archive comparison: 86.3% of the 83,938 stored")
    print("events scored exactly 5, and the archive holds no 1 and no 2 at all)")
    print("  %-12s %16s   %16s" % ("", "ok rows", "would-be-stored"))
    scale = max(sev_all.values()) if sev_all else 1
    for s in sorted(set(sev_all) | set(sev_ll), key=lambda v: (v is None, v)):
        print("  severity %-4s %5d (%5.1f%%)   %5d (%5.1f%%)  %s"
              % (s, sev_all[s], 100 * sev_all[s] / len(ok) if ok else 0.0,
                 sev_ll[s], 100 * sev_ll[s] / len(stored) if stored else 0.0,
                 "#" * int(40 * sev_all[s] / scale)))
    print("  exactly 5:   ok rows %d/%d = %.1f%%   LIKE-FOR-LIKE %d/%d = %.1f%%"
          % (s5_all, len(ok), 100 * s5_all / len(ok) if ok else 0.0,
             s5_ll, len(stored), 100 * s5_ll / len(stored) if stored else 0.0))
    print("       ^ only the LIKE-FOR-LIKE figure belongs beside the archive's")
    print("         86.3%: the ok column also contains the noise rows and the")
    print("         severity-1/2 rows, and the archive contains neither, because")
    print("         both were dropped before insert. A share computed over rows")
    print("         the production path discards cannot be differenced against a")
    print("         share computed over rows it kept.")
    print("       ^ and even the like-for-like difference is NOT a clean")
    print("         model-vs-model comparison. Much of the archive's 86.3% was the")
    print("         Field(default=5) bug: a reply that omitted severity validated")
    print("         clean as a 5, and 89c6f54 removed six such defaults. How much")
    print("         of the 86.3% was the bug and how much was Haiku is NOT measured")
    print("         here and needs the key to separate.")
    print("       ^ THE ARCHIVE'S MISSING 1s AND 2s ARE A FILTER, NOT A MODEL")
    print("         BEHAVIOUR. No model could have put a 1 or a 2 in that archive:")
    print("         MIN_SEVERITY = 3 (telegram.py:29, news_feeds.py:66) and both")
    print("         callers drop anything below it before insert (telegram.py:176,")
    print("         news_feeds.py:347), with classifier.py marking severity <= 1 as")
    print("         noise on top of that. So 'this model uses 1 and 2, the archive")
    print("         never did' compares a pre-filter measurement with a post-filter")
    print("         one and credits the filter to the model. What the 1s and 2s in")
    print("         the left column do show is how much of THIS model's output the")
    print("         ingest filter would throw away -- a property of the pipeline,")
    print("         reported in the block above, not a difference from Haiku.")
    print("         Reproduce the archive side on the VPS (read-only, ~2 min):")
    print("           zcat %s \\" % ARCHIVE)
    print("             | awk -F'\\t' '/^COPY public.events /{c=1;next}")
    print("                            c&&NF>=21{print $7}' | sort -n | uniq -c")
    print("         ($7 is severity and 21 is the column count in that dump's COPY")
    print("         header -- read the header, do not trust the 7. Run 2026-09-20:")
    print("         3:5178  4:2251  5:72397  6:2808  7:1072  8:207  9:24  10:1.")
    print("         No 1, no 2, minimum 3, and 72397/83938 = 86.3% at exactly 5.)")
    print()

    if fb:
        print("EVERY FALLBACK ROW (this is the whole point -- read them):")
        for r in fb:
            print("  [%s/%s] %s  loc=%r"
                  % (r["extraction_status"], r["source"], r["raw_head"][:100],
                     r["location_name"]))
        print()

    print("WHAT THIS IS AND IS NOT")
    print("  * It is %s, not Haiku. The 83,938 archived rows were" % title_model)
    print("    classified by claude-haiku-4-5; this re-classifies their TEXT with")
    print("    the local model named above, read off the rows themselves. Comparing")
    print("    the two needs the Anthropic key, which returns 400")
    print("    organization_on_hold.")
    print("  * It is ONE PASS with no retries over time. The Ollama backend does not")
    print("    retry (classifier.py), and a transient outage in live operation would")
    print("    show up as a burst of ollama_unreachable that a week of wall clock")
    print("    would capture and this does not. This measures the rate at which the")
    print("    MODEL fails to produce a parseable classification, with the daemon up")
    print("    the whole time. It is a FLOOR on the live rate, not an estimate of it.")
    print("  * The parse-failure count is measured UNDER A DECODER CONSTRAINT, and")
    print("    two of its shapes are ruled out before the model is involved.")
    print("    classifier.py sends \"format\": \"json\", so the daemon samples the")
    print("    reply under a JSON grammar. Six shapes reach the parse_failed tag on")
    print("    the Ollama path: the envelope is not JSON / has no \"response\" key /")
    print("    carries a non-string \"response\"; and the reply is not valid JSON /")
    print("    is valid JSON but not an object / is an object that fails the schema.")
    print("    The grammar forecloses the fourth and fifth, so what is left is")
    print("    mostly schema compliance. Measured rather than assumed, on this")
    print("    daemon: asked for the bare JSON number 7 with format:\"json\" it")
    print("    returned {\"number\": 7}; asked for [1,2,3] with format removed it")
    print("    returned [1,2,3]. What the grammar does NOT foreclose is an EMPTY")
    print("    reply -- exactly how qwen3.8-27b failed 3/3 before 949aca8 sent")
    print("    \"think\": False, and the shape the selftest reproduces. Drop")
    print("    format:\"json\" and this rate would be higher; it is a property of")
    print("    the pipeline as configured, not of the model alone.")
    print("  * Different population from live traffic, and the bias is known. The")
    print("    archive holds STORED EVENTS: messages Haiku tagged [NOISE] and")
    print("    low-severity articles were dropped before insert, so this text is")
    print("    pre-filtered toward real incident reports. Live traffic contains the")
    print("    junk that was filtered out, and junk is harder to classify, so the")
    print("    live fallback rate is likely HIGHER than the number above.")
    print("  * Deduplication merged repeats into single rows (8.7% of the archive")
    print("    has report_count > 1), so this is a rate per stored event, not per")
    print("    message received.")
    print("  * It does not cover every feed, and a stratum bound is not a feed")
    print("    bound. %d of the %d sources in the archive have no row in this sample"
          % (n_sources - len(seen), n_sources))
    print("    at all; for those the answer is 'not looked at', and no interval in")
    print("    this report, however wide, applies to them. The COVERAGE block above")
    print("    names them. The pooled stratum's interval bounds the pool as a whole.")
    print("  * The sample is not self-weighting. The per-stratum floor over-samples")
    print("    small sources on purpose; the archive-weighted line corrects for it")
    print("    with W_h = N_h/N, and that is the line to quote for a POINT estimate.")
    if k == 0:
        print("    It is not the line to quote here: with zero hits every stratum")
        print("    weights to zero, so the weighted figure carries no information the")
        print("    pooled upper bound does not carry better. Quote the bound.")
    print("  * It says nothing about whether a successful classification is RIGHT.")
    print("    extraction_status='ok' means the reply parsed and validated. The")
    print("    Unknown-location rate above is the closest thing here to a quality")
    print("    measure, and it only catches the failure that announces itself.")


# ── negative control (runs inside the backend container) ─────────────────────

def cmd_selftest():
    """Prove the counter can reach a non-zero value.

    A fallback rate near 0 is the most dangerous number this repo can print: it
    is what a working classifier and a blind instrument both look like. This
    drives classify_message() down five failure branches on purpose and checks
    each one comes back tagged, so a 0 from the classify run means "no failure
    occurred" rather than "no failure can be seen". Same distinction as states 1
    and 2 in docs/FINDINGS.md, applied to the measuring tool instead of to the
    data.

    FOUR of the five are infrastructure: no model, no daemon, no backend, a
    typo'd backend name. None of them can fire during a healthy run, so on their
    own they prove only that the tagging works when the stack is broken. The
    fifth is parse_failed, the ONE branch a healthy stack can reach — the model
    answers and the answer is not a classification — and it is the branch that
    actually fired in production: with thinking left on, qwen3.8-27b returned an
    EMPTY `response` and every row came back parse_failed until 949aca8 sent
    "think": False. That is the shape reproduced here, by pointing the classifier
    at a stub daemon that returns Ollama's envelope with an empty response. The
    stub is a real HTTP server on loopback, so the whole production path runs:
    httpx, the status check, the envelope read, _handle_response, the raise, the
    tag. Nothing in classifier.py is patched or imitated.

    It mutates `settings` in THIS process only. `docker compose exec` starts a
    fresh interpreter; the uvicorn process serving the app is untouched, and
    the values are restored in a finally block regardless.
    """
    sys.path.insert(0, os.getcwd())
    import asyncio
    import http.server
    import threading
    from app.config import settings
    from app.services.classifier import classify_message

    class EmptyReplyHandler(http.server.BaseHTTPRequestHandler):
        """Ollama's 200 envelope carrying response="" — the 27b's real failure."""

        def do_POST(self):
            # Drain the request body first: answering without reading it can
            # reset the connection, and the test would then measure
            # ollama_unreachable instead of the parse branch it is aiming at.
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            body = json.dumps({"model": "stub", "response": "",
                               "done": True}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass                              # keep the control's output readable

    msg = "IDF strikes Hezbollah positions near Tyre in southern Lebanon."
    saved = (settings.llm_backend, settings.ollama_model, settings.ollama_url)
    cases = []
    stub = None
    try:
        settings.ollama_model = "definitely-not-pulled:0b"
        cases.append(("model not pulled", "ollama_model_missing",
                      asyncio.run(classify_message(msg))))
        settings.ollama_model = saved[1]

        settings.ollama_url = "http://127.0.0.1:1"      # nothing listens there
        cases.append(("daemon unreachable", "ollama_unreachable",
                      asyncio.run(classify_message(msg))))
        settings.ollama_url = saved[2]

        settings.llm_backend = "none"
        cases.append(("backend disabled", "no_backend",
                      asyncio.run(classify_message(msg))))

        settings.llm_backend = "olama"                  # a typo in .env
        cases.append(("backend name typo", "bad_backend",
                      asyncio.run(classify_message(msg))))
        settings.llm_backend = saved[0]

        # Port 0: the OS picks a free one, so the control cannot collide with
        # anything already listening in the container.
        stub = http.server.HTTPServer(("127.0.0.1", 0), EmptyReplyHandler)
        threading.Thread(target=stub.serve_forever, daemon=True).start()
        settings.ollama_url = "http://127.0.0.1:%d" % stub.server_port
        cases.append(("model replies, empty", "parse_failed",
                      asyncio.run(classify_message(msg))))
        settings.ollama_url = saved[2]
    finally:
        settings.llm_backend, settings.ollama_model, settings.ollama_url = saved
        if stub is not None:
            stub.shutdown()

    print("NEGATIVE CONTROL -- can this script see a fallback at all?")
    bad = 0
    for name, expect, r in cases:
        got = r.get("extraction_status")
        counted = got != "ok"              # the same test report() applies
        ok = counted and got == expect
        bad += 0 if ok else 1
        print("  %-20s expected %-22s got %-22s counted as fallback: %s  %s"
              % (name, expect, got, counted, "PASS" if ok else "FAIL"))
    print()
    print("  and the healthy path, restored: %s"
          % asyncio.run(classify_message(msg)).get("extraction_status"))
    print()
    print("%d/%d branches tagged as expected." % (len(cases) - bad, len(cases)))
    if bad:
        print("A FAILING BRANCH HERE INVALIDATES THE 0% HEADLINE -- it means the")
        print("classify run could not have reported a failure even if one occurred.")
        sys.exit(1)
    print("The 0 from `classify` is an absence of failures, not an absence of looking.")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "sample":
        cmd_sample()
    elif cmd == "classify":
        cmd_classify()
    elif cmd == "selftest":
        cmd_selftest()
    elif cmd == "report":
        # Re-render the tables from the per-row results of an earlier run,
        # without re-classifying. The measurement is in RESULT_FILE; this mode
        # is what makes it auditable by someone who does not have the GPU.
        sys.path.insert(0, os.getcwd())
        from app.config import settings
        with open(RESULT_FILE, encoding="utf-8") as fh:
            blob = json.load(fh)
        report(blob["meta"], blob["results"], settings)
    else:
        print(USAGE)


if __name__ == "__main__":
    main()
