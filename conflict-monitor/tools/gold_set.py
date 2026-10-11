#!/usr/bin/env python3
"""The gold set: labelled archive rows and pairs, and the scorer that measures
the pipeline against them.

  python tools/gold_set.py sample [ARCHIVE] [--out DIR] [--force]
      Draw the UNLABELLED set from the production dump: events.jsonl,
      pairs.jsonl and meta.json. Deterministic (SEED). Refuses to overwrite a
      file that already carries labels unless --force.
  python tools/gold_set.py sample ARCHIVE --out data/gold/holdout --seed N --pairs-only --exclude data/gold
      A second draw of pairs only, with another seed, skipping every pair
      that shares an event with the gold pairs: the held-out set a rule
      tuned on the gold pairs is scored on once (C89).
  python tools/gold_set.py check [DIR]
      Validate the files' shape (enum membership, a reason on every
      cannot_tell, a labeller on every label, unique ids) and run the selftest.
      Never judges label quality. Runs in CI.
  python tools/gold_set.py score [DIR] [--run FILE]
      Measure the archive's own stored labels against the gold labels, and a
      classifier run (from `classify`) if one is given. Pairs: precision and
      recall of each matcher at every bar, cannot_tell counted and never
      dropped.
  python tools/gold_set.py classify EVENTS_JSONL OUT_JSONL
      Inside the backend container only: run the project's own
      classify_message() over every gold event and write one row per event.
  python tools/gold_set.py selftest

WHAT A GOLD LABEL IS HERE. A reader read the text of one archive row (or two,
for a pair) and wrote down what the text says: whether it reports an incident
at all, what kind, where it happened as the text names it, how many it says
were killed; for a pair, whether the two texts report the same incident. The
reader saw raw_text, channel and time. The reader did NOT see the archive's
own answers (event_type, location_name, summary) or any similarity score, so
the label is independent of the thing it measures. `cannot_tell` is a label,
not a gap: it carries a reason and the scorer counts it in its own column.

WHO LABELLED. The `labeller` field on every row. The first tranche was
labelled by the assistant that wrote this file (a Claude model, in a Claude
Code session), and `reviewed_by` is null on every one of those rows until a
human reviews them. Two consequences the README spells out: the numbers this
scorer prints are agreement with a second reader, not with the ground; and
these labels must not be used to evaluate a classifier of the same model
family as the labeller, or the evaluation measures self-agreement.

WHAT THE PAIRS CANNOT MEASURE. Every pair is two rows the archive holds. The
old merge destroyed the second text of every pair it DID merge (19,027 of
them), so this set contains the matcher's misses and the pairs outside its
window, never its hits. Recall measured here is recall on that population;
the matcher's precision at ingest is not in this file and cannot be put there.

The archive is read-only and is never written. Nothing under data/gold/ is
loaded by the application at runtime (tools/check_policy.py enforces that).
"""
import argparse
import collections
import datetime as dt
import gzip
import hashlib
import json
import math
import os
import random
import re
import sys
import time
import zlib
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent  # conflict-monitor/
DEFAULT_DIR = PROJECT / "data" / "gold"
DEFAULT_ARCHIVE = "/root/archive/conflict_monitor-20260818.sql.gz"  # READ-ONLY. Never written.

SEED = 20260818  # the other archive_* tools' seed: one seed, one draw

# The regime window: the dump's rows run 2026-02 → 2026-08-18. Six rows carry
# timestamps from 2011–2023 (old content forwarded in 2026) and are out of
# regime; nothing before this date is sampled.
REGIME_START = dt.datetime(2026, 2, 1, tzinfo=dt.timezone.utc)

# Postgres COPY writes NULL as backslash-N. Built, not typed, so a shell cannot
# mangle the escape.
NULLTOK = chr(92) + "N"
TAB = chr(9)
SENTINEL = ("-25", "80")  # the Indian Ocean point the old schema parked unlocated rows on

# ── events: strata and allocation ─────────────────────────────────────────────
EVENTS_TARGET = 350
EVENTS_FLOOR = 25  # a stratum below this measures nothing
EVENT_TYPES = ("military", "diplomatic", "economic", "humanitarian", "cyber")  # the prompt's enum
GOLD_FIELDS = ("is_event", "event_type", "location_name", "killed_reported")

# ── pairs: cells and per-band allocation ──────────────────────────────────────
PAIR_WINDOW_MIN = 180
TIME_CELLS = (("<=15min", 0.0, 15.0), ("15-180min", 15.0, 180.0))  # dedup's window, then the link's
GEO_CELLS = ("both<=50km", "both50-500km", "both_unlocated", "one_unlocated")
SIM_BANDS = (("sim<0.15", -1.0, 0.15), ("sim0.15-0.3", 0.15, 0.3), ("sim0.3-0.4", 0.3, 0.4), ("sim>0.4", 0.4, 2.0))
# Per time×geo cell. Positives are rare and live in the upper bands, so the
# upper bands are drawn harder; the scorer weights nothing here — a pair set
# measures a matcher's curve, not a population rate.
PAIR_ALLOC = {"sim<0.15": 8, "sim0.15-0.3": 10, "sim0.3-0.4": 10, "sim>0.4": 10}
PAIR_LABELS = ("same", "different", "cannot_tell")


# ── the scoring functions, copied from app/services/dedup.py ──────────────────
# tools/ runs on the host without the backend's dependencies (dedup.py imports
# sqlalchemy and geoalchemy2 at module top), so the two pure functions are
# copied here and pinned by selftest to the fixtures tests/unit/test_dedup_pure.py
# pins them to: if dedup.py's _normalize changes, that test and this selftest
# must change together.


def normalize(text: str) -> set[str]:
    """dedup._normalize: lowercase, strip punctuation, words longer than 2."""
    words = re.sub(r"[^\w\s]", "", text.lower()).split()
    return {w for w in words if len(w) > 2}


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    union = len(a | b)
    return len(a & b) / union if union else 0.0


def containment(a: set[str], b: set[str]) -> float:
    """|A∩B| / min(|A|,|B|): how much of the shorter text the longer one holds.
    C76's instrument — a repost that adds a paragraph scores 1.0 here and well
    under 1.0 on Jaccard."""
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


_TAG = re.compile(r"<[^>]+>")
_URL = re.compile(r"https?://\S+|t\.co/\S+")


