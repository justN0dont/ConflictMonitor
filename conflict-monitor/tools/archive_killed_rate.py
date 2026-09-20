# How often does a real message actually state a death toll?
#
# classifier.py's killed_reported comment ASSERTED that "the message states no
# count" is the normal answer "(~86% of messages)", and classifier.py:185 ships
# that prior into the LIVE PROMPT: "Most messages carry no number. null is the
# normal answer." Nobody had measured it — a repo-wide grep found the claim only
# in the comment asserting it. An unmeasured prior in the prompt biases the
# classifier toward the answer it assumed, which is state 3 ("I looked and
# couldn't tell") wearing the clothes of state 1. This script was the missing
# measurement; that comment now carries this script's result instead, so do not
# read the ~86% back out of it. Line numbers are deliberately not cited here —
# this file and that comment cite each other, and a line number is the half of
# a citation that rots.
#
# It needs no LLM and no API key: killed_reported is COPIED out of the message,
# never estimated, so "does this message state a toll" is a property of the
# string. That also means the answer is a BOUND, not a point estimate — see
# WHAT THIS IS AND IS NOT at the end of the output.
#
# Runs on the VPS, read-only, like the other archive_* scripts:
#   scp tools/archive_killed_rate.py truthevades:/tmp/
#   ssh truthevades 'python3 /tmp/archive_killed_rate.py'
import collections, gzip, io, random, re

ARCHIVE = "/root/archive/conflict_monitor-20260818.sql.gz"
SAMPLE_FILE = "/tmp/killed_rate_samples.txt"
SAMPLE_K = 400          # kept per bucket in the sample file
SAMPLE_PRINT = 12       # printed to stdout per bucket
random.seed(20260818)   # deterministic: the next run audits the same strings

# ── what counts as a stated toll ──────────────────────────────────────────────
# A number the classifier could COPY. Deliberately NOT "dozens", "scores",
# "hundreds", "many", "several": those state no count, so killed_reported would
# still be NULL. Spelled-out cardinals are cheap and common in headlines.
_NUM = r"(?:\d{1,3}(?:,\d{3})+|\d{1,6})"
_SPELLED = (r"one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
            r"thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|"
            r"twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety")
_COUNT = r"(?:" + _NUM + r"|" + _SPELLED + r")"
_APPROX = (r"(?:at least|more than|over|nearly|around|about|some|up to|"
           r"no fewer than|a total of)\s+")

# Killed only. Wounded / injured / hospitalised / missing are excluded on
# purpose, mirroring the prompt rule at classifier.py:182 — a message reporting
# "40 wounded" states no death toll, and a regex that counted it would be
# inventing exactly the kind of number this whole field exists to avoid.
# "died" earned its place from the audit sample: the first run missed
# "12 died in the strike" entirely.
_KILL = r"(?:killed|dead|deaths|died|martyred|martyrs|fatalities|slain)"
_VICTIM = (r"(?:people|persons?|civilians?|children|kids|women|men|palestinians?|"
           r"israelis?|iranians?|lebanese|syrians?|yemenis?|iraqis?|americans?|"
           r"soldiers?|troops?|officers?|police(?:men)?|militants?|fighters?|"
           r"members?|medics?|journalists?|workers?|students?|hostages?|victims?|"
           r"bodies|others?|more|additional)")
_FILLER = (r"(?:\s+(?:" + _VICTIM + r"|were|was|are|is|have|has|had|been|"
           r"reportedly|reported|said|confirmed|feared|also|now)){0,3}")
_MONTHS = (r"january|february|march|april|may|june|july|august|september|"
           r"october|november|december")

# Each pattern captures the number so the extracted values can be tabulated —
# a spike at "2023" or "7" is how a human sees the regex eating dates.
PATTERNS = [
    # "17 killed", "at least 60 dead", "12 Palestinians were killed".
    # The lookbehind is not cosmetic: without it "No one was killed" matched
    # here and was counted as a stated toll of one — a message saying nobody
    # died, filed as a death. It belongs in the stated-ZERO bucket, and the
    # sample audit is how it was caught.
    ("count_then_kill",
     re.compile(r"(?<!\bno\s)\b(" + _COUNT + r")\b" + _FILLER + r"\s+" + _KILL + r"\b", re.I)),
    # "killed 17", "killing at least 60". The lookahead blocks "killed 3 days
    # ago" and "killed 7 October" — a duration or a date is not a toll.
    # "kills"/"kill" are here because the audit sample caught the first run
    # missing "Hezbollah fire kills one in Israel".
    ("kill_then_count",
     re.compile(r"\b(?:killed|killing|kills|kill|martyred|murdered|executed|"
                r"claim(?:ed|ing)? the lives of)\s+"
                r"(?:" + _APPROX + r")?(" + _COUNT + r")\b"
                r"(?!\s*(?:%|percent|years?|yrs?|months?|weeks?|days?|hours?|"
                r"minutes?|" + _MONTHS + r"))", re.I)),
    # "death toll rises to 40", "death toll: 40", "number of dead reached 12"
    ("death_toll_phrase",
     re.compile(r"\b(?:death toll|toll of (?:the )?dead|"
                r"number of (?:the )?(?:dead|deaths|killed|martyrs)|fatalities)\b"
                r"[^.\n]{0,40}?\b(" + _COUNT + r")\b", re.I)),
]

# "One person killed, named or described -> 1" (classifier.py:181). Counted in
# its own bucket, not folded into the numeric rate: whether a reader agrees
# that "a paramedic was killed" is a stated count of 1 is a judgement call, and
# a judgement call must not be buried inside a headline number.
P_SINGULAR = re.compile(
    r"\b(?:a|an|one)\s+(?:[\w-]+\s+){0,2}?"
    r"(?:man|woman|child|boy|girl|person|soldier|officer|civilian|policeman|"
    r"teenager|worker|journalist|medic|paramedic|driver|guard|farmer|student|"
    r"doctor|nurse)\s+"
    r"(?:was\s+|has\s+been\s+|were\s+|had\s+been\s+)?"
    r"(?:killed|shot\s+dead|martyred)\b", re.I)

# The other half of the field's contract: 0 means the message said nobody was
# killed. It is a measurement, not an absence, and it is not NULL.
_ZERO_PHRASE = (r"no\s+(?:one|body|\w+\s+)?(?:was\s+|were\s+|been\s+)?"
                r"(?:killed|martyred)\b"
                r"|nobody\s+(?:was\s+)?killed"
                r"|no\s+(?:deaths|fatalities|casualties)\b"
                r"|without\s+(?:any\s+)?(?:deaths|fatalities|casualties)"
                r"|zero\s+(?:deaths|fatalities|casualties)")
# ...and the phrasing alone does not establish it. "No deaths have been
# ANNOUNCED", "no casualties REPORTED", "no fatalities CONFIRMED yet" say that
# nobody has PUBLISHED a toll — state 2 or state 3 — and filing them as a
# measured 0 is this project's cardinal bug committed inside the tool written
# to police it. A reporting verb within three words of the phrase disqualifies
# it. "reported" also came OUT of the phrase itself: "no one reported killed"
# is the same absence with the verb on the inside. The disqualified rows are
# counted in their own bucket, not silently folded into the zero bucket's
# neighbours, because the size of a correction to this field is exactly the
# thing that must stay visible.
_UNPUBLISHED = (r"(?:\w+\s+){0,3}?"
                r"(?:announced|reported|confirmed|released|published|yet)\b")
P_ZERO = re.compile(r"\b(?:" + _ZERO_PHRASE + r")(?!\s+" + _UNPUBLISHED + r")", re.I)
P_ZERO_UNPUBLISHED = re.compile(
    r"\b(?:" + _ZERO_PHRASE + r")\s+" + _UNPUBLISHED
    + r"|\bno\s+(?:one|body)\s+(?:was\s+|were\s+|has\s+been\s+)?reported\s+"
      r"(?:killed|dead|martyred)\b", re.I)