def clean_text(text: str) -> str:
    """raw_text without HTML tags and URLs — the RSS feeds carry both."""
    return " ".join(_URL.sub(" ", _TAG.sub(" ", text)).split())


MATCHERS = {
    # name: (which text, how to score). The first is the live dedup score.
    "jaccard_summary": ("summary", jaccard),
    "jaccard_raw": ("raw_text", jaccard),
    "containment_raw": ("raw_text", containment),
    "containment_raw_clean": ("raw_clean", containment),
}


def side_text(side: dict, field: str) -> str:
    return clean_text(side["raw_text"]) if field == "raw_clean" else side[field]


def pair_score(matcher: str, a: dict, b: dict) -> float:
    field, fn = MATCHERS[matcher]
    return fn(normalize(side_text(a, field)), normalize(side_text(b, field)))


# ── geometry and time ─────────────────────────────────────────────────────────


def haversine_km(lat1, lon1, lat2, lon2) -> float:
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi, dlmb = p2 - p1, math.radians(lon2 - lon1)
    h = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def time_cell(dt_min: float) -> str | None:
    for name, lo, hi in TIME_CELLS:
        if lo <= dt_min <= hi and (dt_min > lo or lo == 0.0):
            return name
    return None


def geo_cell(a_loc, b_loc) -> str | None:
    """a_loc/b_loc: (lat, lon) or None. None when the pair is not a candidate."""
    if a_loc is None and b_loc is None:
        return "both_unlocated"
    if a_loc is None or b_loc is None:
        return "one_unlocated"
    km = haversine_km(*a_loc, *b_loc)
    if km <= 50:
        return "both<=50km"
    if km <= 500:
        return "both50-500km"
    return None


def sim_band(sim: float) -> str:
    """The live bar is `> 0.4`, so exactly 0.4 sits in the band below it."""
    for name, lo, hi in SIM_BANDS:
        if lo < sim <= hi:
            return name
    return SIM_BANDS[0][0]


# ── statistics ────────────────────────────────────────────────────────────────


def wilson(k, n, z=1.96):
    """Wilson score interval for k/n; behaves at k=0 and k=n."""
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, centre - half), min(1.0, centre + half))


def stratified(strata):
    """Weighted proportion over strata [{N, n, k}]: (p, half_width_95, ok).
    ok is False when the variance is exactly 0 everywhere — an interval of
    zero width is an unmeasured certainty, which this project does not print."""
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
            v = W * W * (1 - s["n"] / s["N"]) * ph * (1 - ph) / (s["n"] - 1)
            var += v
            any_var = any_var or v > 0
    return p, 1.96 * math.sqrt(var), any_var


def pct(k, n):
    if n == 0:
        return "   –  (0)"
    lo, hi = wilson(k, n)
    return "%5.1f%% [%4.1f–%4.1f] (%d/%d)" % (100 * k / n, 100 * lo, 100 * hi, k, n)


# ── reading the dump ──────────────────────────────────────────────────────────


def unescape(v: str) -> str:
    """Undo pg_dump's COPY text escaping (\\n, \\t, \\r, \\\\). Same helper and
    caveat as archive_fallback_rate.py: octal escapes drop the backslash."""
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


def read_events(fh):
    """Yield (fields, index) per row of the events COPY block; columns from the header."""
    idx, cols = None, []
    for line in fh:
        if idx is None:
            if line.startswith("COPY public.events "):
                cols = [c.strip().strip('"') for c in
                        line[line.index("(") + 1:line.rindex(")")].split(",")]
                idx = {c: i for i, c in enumerate(cols)}
            continue
        if line.startswith(chr(92) + "."):
            break
        f = line.rstrip("\n").split(TAB)
        if len(f) < len(cols):
            continue
        yield f, idx


def parse_ts(s: str) -> dt.datetime:
    # "2026-04-14 12:47:16+00" (pg_dump) → aware datetime
    s = s.replace(" ", "T")
    if re.search(r"[+-]\d\d$", s):
        s += ":00"
    return dt.datetime.fromisoformat(s)


def family(source: str) -> str | None:
    if source == "telegram":
        return "telegram"
    if source.startswith("rss_"):
        return "rss"
    return None  # satellite_firms: machine-written rows, no text to label


def coords(f, idx):
    lat, lon = f[idx["lat"]], f[idx["lon"]]
    if lat == NULLTOK or lon == NULLTOK or (lat, lon) == SENTINEL:
        return None
    return (float(lat), float(lon))


class Reservoir:
    """Fixed-size uniform sample of a stream; one seeded Random per stratum."""

    def __init__(self, k, label, seed=SEED):
        self.k, self.items, self.n = k, [], 0
        self.rnd = random.Random(seed ^ zlib.crc32(label.encode("utf-8")))

    def add(self, item):
        self.n += 1
        if len(self.items) < self.k:
            self.items.append(item)
        else:
            j = self.rnd.randrange(self.n)
            if j < self.k:
                self.items[j] = item


# ── sample ────────────────────────────────────────────────────────────────────


def has_labels(path: Path) -> bool:
    if not path.exists():
        return False
    with path.open(encoding="utf-8") as fh:
        return any(json.loads(line).get("gold") is not None for line in fh if line.strip())


def cmd_sample(args):
    out = Path(args.out)
    for name in ("events.jsonl", "pairs.jsonl"):
        if has_labels(out / name) and not args.force:
            raise SystemExit("%s carries labels; refusing to overwrite without --force" % (out / name))
    out.mkdir(parents=True, exist_ok=True)

    # Pass 1: every row's light fields; summaries as word sets for the pairs.
    t0 = time.time()
    light = []
    skipped = collections.Counter()
    with gzip.open(args.archive, "rt", encoding="utf-8", errors="replace") as fh:
        for f, idx in read_events(fh):
            fam = family(f[idx["source"]])
            if fam is None:
                skipped["not text (satellite_firms)"] += 1
                continue
            raw = f[idx["raw_text"]]
            if raw == NULLTOK or not raw.strip():
                skipped["empty raw_text"] += 1
                continue
            ts = parse_ts(f[idx["timestamp"]])
            if ts < REGIME_START:
                skipped["before regime window"] += 1
                continue
            loc = coords(f, idx)
            etype = f[idx["event_type"]]
            summary = unescape(f[idx["summary"]]) if f[idx["summary"]] != NULLTOK else ""
            light.append({
                "id": int(f[idx["id"]]), "source": f[idx["source"]], "channel": f[idx["channel_name"]],
                "ts": ts, "event_type": etype, "loc": loc, "fam": fam,
                "location_name": f[idx["location_name"]] if f[idx["location_name"]] != NULLTOK else "",
                "severity": None if f[idx["severity"]] == NULLTOK else int(f[idx["severity"]]),
                "report_count": None if f[idx["report_count"]] == NULLTOK else int(f[idx["report_count"]]),
                "summary": summary, "words": normalize(summary),
            })
    print("pass 1: %d rows in regime with text; skipped %s; %.0fs"
          % (len(light), dict(skipped), time.time() - t0))

    # Events: strata = family × located × military-or-not.
    def stratum(r):
        return "%s/%s/%s" % (r["fam"], "located" if r["loc"] else "unlocated",
                             "military" if r["event_type"] == "military" else "other")
    N_h = collections.Counter(stratum(r) for r in light)
    N = sum(N_h.values())
    alloc = {h: min(n, max(EVENTS_FLOOR, round(EVENTS_TARGET * n / N))) for h, n in N_h.items()}
    ev_res = {h: Reservoir(k, "events/" + h, args.seed) for h, k in alloc.items()}
    for r in light:
        ev_res[stratum(r)].add(r)
    chosen_events = {r["id"]: (h, r) for h in ev_res for r in ev_res[h].items}
    print("events: %d strata, %d rows allocated (target %d, floor %d)"
          % (len(alloc), sum(alloc.values()), EVENTS_TARGET, EVENTS_FLOOR))

    # Pairs: same event_type, b within PAIR_WINDOW_MIN after a, cell by time × geo × sim.
    # --exclude: no pair that shares an event with a pair already drawn there, so
    # a rule tuned on those texts meets none of them here.
    excluded = set()
    if args.exclude:
        for r in read_jsonl(Path(args.exclude) / "pairs.jsonl"):
            excluded.update((r["a"]["id"], r["b"]["id"]))
    n_excluded = 0
    t0 = time.time()
    light.sort(key=lambda r: (r["ts"], r["id"]))
    pair_res = {}
    cell_N = collections.Counter()
    n_cand = 0
    for i, a in enumerate(light):
        for j in range(i + 1, len(light)):
            b = light[j]
            dmin = (b["ts"] - a["ts"]).total_seconds() / 60.0
            if dmin > PAIR_WINDOW_MIN:
                break
            if a["event_type"] != b["event_type"]:
                continue
            if a["id"] in excluded or b["id"] in excluded:
                n_excluded += 1
                continue
            tc = time_cell(dmin)
            gc = geo_cell(a["loc"], b["loc"])
            if tc is None or gc is None:
                continue
            sim = jaccard(a["words"], b["words"])
            band = sim_band(sim)
            key = "%s/%s/%s" % (tc, gc, band)
            n_cand += 1
            cell_N[key] += 1
            if key not in pair_res:
                pair_res[key] = Reservoir(PAIR_ALLOC[band], "pairs/" + key, args.seed)
            km = haversine_km(*a["loc"], *b["loc"]) if a["loc"] and b["loc"] else None
            pair_res[key].add((a["id"], b["id"], round(dmin, 1), None if km is None else round(km, 1)))
    chosen_pairs = [(key, item) for key in sorted(pair_res) for item in pair_res[key].items]
    print("pairs: %d candidate pairs in %d cells; %d drawn; %d skipped for sharing an event with %s; %.0fs"
          % (n_cand, len(cell_N), len(chosen_pairs), n_excluded, args.exclude or "nothing", time.time() - t0))

    # Pass 2: raw_text for every chosen row.
    need = (set() if args.pairs_only else set(chosen_events)) | {i for _, it in chosen_pairs for i in it[:2]}
    texts = {}
    t0 = time.time()
    with gzip.open(args.archive, "rt", encoding="utf-8", errors="replace") as fh:
        for f, idx in read_events(fh):
            i = int(f[idx["id"]])
            if i in need:
                texts[i] = unescape(f[idx["raw_text"]])
                if len(texts) == len(need):
                    break
    print("pass 2: %d texts fetched; %.0fs" % (len(texts), time.time() - t0))
    by_id = {r["id"]: r for r in light}

    def row(r, with_geo):
        d = {"id": r["id"], "source": r["source"], "channel": r["channel"],
             "reported_at": r["ts"].isoformat(), "raw_text": texts[r["id"]]}
        if with_geo:
            d["event_type"] = r["event_type"]
            d["location_name"] = r["location_name"]
            d["lat"], d["lon"] = (r["loc"] or (None, None))
            d["summary"] = r["summary"]
        return d

    events_out = []
    for h in ([] if args.pairs_only else sorted(ev_res)):  # --pairs-only fetched no event texts
        for r in sorted(ev_res[h].items, key=lambda r: r["id"]):
            events_out.append({
                **row(r, False), "stratum": h,
                # The archive's own answers, stored for the scorer; the labeller does not read them.
                "archive": {"event_type": r["event_type"], "location_name": r["location_name"],
                            "located": r["loc"] is not None, "severity": r["severity"],
                            "report_count": r["report_count"], "summary": r["summary"]},
                "gold": None, "labeller": None, "labelled_at": None, "reviewed_by": None,
            })
    pairs_out = []
    for key, (ia, ib, dmin, km) in chosen_pairs:
        pairs_out.append({
            "pair_id": "%d-%d" % (ia, ib), "stratum": key, "dt_min": dmin, "km": km,
            "a": row(by_id[ia], True), "b": row(by_id[ib], True),
            "gold": None, "labeller": None, "labelled_at": None, "reviewed_by": None,
        })

    md5 = hashlib.md5()
    with open(args.archive, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            md5.update(chunk)
    meta = {
        "archive": os.path.basename(args.archive), "archive_md5": md5.hexdigest(),
        "drawn_at": dt.date.today().isoformat(), "seed": args.seed,
        "pairs_only": bool(args.pairs_only), "excluded_pairs_sharing_an_event_with": args.exclude,
        "pairs_skipped_as_excluded": n_excluded,
        "regime_start": REGIME_START.isoformat(), "skipped": dict(skipped),
        "events": {"target": EVENTS_TARGET, "floor": EVENTS_FLOOR,
                   "strata": {h: {"N": N_h[h], "n": len(ev_res[h].items)} for h in sorted(ev_res)}},
        "pairs": {"window_min": PAIR_WINDOW_MIN, "alloc_per_cell_by_band": PAIR_ALLOC,
                  "candidates": n_cand,
                  "cells": {k: {"N": cell_N[k], "n": len(pair_res[k].items)} for k in sorted(pair_res)}},
    }
    if not args.pairs_only:
        write_jsonl(out / "events.jsonl", events_out)
    write_jsonl(out / "pairs.jsonl", pairs_out)
    (out / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print()
    print("%-36s %8s %8s" % ("event stratum", "N", "n"))
    for h in sorted(ev_res):
        print("  %-34s %8d %8d" % (h, N_h[h], len(ev_res[h].items)))
    print()
    print("%-44s %8s %8s" % ("pair cell", "N", "n"))
    for k in sorted(pair_res):
        print("  %-42s %8d %8d" % (k, cell_N[k], len(pair_res[k].items)))
    print()
    print("wrote %s%d pairs to %s" % ("" if args.pairs_only else "%d events, " % len(events_out), len(pairs_out), out))


def write_jsonl(path: Path, rows):
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


def read_jsonl(path: Path):
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


# ── check: shape, never quality ───────────────────────────────────────────────


def validate_event(r, seen) -> list[str]:
    errs = []
    for k in ("id", "source", "channel", "reported_at", "raw_text", "stratum", "archive"):
        if k not in r:
            errs.append("missing %s" % k)
    if errs:
        return errs
    if r["id"] in seen:
        errs.append("duplicate id %s" % r["id"])
    seen.add(r["id"])
    if not str(r["raw_text"]).strip():
        errs.append("empty raw_text")
    g = r.get("gold")
    if g is None:
        return errs
    if not r.get("labeller") or not r.get("labelled_at"):
        errs.append("labelled row without labeller/labelled_at")
    for k in GOLD_FIELDS:
        if k not in g:
            errs.append("gold missing %s" % k)
    if errs:
        return errs
    ct = g.get("cannot_tell", {})
    if not isinstance(ct, dict):
        errs.append("cannot_tell must be a dict of field -> reason")
        ct = {}
    for k, why in ct.items():
        if k not in GOLD_FIELDS:
            errs.append("cannot_tell names an unknown field %r" % k)
        if not isinstance(why, str) or not why.strip():
            errs.append("cannot_tell[%s] has no reason" % k)
    if not isinstance(g["is_event"], bool):
        errs.append("is_event must be true/false")
    if g["event_type"] is not None and g["event_type"] not in EVENT_TYPES:
        errs.append("event_type %r not in %s" % (g["event_type"], EVENT_TYPES))
    if g["location_name"] is not None and not isinstance(g["location_name"], str):
        errs.append("location_name must be a string ('' = the text names no place) or null")
    kr = g["killed_reported"]
    if kr is not None and (not isinstance(kr, int) or isinstance(kr, bool) or kr < 0):
        errs.append("killed_reported must be a non-negative integer or null")
    # null in a gold field means "cannot tell", and that needs the reason.
    for k in ("event_type", "location_name"):
        if g[k] is None and k not in ct:
            errs.append("%s is null but cannot_tell gives no reason" % k)
    return errs


def validate_pair(r, seen) -> list[str]:
    errs = []
    for k in ("pair_id", "stratum", "a", "b", "dt_min"):
        if k not in r:
            errs.append("missing %s" % k)
    if errs:
        return errs
    if r["pair_id"] in seen:
        errs.append("duplicate pair_id %s" % r["pair_id"])
    seen.add(r["pair_id"])
    for side in ("a", "b"):
        for k in ("id", "channel", "reported_at", "raw_text", "summary", "event_type"):
            if k not in r[side]:
                errs.append("%s missing %s" % (side, k))
    g = r.get("gold")
    if g is None:
        return errs
    if not r.get("labeller") or not r.get("labelled_at"):
        errs.append("labelled pair without labeller/labelled_at")
    if g.get("label") not in PAIR_LABELS:
        errs.append("label %r not in %s" % (g.get("label"), PAIR_LABELS))
    if g.get("label") == "cannot_tell" and not str(g.get("reason", "")).strip():
        errs.append("cannot_tell without a reason")
    return errs


def cmd_check(args) -> int:
    d = Path(args.dir)
    bad = 0
    for name, fn in (("events.jsonl", validate_event), ("pairs.jsonl", validate_pair)):
        path = d / name
        if not path.exists():
            print("FAIL %s: missing" % path)
            bad += 1
            continue
        rows, seen, labelled = read_jsonl(path), set(), 0
        for n, r in enumerate(rows, 1):
            errs = fn(r, seen)
            if errs:
                bad += 1
                print("FAIL %s line %d (%s): %s" % (name, n, r.get("id", r.get("pair_id")), "; ".join(errs)))
            labelled += r.get("gold") is not None
        print("ok   %s: %d rows, %d labelled" % (name, len(rows), labelled))
    holdout = d / "holdout" / "pairs.jsonl"
    if holdout.exists():
        rows, seen, labelled = read_jsonl(holdout), set(), 0
        for n, r in enumerate(rows, 1):
            errs = validate_pair(r, seen)
            if errs:
                bad += 1
                print("FAIL holdout/pairs.jsonl line %d (%s): %s" % (n, r.get("pair_id"), "; ".join(errs)))
            labelled += r.get("gold") is not None
        print("ok   holdout/pairs.jsonl: %d rows, %d labelled" % (len(rows), labelled))
    bad += check_link_rule(d)
    bad += selftest()
    return 1 if bad else 0


# The shipped link rule's counts on the labelled pairs (TP, FP, FN), by rule
# version, as FINDINGS.md Measured facts "Link between events" states them.
# A rule, a bar or a label that changes must change this and the register
# together, never one without the other.
LINK_RULE_PINNED = {"v1": (74, 12, 38)}


def check_link_rule(d: Path) -> int:
    """backend/app/services/linkrule.py, imported rather than copied, scores
    the labelled pairs exactly as pinned. The one place the gold set meets
    the shipped rule; the backend suite never opens the gold files."""
    rows = [r for r in read_jsonl(d / "pairs.jsonl") if r.get("gold") is not None]
    if not rows:
        return 0
    sys.path.insert(0, str(PROJECT / "backend"))
    from app.services import linkrule as lr
    tp = fp = fn = 0
    for r in rows:
        a = lr.assess(r["a"]["raw_text"], r["a"]["location_name"], r["b"]["raw_text"], r["b"]["location_name"])
        label = r["gold"]["label"]
        tp += a.linked and label == "same"
        fp += a.linked and label == "different"
        fn += (not a.linked) and label == "same"
    want = LINK_RULE_PINNED.get(lr.RULE)
    if want is None:
        print("FAIL link rule %s has no pinned counts; measure it and pin them" % lr.RULE)
        return 1
    if (tp, fp, fn) != want:
        print("FAIL link rule %s on the pairs: TP %d FP %d FN %d, pinned %s" % (lr.RULE, tp, fp, fn, want))
        return 1
    print("ok   link rule %s on the pairs: TP %d FP %d FN %d, as pinned" % (lr.RULE, tp, fp, fn))
    return 0


# ── score ─────────────────────────────────────────────────────────────────────


def norm_loc(s: str) -> str:
    s = re.sub(r"[^\w\s]", " ", (s or "").casefold())
    s = re.sub(r"^(the|al|el)\s+", "", " ".join(s.split()))
    return s


def loc_compatible(a: str, b: str) -> bool:
    """Equal after normalisation, or one names a part of the other
    ("Beirut" ~ "southern Beirut"). Not a geocoder: "Lebanon" ≠ "Beirut"."""
    a, b = norm_loc(a), norm_loc(b)
    if not a or not b:
        return False
    return a == b or (" " + a + " ") in (" " + b + " ") or (" " + b + " ") in (" " + a + " ")


def archive_named(loc: str) -> bool:
    return bool(loc) and loc.strip().casefold() != "unknown"


def weighted_line(label, strata_counts, meta_strata):
    """strata_counts: {stratum: (k, n)}; weights from meta N_h."""
    strata = [{"N": meta_strata[h]["N"], "n": n, "k": k} for h, (k, n) in strata_counts.items() if h in meta_strata]
    k = sum(s["k"] for s in strata)
    n = sum(s["n"] for s in strata)
    p, hw, ok = stratified(strata)
    iv = ("±%.1f" % (100 * hw)) if ok else "(no variance: every stratum degenerate)"
    print("  %-58s weighted %5.1f%% %s   raw %s" % (label, 100 * p, iv, pct(k, n)))


def _counts(pred, groups):
    """{stratum: (k, n)} over rows where pred is not None (None = not applicable)."""
    out = {}
    for h, rs in groups.items():
        verdicts = [pred(r) for r in rs]
        sel = [v for v in verdicts if v is not None]
        out[h] = (sum(1 for v in sel if v), len(sel))
    return out


def score_events(d: Path, meta, run_rows):
    rows = [r for r in read_jsonl(d / "events.jsonl") if r.get("gold") is not None]
    meta_strata = meta["events"]["strata"]
    print("EVENTS  %d labelled of %d drawn; weights W_h = N_h/N from meta.json" % (
        len(rows), sum(s["n"] for s in meta_strata.values())))
    if not rows:
        print("  nothing labelled yet")
        return
    by_h = collections.defaultdict(list)
    for r in rows:
        by_h[r["stratum"]].append(r)
    print("  %-36s %6s %6s" % ("stratum", "N", "lab."))
    for h in sorted(meta_strata):
        print("  %-36s %6d %6d" % (h, meta_strata[h]["N"], len(by_h.get(h, []))))
    ct = collections.Counter(k for r in rows for k in r["gold"].get("cannot_tell", {}))
    print("  cannot_tell: %s" % (dict(ct) or "none"))
    print()

    g = lambda r: r["gold"]
    a = lambda r: r["archive"]
    print("  The archive against the gold labels (what is stored vs what the text says):")
    weighted_line("rows whose text reports no incident (noise the gate let in)",
                  _counts(lambda r: not g(r)["is_event"], by_h), meta_strata)
    ev = {h: [r for r in rs if g(r)["is_event"]] for h, rs in by_h.items()}
    weighted_line("event_type agrees (incident rows, both sides decided)",
                  _counts(lambda r: None if g(r)["event_type"] is None else a(r)["event_type"] == g(r)["event_type"], ev),
                  meta_strata)
    conf = collections.Counter((a(r)["event_type"], g(r)["event_type"]) for rs in ev.values() for r in rs
                               if g(r)["event_type"] is not None and a(r)["event_type"] != g(r)["event_type"])
    if conf:
        print("    disagreements (archive → gold): %s" % ", ".join(
            "%s→%s ×%d" % (x, y, n) for (x, y), n in conf.most_common()))
    named = {h: [r for r in rs if g(r)["location_name"]] for h, rs in ev.items()}
    unnamed = {h: [r for r in rs if g(r)["location_name"] == ""] for h, rs in ev.items()}
    weighted_line("text names a place (incident rows)",
                  _counts(lambda r: None if g(r)["location_name"] is None else bool(g(r)["location_name"]), ev), meta_strata)
    weighted_line("  …and the archive stored a place too (not Unknown)",
                  _counts(lambda r: archive_named(a(r)["location_name"]), named), meta_strata)
    weighted_line("  …and the stored place is the text's place (compatible)",
                  _counts(lambda r: loc_compatible(a(r)["location_name"], g(r)["location_name"]), named), meta_strata)
    weighted_line("  …and the stored place is exactly the text's place",
                  _counts(lambda r: norm_loc(a(r)["location_name"]) == norm_loc(g(r)["location_name"]), named), meta_strata)
    weighted_line("text names NO place, yet the archive stored one",
                  _counts(lambda r: archive_named(a(r)["location_name"]), unnamed), meta_strata)
    weighted_line("archive says Unknown while the text names a place",
                  _counts(lambda r: not archive_named(a(r)["location_name"]), named), meta_strata)
    weighted_line("text states a death toll (incident rows)",
                  _counts(lambda r: g(r)["killed_reported"] is not None, ev), meta_strata)
    print("  killed_reported: the dump predates the column, so the archive cannot be scored on it; a run can.")
    print()

    if run_rows is None:
        print("  No classifier run given (--run FILE from `classify`): the current classifier is not scored.")
        return
    run = {r["id"]: r for r in run_rows}
    rows_r = [r for r in rows if r["id"] in run]
    model = collections.Counter(run[r["id"]].get("extraction_model") for r in rows_r)
    status = collections.Counter(run[r["id"]].get("extraction_status") for r in rows_r)
    print("  The current classifier against the gold labels: %d of %d gold rows in the run; model %s; status %s"
          % (len(rows_r), len(rows), dict(model), dict(status)))
    ok = {h: [r for r in rs if r["id"] in run and run[r["id"]].get("extraction_status") == "ok"] for h, rs in by_h.items()}
    rr = lambda r: run[r["id"]]
    weighted_line("noise verdict agrees (is_noise == not is_event)",
                  _counts(lambda r: bool(rr(r).get("is_noise")) == (not g(r)["is_event"]), ok), meta_strata)
    weighted_line("  noise missed: text reports no incident, model kept it",
                  _counts(lambda r: None if g(r)["is_event"] else not rr(r).get("is_noise"), ok), meta_strata)
    weighted_line("  incident dropped: text reports one, model called it noise",
                  _counts(lambda r: None if not g(r)["is_event"] else bool(rr(r).get("is_noise")), ok), meta_strata)
    kept = {h: [r for r in rs if g(r)["is_event"] and not rr(r).get("is_noise")] for h, rs in ok.items()}
    weighted_line("event_type agrees (kept incident rows)",
                  _counts(lambda r: None if g(r)["event_type"] is None else rr(r).get("event_type") == g(r)["event_type"], kept),
                  meta_strata)
    conf = collections.Counter((rr(r).get("event_type"), g(r)["event_type"]) for rs in kept.values() for r in rs
                               if g(r)["event_type"] is not None and rr(r).get("event_type") != g(r)["event_type"])
    if conf:
        print("    disagreements (model → gold): %s" % ", ".join(
            "%s→%s ×%d" % (x, y, n) for (x, y), n in conf.most_common()))
    knamed = {h: [r for r in rs if g(r)["location_name"]] for h, rs in kept.items()}
    kunnamed = {h: [r for r in rs if g(r)["location_name"] == ""] for h, rs in kept.items()}
    weighted_line("text names a place and the model named one",
                  _counts(lambda r: archive_named(rr(r).get("location_name") or ""), knamed), meta_strata)
    weighted_line("  …the model's place is the text's place (compatible)",
                  _counts(lambda r: loc_compatible(rr(r).get("location_name") or "", g(r)["location_name"]), knamed), meta_strata)
    weighted_line("  …exactly",
                  _counts(lambda r: norm_loc(rr(r).get("location_name") or "") == norm_loc(g(r)["location_name"]), knamed),
                  meta_strata)
    weighted_line("text names NO place, yet the model named one",
                  _counts(lambda r: archive_named(rr(r).get("location_name") or ""), kunnamed), meta_strata)
    weighted_line("killed_reported agrees (null==null, or the same number)",
                  _counts(lambda r: rr(r).get("killed_reported") == g(r)["killed_reported"], kept), meta_strata)
    stated = {h: [r for r in rs if g(r)["killed_reported"] is not None] for h, rs in kept.items()}
    unstated = {h: [r for r in rs if g(r)["killed_reported"] is None] for h, rs in kept.items()}
    weighted_line("  text states a toll: model copied the same number",
                  _counts(lambda r: rr(r).get("killed_reported") == g(r)["killed_reported"], stated), meta_strata)
    weighted_line("  text states a toll: model returned null",
                  _counts(lambda r: rr(r).get("killed_reported") is None, stated), meta_strata)
    weighted_line("  text states NO toll: model invented a number",
                  _counts(lambda r: rr(r).get("killed_reported") is not None, unstated), meta_strata)
    print()


THRESHOLDS = [round(0.1 * i, 1) for i in range(1, 10)]


def pr_at(scored, t):
    """scored: [(score, label)]. Precision/recall of `score > t` for `same`
    against `different`; cannot_tell is its own count, never dropped."""
    tp = sum(1 for s, l in scored if l == "same" and s > t)
    fp = sum(1 for s, l in scored if l == "different" and s > t)
    fn = sum(1 for s, l in scored if l == "same" and s <= t)
    ct = sum(1 for s, l in scored if l == "cannot_tell" and s > t)
    p = tp / (tp + fp) if tp + fp else None
    r = tp / (tp + fn) if tp + fn else None
    f1 = (2 * p * r / (p + r)) if p and r else None
    return tp, fp, fn, ct, p, r, f1


def fmt(x):
    return "   –" if x is None else "%4.2f" % x


def _live_rule_sees(stratum: str) -> bool:
    tc, gc, _ = stratum.split("/")
    return tc == "<=15min" and gc in ("both<=50km", "both_unlocated")


def score_pairs(d: Path, meta):
    rows = [r for r in read_jsonl(d / "pairs.jsonl") if r.get("gold") is not None]
    print("PAIRS   %d labelled of %d drawn" % (len(rows), sum(c["n"] for c in meta["pairs"]["cells"].values())))
    if not rows:
        print("  nothing labelled yet")
        return
    lab = collections.Counter(r["gold"]["label"] for r in rows)
    print("  labels: %s" % dict(lab))
    print("  Every pair is two rows the archive kept apart: the old matcher's misses and the pairs outside its window.")
    print("  Its hits are not here (the merge destroyed their second text), so no number below is its precision at ingest.")
    print()
    cells = collections.defaultdict(collections.Counter)
    for r in rows:
        tc, gc, _ = r["stratum"].split("/")
        cells[(tc, gc)][r["gold"]["label"]] += 1
    print("  %-12s %-16s %5s %5s %5s" % ("time", "geometry", "same", "diff", "c/t"))
    for (tc, gc), c in sorted(cells.items()):
        print("  %-12s %-16s %5d %5d %5d" % (tc, gc, c["same"], c["different"], c["cannot_tell"]))
    print()
    for m in MATCHERS:
        scored = [(pair_score(m, r["a"], r["b"]), r["gold"]["label"]) for r in rows]
        print("  %s  (score > bar ⇒ same)" % m)
        print("    %4s %4s %4s %4s %4s   %5s %5s %5s" % ("bar", "TP", "FP", "FN", "c/t>", "P", "R", "F1"))
        for t in THRESHOLDS:
            tp, fp, fn, ct, p, r, f1 = pr_at(scored, t)
            print("    %4.1f %4d %4d %4d %4d   %5s %5s %5s" % (t, tp, fp, fn, ct, fmt(p), fmt(r), fmt(f1)))
        print()
    # The live rule, as dedup.py applies it: same type (by construction), within
    # 15 min, both located within 50 km or both unlocated, jaccard_summary > 0.4.
    live = [(pair_score("jaccard_summary", r["a"], r["b"]), r["gold"]["label"]) for r in rows if _live_rule_sees(r["stratum"])]
    tp, fp, fn, ct, p, r, f1 = pr_at(live, 0.4)
    print("  the live rule on the pairs it can see (<=15 min, both<=50km or both unlocated, jaccard_summary > 0.4):")
    print("    TP %d  FP %d  FN %d  cannot_tell above bar %d   P %s  R %s" % (tp, fp, fn, ct, fmt(p), fmt(r)))
    unseen = sum(1 for rr in rows if rr["gold"]["label"] == "same" and not _live_rule_sees(rr["stratum"]))
    print("    same-incident pairs the rule can never see (outside 15 min, one side unlocated, or 50–500 km apart): %d" % unseen)
    print()
    score_link_rule(rows, meta)


def score_link_rule(rows, meta):
    """The link between events, as shipped: backend/app/services/linkrule.py
    is imported, not copied, so this is the rule's number. Its bar and vetoes
    were chosen on these pairs, so the figures are in-sample."""
    sys.path.insert(0, str(PROJECT / "backend"))
    from app.services import linkrule as lr
    assessed = [(lr.assess(r["a"]["raw_text"], r["a"]["location_name"], r["b"]["raw_text"], r["b"]["location_name"]), r)
                for r in rows]
    print("  the link rule %s as shipped (containment over clean raw_text, place and towns vetoes); * = its bar %.1f" % (lr.RULE, lr.BAR))
    print("    %5s %4s %4s %4s %4s   %5s %5s %5s  | unseen by the live rule: %4s %4s %4s   %5s %5s"
          % ("bar", "TP", "FP", "FN", "c/t>", "P", "R", "F1", "TP", "FP", "FN", "P", "R"))
    for t in (0.3, 0.4, 0.5, 0.6, 0.7):
        def hit(a, t=t):
            return a.score > t and a.place != "different" and a.towns != "different"
        tp, fp, fn, ct, p, r, f1 = pr_at([(1.0 if hit(a) else 0.0, rr["gold"]["label"]) for a, rr in assessed], 0.5)
        tp2, fp2, fn2, _, p2, r2, _ = pr_at(
            [(1.0 if hit(a) else 0.0, rr["gold"]["label"]) for a, rr in assessed if not _live_rule_sees(rr["stratum"])], 0.5)
        print("    %4.1f%s %4d %4d %4d %4d   %5s %5s %5s  |                          %4d %4d %4d   %5s %5s"
              % (t, "*" if t == lr.BAR else " ", tp, fp, fn, ct, fmt(p), fmt(r), fmt(f1), tp2, fp2, fn2, fmt(p2), fmt(r2)))
    for key in ("place", "towns"):
        c = collections.Counter(rr["gold"]["label"] for a, rr in assessed if a.score > lr.BAR and getattr(a, key) == "different")
        print("    the %s veto, among pairs over the bar on text alone: %s" % (key, dict(c)))
    cells = meta["pairs"]["cells"]
    total = 0.0
    for stratum, c in cells.items():
        rs = [a for a, rr in assessed if rr["stratum"] == stratum]
        if rs:
            total += c["N"] * sum(1 for a in rs if a.linked) / len(rs)
    print("    expected pairs linked on the archive, by the strata weights (8-10 labelled per cell, so an order of magnitude): ~%.0f of %d candidate pairs"
          % (total, sum(c["N"] for c in cells.values())))
    print()


def cmd_score(args):
    d = Path(args.dir)
    meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    print("gold set drawn %s from %s (md5 %s), seed %d" % (
        meta["drawn_at"], meta["archive"], meta["archive_md5"][:8], meta["seed"]))
    labellers = collections.Counter(r.get("labeller") for f in ("events.jsonl", "pairs.jsonl")
                                    for r in read_jsonl(d / f) if r.get("gold") is not None)
    reviewed = sum(1 for f in ("events.jsonl", "pairs.jsonl") for r in read_jsonl(d / f) if r.get("reviewed_by"))
    print("labellers: %s; rows reviewed by a human: %d" % (dict(labellers), reviewed))
    print("These numbers are agreement with the labeller, not with the ground, until the review count is not 0.")
    print()
    run = read_jsonl(Path(args.run)) if args.run else None
    score_events(d, meta, run)
    score_pairs(d, meta)


# ── classify: inside the backend container ────────────────────────────────────


def cmd_classify(args):
    sys.path.insert(0, os.getcwd())  # /app in the container
    import asyncio
    from app.config import settings
    from app.services.classifier import classify_message

    rows = read_jsonl(Path(args.events))
    print("classifying %d gold events with the project's own classify_message(); backend=%s model=%s url=%s"
          % (len(rows), settings.llm_backend, settings.ollama_model, settings.ollama_url))

    async def run():
        out, t0 = [], time.time()
        for i, row in enumerate(rows, 1):
            t = time.time()
            r = await classify_message(row["raw_text"])
            out.append({
                "id": row["id"], "secs": round(time.time() - t, 3),
                "extraction_status": r.get("extraction_status"), "extraction_model": r.get("extraction_model"),
                "is_noise": r.get("is_noise"), "event_type": r.get("event_type"), "severity": r.get("severity"),
                "killed_reported": r.get("killed_reported"), "location_name": r.get("location_name"),
                "summary": (r.get("summary") or "")[:300],
            })
            if i % 25 == 0 or i == len(rows):
                el = time.time() - t0
                print("  %4d/%d  %.0fs elapsed, %.2fs/msg, ~%.0fs left" % (i, len(rows), el, el / i, el / i * (len(rows) - i)),
                      flush=True)
        return out

    results = asyncio.run(run())
    write_jsonl(Path(args.out), results)
    print("wrote %d rows to %s" % (len(results), args.out))


# ── selftest ──────────────────────────────────────────────────────────────────


def selftest() -> int:
    fails = []
    total = [0]

    def check(cond, what):
        total[0] += 1
        if not cond:
            fails.append(what)

    # Pinned to tests/unit/test_dedup_pure.py: the copies must score what dedup.py scores.
    check(normalize("Israeli F-35s hit Natanz!") == {"israeli", "f35s", "hit", "natanz"}, "normalize")
    check(jaccard(normalize("aaa bbb ccc"), normalize("bbb ccc ddd eee")) == 0.4, "jaccard pins the 0.4 fixture")
    check(jaccard(set(), set()) == 0.0, "jaccard of empty sides")
    check(containment({"a", "b", "c"}, {"a", "b"}) == 1.0, "containment: short side fully held")
    check(abs(containment({"a", "b", "c"}, {"a", "x", "y"}) - 1 / 3) < 1e-9, "containment 1/3")
    check(containment(set(), {"a"}) == 0.0, "containment of an empty side")
    check(clean_text("<p>Hi</p> https://x.co/abc there") == "Hi there", "clean_text strips tags and urls")
    check(sim_band(0.4) == "sim0.3-0.4" and sim_band(0.41) == "sim>0.4" and sim_band(0.0) == "sim<0.15", "sim bands mirror > 0.4")
    check(time_cell(0.0) == "<=15min" and time_cell(15.0) == "<=15min" and time_cell(15.1) == "15-180min"
          and time_cell(181) is None, "time cells")
    check(geo_cell(None, None) == "both_unlocated" and geo_cell((1, 1), None) == "one_unlocated", "geo cells: unlocated")
    check(geo_cell((35.69, 51.39), (35.70, 51.40)) == "both<=50km", "geo cells: near")
    km = haversine_km(35.6892, 51.3890, 32.6539, 51.6660)  # Tehran – Isfahan ≈ 340 km
    check(330 < km < 350 and geo_cell((35.6892, 51.3890), (32.6539, 51.6660)) == "both50-500km", "haversine / geo cells: far")
    check(geo_cell((0, 0), (10, 10)) is None, "geo cells: >500 km is not a candidate")
    scored = [(0.9, "same"), (0.5, "same"), (0.3, "same"), (0.6, "different"), (0.1, "different"), (0.7, "cannot_tell")]
    tp, fp, fn, ct, p, r, f1 = pr_at(scored, 0.4)
    check((tp, fp, fn, ct) == (2, 1, 1, 1) and abs(p - 2 / 3) < 1e-9 and abs(r - 2 / 3) < 1e-9, "pr_at counts cannot_tell separately")
    check(loc_compatible("Beirut", "southern Beirut") and loc_compatible("Al-Dahieh", "Dahieh")
          and not loc_compatible("Lebanon", "Beirut") and not loc_compatible("", "Beirut"), "loc_compatible")
    check(archive_named("Beirut") and not archive_named("Unknown") and not archive_named(""), "archive_named")
    base = {"id": 1, "source": "telegram", "channel": "c", "reported_at": "2026-03-01T00:00:00+00:00",
            "raw_text": "x", "stratum": "s", "archive": {}, "labeller": "L", "labelled_at": "2026-10-10"}
    good = {**base, "gold": {"is_event": True, "event_type": "military", "location_name": "Natanz",
                             "killed_reported": None, "cannot_tell": {}}}
    check(validate_event(good, set()) == [], "a well-formed event row validates")
    check(validate_event({**good, "gold": {**good["gold"], "event_type": None}}, set()), "null event_type needs a cannot_tell reason")
    check(validate_event({**good, "gold": {**good["gold"], "event_type": None, "cannot_tell": {"event_type": ""}}}, set()),
          "an empty cannot_tell reason is rejected")
    check(validate_event({**good, "gold": {**good["gold"], "event_type": "protest"}}, set()), "event_type outside the enum is rejected")
    check(validate_event({**good, "gold": {**good["gold"], "killed_reported": True}}, set()), "a boolean toll is rejected")
    check(validate_event({**good, "labeller": None}, set()), "a label without a labeller is rejected")
    check(validate_event(good, {1}), "a duplicate id is rejected")
    side = {"id": 1, "channel": "c", "reported_at": "t", "raw_text": "x", "summary": "s", "event_type": "military"}
    pgood = {"pair_id": "1-2", "stratum": "<=15min/both_unlocated/sim>0.4", "a": side, "b": {**side, "id": 2},
             "dt_min": 1.0, "labeller": "L", "labelled_at": "2026-10-10", "gold": {"label": "same", "reason": ""}}
    check(validate_pair(pgood, set()) == [], "a well-formed pair validates")
    check(validate_pair({**pgood, "gold": {"label": "cannot_tell", "reason": ""}}, set()), "cannot_tell without a reason is rejected")
    check(validate_pair({**pgood, "gold": {"label": "maybe", "reason": ""}}, set()), "a label outside the enum is rejected")
    p, hw, ok = stratified([{"N": 100, "n": 10, "k": 5}, {"N": 100, "n": 10, "k": 5}])
    check(abs(p - 0.5) < 1e-9 and ok, "stratified estimate")
    check(not stratified([{"N": 100, "n": 10, "k": 0}])[2], "stratified: zero variance is flagged, not printed as certainty")
    lo, hi = wilson(0, 10)
    check(lo == 0.0 and hi > 0.25, "wilson at k=0 has width")
    for f in fails:
        print("FAIL selftest: %s" % f)
    print("ok   selftest: %d assertions%s" % (total[0], "" if not fails else ", %d failed" % len(fails)))
    return len(fails)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sample")
    s.add_argument("archive", nargs="?", default=DEFAULT_ARCHIVE)
    s.add_argument("--out", default=str(DEFAULT_DIR))
    s.add_argument("--force", action="store_true")
    s.add_argument("--seed", type=int, default=SEED)
    s.add_argument("--pairs-only", action="store_true", help="write pairs.jsonl and meta.json only")
    s.add_argument("--exclude", default=None, help="a gold dir whose pairs' events this draw must not touch")
    c = sub.add_parser("check")
    c.add_argument("dir", nargs="?", default=str(DEFAULT_DIR))
    sc = sub.add_parser("score")
    sc.add_argument("dir", nargs="?", default=str(DEFAULT_DIR))
    sc.add_argument("--run", default=None)
    cl = sub.add_parser("classify")
    cl.add_argument("events")
    cl.add_argument("out")
    sub.add_parser("selftest")
    args = ap.parse_args()
    if args.cmd == "sample":
        cmd_sample(args)
        return 0
    if args.cmd == "check":
        return cmd_check(args)
    if args.cmd == "score":
        cmd_score(args)
        return 0
    if args.cmd == "classify":
        cmd_classify(args)
        return 0
    return 1 if selftest() else 0


if __name__ == "__main__":
    sys.exit(main())