# Messages that talk about casualties WITHOUT stating a death toll. Reported as
# a sanity check on the exclusion above: these are the rows a sloppier regex
# would have turned into invented death tolls. Tested at EVERY exit that ends in
# "states no count", not only at the last one: incremented where it used to sit,
# it could only be reached after the has_en gate, so "Over 200 Israelis injured"
# — a row with no kill word at all, and the purest example of what this bucket
# is for — was dropped before the counter and the printed line did not measure
# its own label. English-only, so Arabic-script rows are not represented in it.
P_WOUNDED = re.compile(
    r"\b(?:wounded|injur\w+|hospitali[sz]\w+|casualt\w+|missing)\b", re.I)

# Cheap substring gate before the expensive patterns. The claim a gate like this
# makes -- "every ENGLISH pattern above requires one of these, so skipping on
# all-absent changes no count" -- has to be checked against the alternations
# rather than asserted: kill_then_count also fires on "murdered", "executed" and
# "claimed the lives of", none of which contain any of kill/dead/death/died/
# martyr/fatalit/slain/toll/casualt, so the gate was silently dropping rows the
# pattern would have matched. The direction was safe (a dropped row inflates the
# null rate this script exists to bound) but the premise of the whole file is
# that an unverified assertion in a comment is a defect. Re-checked verb by verb
# after adding the last three; the claim now holds.
PREFILTER = ("kill", "dead", "death", "died", "martyr", "fatalit", "slain",
             "toll", "casualt", "murder", "execut", "lives of")

# 16.6% of the archive (13,946 rows) is Arabic script, and every pattern above
# is blind to it — an English-only regex would silently file all of that as
# "states no count", which is the exact error this script exists to expose,
# committed by the script itself. Kept as its own bucket rather than folded in:
# it is a coarser pattern (a number NEAR a kill word, not a parsed phrase) and
# a coarser measurement must not hide inside a finer one.
_AR_SCRIPT = re.compile(r"[؀-ۿ]")
_AR_KILL = (r"(?:قتل|قتلى|قتيل|"
            r"شهيد|شهداء|"
            r"استشه|مقتل|"
            r"وفاة|وفيات)")
_AR_NUM = r"(?:[0-9٠-٩][0-9٠-٩,]{0,6})"
# Either order: Arabic puts the count on both sides ("12 قتيلا", "استشهاد 12").
P_AR_TOLL = re.compile(_AR_KILL + r"[^.\n]{0,30}?" + _AR_NUM
                       + r"|" + _AR_NUM + r"[^.\n]{0,30}?" + _AR_KILL)


def unescape(v):
    """Undo pg_dump's COPY text escaping (\\n, \\t, \\r, \\\\).

    Not cosmetic: a message stored as "killed\\n17" is the two characters
    backslash-n in the dump, which glues the words together and hides a real
    toll from every pattern above. Octal escapes are NOT decoded, and the
    earlier claim here that they were "left alone" was wrong in the direction
    that matters: the fallback below drops the backslash and keeps the digits,
    so "\\101" arrives at the patterns as the literal text "101" — a number they
    will scan. pg_dump emits \\nnn only for control bytes, so the residue is
    rare, but how often is not measured here; the point is that the code, not
    this docstring, decides what the regexes see.
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
    """Fixed-size uniform sample, deterministic under the seed at the top."""

    def __init__(self, k):
        self.k, self.items, self.n = k, [], 0

    def add(self, item):
        self.n += 1
        if len(self.items) < self.k:
            self.items.append(item)
        else:
            j = random.randrange(self.n)
            if j < self.k:
                self.items[j] = item


def window(text, m, width=90):
    """The matched string plus context, one line, for a human to eyeball."""
    lo = max(0, m.start() - width // 2)
    hi = min(len(text), m.end() + width // 2)
    return ("..." if lo else "") + " ".join(text[lo:hi].split()) + ("..." if hi < len(text) else "")


rows = short = null_text = 0
numeric = arabic_numeric = singular_only = zero_only = wounded_no_toll = 0
arabic_rows = zero_unpublished = 0
per_pattern = collections.Counter()
values = collections.Counter()
by_source = collections.defaultdict(lambda: [0, 0])    # source  -> [rows, numeric]
by_channel = collections.defaultdict(lambda: [0, 0])   # channel -> [rows, numeric]
s_matched = Reservoir(SAMPLE_K)
s_arabic = Reservoir(SAMPLE_K)
s_singular = Reservoir(SAMPLE_K)
s_zero = Reservoir(SAMPLE_K)
s_zero_unpub = Reservoir(SAMPLE_K)      # "no deaths REPORTED" -- an absence, not a 0
s_miss_killword = Reservoir(SAMPLE_K)   # says "killed" but no pattern fired
s_miss_plain = Reservoir(SAMPLE_K)      # no kill word at all

# Column positions are read from the COPY header rather than hardcoded the way
# the other archive_* scripts hardcode f[1]/f[6]/f[15]. Here the whole
# measurement IS the text column: guessing it and silently counting `summary`
# instead of `raw_text` would produce a confident number about the wrong data,
# which is the failure this script exists to correct.
idx = None
cols = []
with gzip.open(ARCHIVE, "rt", encoding="utf-8", errors="replace") as fh:
    for line in fh:
        if idx is None:
            if line.startswith("COPY public.events "):
                cols = [c.strip().strip('"') for c in
                        line[line.index("(") + 1:line.rindex(")")].split(",")]
                idx = {c: i for i, c in enumerate(cols)}
                missing = [c for c in ("raw_text", "source", "channel_name") if c not in idx]
                if missing:
                    raise SystemExit("columns not in dump: %s -- header was: %s"
                                     % (", ".join(missing), line.strip()[:200]))
            continue
        if line.startswith("\\."):
            break
        f = line.rstrip("\n").split("\t")
        if len(f) < len(cols):
            short += 1
            continue
        rows += 1
        source, channel = f[idx["source"]], f[idx["channel_name"]]
        by_source[source][0] += 1
        by_channel[channel][0] += 1
        raw = f[idx["raw_text"]]
        if raw == "\\N" or not raw:
            null_text += 1
            continue
        text = unescape(raw)
        low = text.lower()
        has_en = any(tok in low for tok in PREFILTER)
        has_ar = _AR_SCRIPT.search(text) is not None
        if has_ar:
            arabic_rows += 1
        if not has_en and not has_ar:
            if P_WOUNDED.search(text):
                wounded_no_toll += 1
            s_miss_plain.add((source, " ".join(text.split())[:160]))
            continue

        hit = None
        if has_en:
            for name, pat in PATTERNS:
                m = pat.search(text)
                if m:
                    per_pattern[name] += 1
                    if hit is None:
                        hit = (name, m)
        if hit:
            numeric += 1
            by_source[source][1] += 1
            by_channel[channel][1] += 1
            values[hit[1].group(1).lower()] += 1
            s_matched.add((source, hit[0], window(text, hit[1])))
            continue

        if has_ar:
            m = P_AR_TOLL.search(text)
            if m:
                arabic_numeric += 1
                per_pattern["arabic_num_near_kill"] += 1
                by_source[source][1] += 1
                by_channel[channel][1] += 1
                s_arabic.add((source, window(text, m)))
                continue
        if not has_en:
            if P_WOUNDED.search(text):
                wounded_no_toll += 1
            s_miss_plain.add((source, " ".join(text.split())[:160]))
            continue

        m = P_SINGULAR.search(text)
        if m:
            singular_only += 1
            s_singular.add((source, window(text, m)))
            continue
        m = P_ZERO.search(text)
        if m:
            zero_only += 1
            s_zero.add((source, window(text, m)))
            continue
        m = P_ZERO_UNPUBLISHED.search(text)
        if m:
            # No `continue`: the row says nobody has published a toll yet, so it
            # belongs in the states-no-count total below, which is what NULL
            # means. Counted here only so the correction is visible.
            zero_unpublished += 1
            s_zero_unpub.add((source, window(text, m)))
        if P_WOUNDED.search(text):
            wounded_no_toll += 1
        if "kill" in low or "dead" in low or "died" in low or "martyr" in low:
            s_miss_killword.add((source, " ".join(text.split())[:160]))
        else:
            s_miss_plain.add((source, " ".join(text.split())[:160]))

scanned = rows - null_text
no_count = scanned - numeric - arabic_numeric - singular_only - zero_only


def pct(a, b):
    return 100.0 * a / b if b else 0.0


print("archive:", ARCHIVE)
print("event rows: %d   (short/unsplittable lines skipped: %d)" % (rows, short))
print("raw_text NULL or empty: %d   -> rows measured: %d" % (null_text, scanned))
print()
print("STATES A DEATH TOLL (a number killed_reported could be copied from):")
print("  numeric count, English      %7d  (%5.1f%%)" % (numeric, pct(numeric, scanned)))
print("  numeric count, Arabic       %7d  (%5.1f%%)   number near an Arabic kill word"
      % (arabic_numeric, pct(arabic_numeric, scanned)))
print("       ^ proximity only: this bucket does not check that the digit is the")
print("         DEATH count, and in a sentence reporting both it is often the")
print("         wounded count sitting next to the kill word. Read the sample.")
print("  one person, no numeral      %7d  (%5.1f%%)   e.g. 'a paramedic was killed' -> 1"
      % (singular_only, pct(singular_only, scanned)))
print("  stated ZERO killed          %7d  (%5.1f%%)   -> killed_reported = 0, not NULL"
      % (zero_only, pct(zero_only, scanned)))
print("       ^ the message says nobody died. Rows that instead say no toll has")
print("         been ANNOUNCED/REPORTED/CONFIRMED yet are excluded from it:")
print("         %7d  such rows, counted in STATES NO COUNT below, because"
      % zero_unpublished)
print("         'nothing published yet' is an absence and 0 is a measurement.")
print()
print("STATES NO COUNT  -> killed_reported = NULL:")
print("  %7d  (%5.1f%%)   of %d measured rows" % (no_count, pct(no_count, scanned), scanned))
print("  (%5.1f%% if the singular-person bucket is read as 'no count' instead)"
      % pct(no_count + singular_only, scanned))
print()
print("mentions wounded/injured/missing but states no death toll: %d (%.1f%%)"
      % (wounded_no_toll, pct(wounded_no_toll, scanned)))
print("  (English wording only, and tested at every states-no-count exit --")
print("   including rows with no kill word at all, e.g. '200 Israelis injured')")
print("rows containing Arabic script: %d (%.1f%%) -- covered only by the coarse"
      % (arabic_rows, pct(arabic_rows, scanned)))
print("  Arabic bucket above, so misses are concentrated here")
print()

print("rate by `source` column:")
for k, (tot, hits) in sorted(by_source.items(), key=lambda kv: -kv[1][0]):
    print("  %-20s %7d rows   states a numeric toll: %6d (%5.1f%%)"
          % (k, tot, hits, pct(hits, tot)))
print()
print("rate by `channel_name` (top 20 by volume) -- per-source spread is what")
print("proved the classifier was working before (docs/FINDINGS.md:110):")
for k, (tot, hits) in sorted(by_channel.items(), key=lambda kv: -kv[1][0])[:20]:
    print("  %-24s %7d rows   numeric toll: %6d (%5.1f%%)"
          % (k[:24] or "(empty)", tot, hits, pct(hits, tot)))
print()

print("which pattern fired (a row can fire more than one):")
for name, c in per_pattern.most_common():
    print("  %-20s %7d" % (name, c))
print()
print("most common extracted numbers -- a date or a year here means the regex is")
print("manufacturing tolls and the bound below does NOT hold:")
print("  " + "  ".join("%sx%d" % (v, c) for v, c in values.most_common(25)))
print()

buckets = [
    ("MATCHED -- numeric toll", s_matched),
    ("MATCHED -- numeric toll, Arabic", s_arabic),
    ("MATCHED -- one person, no numeral", s_singular),
    ("MATCHED -- stated zero", s_zero),
    ("REJECTED zero -- no toll published yet (counted as no count)", s_zero_unpub),
    ("UNMATCHED but contains a kill word (where misses live)", s_miss_killword),
    ("UNMATCHED, no kill word at all", s_miss_plain),
]
for title, res in buckets:
    print("%s  (%d rows, showing %d):" % (title, res.n, min(SAMPLE_PRINT, len(res.items))))
    for item in res.items[:SAMPLE_PRINT]:
        print("   [%s] %s" % (item[0], " | ".join(str(x) for x in item[1:])))
    print()

with io.open(SAMPLE_FILE, "w", encoding="utf-8") as out:
    for title, res in buckets:
        out.write("== %s == (%d rows, %d sampled)\n" % (title, res.n, len(res.items)))
        for item in res.items:
            out.write("[%s] %s\n" % (item[0], " | ".join(str(x) for x in item[1:])))
        out.write("\n")
print("full sample (%d per bucket) written to %s" % (SAMPLE_K, SAMPLE_FILE))
print()

print("WHAT THIS IS AND IS NOT")
print("  * A LOWER bound on messages that state a toll, so an UPPER bound on the")
print("    NULL rate: the patterns above catch the phrasings written into them and")
print("    miss every other way a toll can be worded. The true 'states no count'")
print("    share of these rows is AT MOST the percentage printed above.")
print("  * That bound holds only while false positives are rare. The extracted-number")
print("    table and the sampled strings are there to be read -- a regex nobody")
print("    eyeballed is itself an unmeasured assumption.")
print("  * The misses are not spread evenly. The English patterns parse a phrase;")
print("    the Arabic one only asks whether a number sits near a kill word, and no")
print("    other language is covered at all. Read the Arabic sample before trusting")
print("    that row, and treat the Arabic-script share printed above as where the")
print("    remaining error lives.")
print("  * One caveat runs the OTHER way, and it is the only one that does. The")
print("    patterns cannot tell a toll for THIS event from a CUMULATIVE war total")
print("    quoted as background -- a wire story ending 'more than 73,000 have been")
print("    killed in Gaza since October 2023' counts here as 'states a death toll'")
print("    while the classifier must NOT copy that number into killed_reported for")
print("    the incident being reported. Counting them pushes the printed NULL rate")
print("    DOWN -- the one error here that the 'AT MOST' bound above does NOT")
print("    cover. The big round values in the extracted-number table are where")
print("    they surface; read that table before quoting the bound.")
print("  * Different population from the prompt's claim. The archive holds STORED")
print("    EVENTS; messages the classifier tagged [NOISE] and low-severity articles")
print("    were dropped before insert (telegram.py:269, news_feeds.py:341) and are")
print("    not rows here. Those carry tolls less often than real incident reports,")
print("    so the null rate across all messages SEEN is likely higher than this.")
print("    This bias runs opposite to the regex-miss bias; neither is quantified.")
print("  * Deduplication merges repeats into one row (report_count > 1, 8.7% of the")
print("    archive), so this is a rate per stored event, not per message received.")
print("  * It says nothing about whether the classifier copies a toll CORRECTLY when")
print("    one is present -- only how often there is one to copy.")
