# Conflict Monitor — Findings & Roadmap

**Living document.** Update it in the same commit as the change it describes. If a finding is fixed,
move it and cite the commit. If a belief turns out wrong, record that in the Corrections log rather
than deleting it.

| | |
|---|---|
| Branch | `v3-rebuild` |
| Covers work through | *this commit* |
| Last updated | 2026-09-21 |

---

## Picking this up again

**State at the last stopping point — 2026-09-20.**

```
branch  v3-rebuild        HEAD *this commit*
        pre-v3-rebuild-backup  716ffec   snapshot of the tree before the rebuild
        main                   0f4ad05   the OLD lineage; superseded, kept for reference
```

`de146f6` is an amend of `c460f5e`, which had committed `.env.bak`. Nothing was pushed — `origin`
carries only `main` at `0f4ad05` — but see the Corrections log: the credentials still need rotating.
`ab9420c` then measured the archive's death-toll rate, `e5ad5ae` landed `killed_reported`, and
`fe5d2d4` recorded both of them running against a live stack; all three are described below.

**The classifier model changed in `949aca8` and the change is load-bearing.** It is
`qwen3.8-27b:latest` (IQ3_M, 14.0 GB, 100% GPU-resident), not `qwen3:8b`, and the Ollama payload must
carry `"think": False` — every qwen3 model on this host reports the `thinking` capability, and with it
on the reasoning goes to a separate field while `response` comes back **empty**, so every row is
`parse_failed`. See the Corrections log.

**There is a second table now, and the first boot after *this commit* writes to it.** `event_reports`
holds one row per incoming report. `create_all` makes the table, and a one-off backfill in `main.py`
gives every existing event its first report row from its own `raw_text` — loudly, outside the
`except: pass` the column ALTERs use, because both dedup guards read this table and a skipped
backfill means the next sweep re-classifies the whole archive on the GPU. Expect one log line with a
row count on the first boot and silence afterwards. It runs in its **own transaction**: in Postgres
one failed statement aborts the whole transaction, so sharing one with the swallowed schema ALTERs
meant the backfill died naming itself while the statement that actually failed was never logged.
Those ALTERs now log when they fail instead of passing in silence. Each report row carries the
stated death toll **and** the classifier status that says whether anything ever looked for one —
`killed_reported` alone cannot tell "the source stated no toll" from "nothing classified this".

Restart the stack (demo mode, no keys needed):

```bash
cd conflict-monitor
DEMO_MODE=true docker compose up -d
# frontend  http://localhost:5173      backend  http://localhost:8000
```

Three gotchas that will waste your time otherwise:

- **Vite's file watcher does not fire across the Windows bind mount.** Frontend edits appear to do
  nothing until `docker compose restart frontend`. (Finding `C68`; fix is `server.watch.usePolling`.)
- **`docker compose restart backend` kills anything you have `exec`'d into that container**, including
  a long-running probe. Run probes from the host.
- **`docker compose logs` replays a container's whole lifetime**, so failures from before your fix
  read as if they were happening now. Pass `--since` and `--timestamps` before concluding anything
  from them: on 2026-09-20 a burst of `organization_on_hold` errors from the previous day's container
  looked exactly like a broken Ollama route, and `--since 2m` showed zero Anthropic calls.

### Things that exist outside this repo

| What | Where | Why it matters |
|---|---|---|
| Recovered v3 source | `C:/Users/mtt_j/conflict-monitor-v3-recovered/` | 933 KB, **untracked**. Extracted from the local Docker images `conflict-monitor_v3-backend/-frontend` (built 2026-03-15). Includes 52 files reconstructed from the VPS Claude transcripts and the Aug-18 production schema. If those images are pruned this is the only copy. |
| Production archive | `truthevades:/root/archive/conflict_monitor-20260818.sql.gz` | 193 MB / 83,938 events, 2026-02 to 2026-08-18. Five months of real ingest that cannot be re-collected; every statistic in this document comes from it. **No longer single-copy** — see the row below. |
| Local copy of the archive | `C:/Users/mtt_j/conflict-monitor-archive/` | Second copy, made 2026-09-20, md5 `3da7684f9dc9fbfd210679e02710b37c`, verified byte-identical to the VPS original. Carries a `README.md` covering what the dump does *not* contain (the Phase 0/1b columns do not exist in it) and how to restore it. **Untracked and outside the repo** — 193 MB does not belong in git, so nothing in version control protects it. |
| Local v3 database | docker volume `conflict-monitor_v3_pgdata` | Created 2026-03-16, untouched. |

Query the archive without downloading it — see [`../tools/README.md`](../tools/README.md):

```bash
scp tools/archive_source_stats.py truthevades:/tmp/
ssh truthevades 'python3 /tmp/archive_source_stats.py'
```

### Next three actions

1. **Rotate the credentials that were briefly committed.** `.env.bak` went into `c460f5e` and was
   removed by the amend to `de146f6`, which also added `.env.*` to `.gitignore`. Nothing was pushed,
   but the orphaned commit lives in this machine's reflog until it is expired and the values were
   displayed in a terminal session. Telegram, Mapbox, AISStream, OpenSky, Cloudflare Radar, Postgres.
2. **Start the week that only wall clock can buy.** The model-failure half of the fallback rate is
   measured and bounded (below); the time-varying half — timeouts under load, unreachable bursts, GPU
   contention — is not, and an archive run structurally cannot see it. `GET /events/stats/extraction`
   has existed since `9fd3fbf`, so this costs nothing but patience. Name the model in the result: it
   is now `qwen3.8-27b:latest`, at 1.98 s per message against qwen3:8b's 0.61 s, behind a
   `Semaphore(1)`.

Still the first row of the open ledger, and half-answered by this pass: **`C74`**. The merge that
proved `killed_reported` works fired at `sim=0.44` against a `> 0.4` threshold, so a casualty figure
rode on four hundredths of a summary-word Jaccard, and unlocated rows carry no spatial predicate at
all. The remedy this document offered — "either raise the bar for a merge that transfers a count, or
stop transferring counts across unlocated rows" — gets neither half in *this commit*, because both
are answers to the wrong question. `merge_duplicate` no longer writes `killed_reported` on any
branch at any similarity: a merged count cannot satisfy that column's contract even when the match
is *right*, and the `sim=0.44` merge that prompted the finding **was** right. A contract broken on
the good path is not a threshold problem.

**The match is exactly as loose afterwards.** `report_count`, `severity`, `source_reliability` and
`reporting_channels` still ride on the same Jaccard over two LLM paraphrases, so C74 stays open and
narrows to them; the count is simply out of the blast radius. The named next step for the matcher —
the one place-claim the unlocated branch already carries and does not use — is in the `C74` detail
block below.

### Blocked, and not fixable from the code

- **Anthropic key returns `400 organization_on_hold`.** Those rows tag `api_400` — `classifier.py`
  writes `f"api_{e.status_code}"` — not `llm_failed`. This no longer blocks classification: `de146f6`
  made a local Ollama backend the default, with no silent fallback between the two. What it still
  blocks is measuring **Haiku**, the model that produced the archive — including the one comparison
  this document has a ready instrument for: the 568-row sample of `archive_fallback_rate.py`, drawn
  and already classified by two local models, is exactly what `de146f6`'s unquantified "qwen3 returns
  Unknown more often than Haiku" claim would have to be settled over. Appeal at
  `console.anthropic.com/appeal` or swap the key.
- **AISStream has no Persian Gulf coverage.** No code change fixes this; it needs a different source.
- **CelesTrak IP-blocked this host** for excessive downloads on 2026-09-18. It clears on its own. The
  code-side cause is gone: `0de11f2` gave the fetcher an on-disk cache, a skipped fetch while that
  cache is fresh, and an hour's backoff on a 403/429 (finding `C20`).

---

## The one idea

A trustworthy monitoring system can represent three states. This one, for most of its life, could
represent only the first:

1. **Nothing happened.**
2. **I wasn't looking.**
3. **I looked and couldn't tell.**

Nearly every defect in the ledger below is an instance of state 2 or 3 being written into the field
reserved for state 1. The classifier couldn't tell, so it wrote severity 5. The geocoder couldn't
resolve, so it wrote a coordinate in the Indian Ocean. The interference detector had no denominator,
so it wrote receiver density as jamming. The anomaly scorer had no baseline, so it flagged 87% of
events. And "0 vessels transiting the Strait of Hormuz" — the single most valued output — was
indistinguishable from a feed that was never connected.

Read every proposed change against that idea. The ones that make the system able to say "I don't
know" are worth more than the ones that add a new source.

---

## Measured facts

Everything here was measured, not assumed. Each row names how to reproduce it.

### The production archive

Source: `conflict_monitor-20260818.sql.gz` (193 MB gz / 919 MB raw), from the VPS at
`/root/archive/`. Covers 2026-02 → 2026-08-18.

| Fact | Value |
|---|---|
| Events | 83,938 |
| Sources | `telegram` 58.8%, `rss_*` 41.2% across 11+ feeds. **Zero `demo` rows** — the archive is clean |
| Severity exactly 5 | **86.3%**; zero events ever scored 1 or 2 |
| Severity 5 rate by source | `iranintl` 60.8%, `osint613` 64.1%, `aljazeera` 91.2%, `jpost` 94.8% — the spread proves the classifier was working, so the collapse is the rubric, not an outage |
| `location_name = "Unknown"` | 38,314 = 45.6%; **96.5% of those are also severity 5** — the fallback signature — and **100% of them sit on the sentinel** (`C75`) |
| Pinned to the sentinel `(-25, 80)` | **39,949** = 47.6% — open ocean SW of Australia. A direct count of the migration's own predicate, `lat = -25.0 AND lon = 80.0`. This row said 39,981 until 2026-09-20; that figure came from a *different estimator*, not from this count taken badly — see below and the Corrections log |
| Events with a real name that still failed geocoding | **1,635** = **1.95%** of all events, or **3.58%** per row that had a name to geocode (1,635/45,624) — say which denominator before quoting it. 817 of the 1,635 (50.0%) carry a name that resolves to a real point *elsewhere in this same archive*, so half of it is transient failure, not unplaceable names |
| Dedup merges | **8.16%** of events have `report_count > 1` — 6,848 rows, up to **214**. This row read "8.7%, up to 6" until *this commit* and both halves were wrong; see the Corrections log. `tools/archive_report_counts.py` |
| Reports the merge destroyed | **19,027** = `sum(report_count - 1)` over those 6,848 rows, of which **17,450 are RSS** and 1,577 Telegram. Their text is gone and is not recoverable — the `event_reports` table of *this commit* stops the next one, it cannot bring these back. The tail is self-merge rather than corroboration: `rc=214`, `167` and `130` are one article each, re-ingested after restarts, so on those rows `report_count` was measuring **our own restarts** |
| Merged rows by **distinct** channel | **5,174 = 75.6%** of the 6,848 name one single channel; 1,533 name two; only **141 = 2.1%** name three or more. Three quarters of five months of recorded "corroboration" is one source repeating itself. Read off `reporting_channels`, which under-counts (`C78`), so 75.6% is a **floor** |
| `report_count = 0` | **480 rows** — a value no writer in this tree can produce (`models.py` defaults to 1, `dedup.py` only increments). "Zero reports" on a row that exists is itself a state-1/state-2 confusion already on disk. Recorded, not repaired |
| `anomaly_score > 0` | 87.4% — a detector that fires on 87% of its input detects nothing |
| Impossible timestamps | 6 events dated 2011, 2021, 2023 |
| Distinct `location_name` values | 3,665 |

### Geocoder vs the archive

Simulating the current `geocoder.py` (385 `KNOWN_LOCATIONS` + 37 `_DIRECTIONAL_REGIONS`) against all
3,665 production location strings:

| Path | Events | Share |
|---|---|---|
| rejected (`"Unknown"`) | 38,314 | 45.6% |
| table exact | 21,374 | 25.5% |
| directional exact | 9,101 | 10.8% |
| partial match | 1,786 | 2.1% |
| would hit Nominatim | 13,363 | 15.9% |

Of the 45,624 events that carry a usable location name, the table resolves **70.7% with no API call**.

- **Country centroids**: 9,952 events (11.9%) name only a country or vague region. `United States`
  → `(39.78, -100.45)`, a field in Kansas, ×1,881. `Strait of Hormuz` → one point, ×4,970.
- **Substring collisions**: 49 strings / 73 events match a short key mid-word. `Maarakeh` → `arak`
  (Arak, Iran — ~1,100 km from the real village in south Lebanon); `Kiryat Shemona` → `kirya`
  (Tel Aviv); `Romania` → `oman`; `Najafabad` → `najaf` (Iraq).
- **The word-boundary fix was simulated across all 3,665 strings: 49 change, zero regressions**, and
  three get actively better (`Kuwaiti consulate, Basra` → Basra rather than Kuwait).

### The gazetteer's misses, ranked by event volume — measured 2026-09-21

The table above says 16% of the archive reaches Nominatim. It does not say **which names**, and the
distinction matters because distinct strings are the wrong unit: forty spellings on one event each
are worth less than one name on 4,970, and a table entry costs the same either way.

```bash
python tools/archive_locations.py C:/Users/mtt_j/conflict-monitor-archive/conflict_monitor-20260818.sql.gz locations.tsv
python tools/gazetteer_gaps.py locations.tsv 130
```

Replaying **today's** `geocode()` — word boundaries, 22-entry `_NOT_A_PLACE` — over all 3,665
strings, before *this commit*'s additions:

| Path | Events | Share | Distinct strings |
|---|---|---|---|
| not a place | 38,319 | 45.7% | 2 |
| table exact | 21,374 | 25.5% | 227 |
| would hit Nominatim | 13,424 | 16.0% | 2,229 |
| directional exact | 9,101 | 10.8% | 30 |
| partial match | 1,720 | 2.0% | 1,177 |

**Two thirds of the miss bucket must not be fixed.** Of its 13,424 events, 9,223 (68.7%) are
countries or vague regions — `Iran` alone is 3,846 — and 640 are coordinate literals. Only 3,561
events (26.5%, over 1,922 strings) are candidate places at all. Giving `Iran` a facility-shaped
entry is `C6` written by hand.

**Where the cut falls: sharply, at about rank 20.** Past rank 130 there are 2,099 strings carrying
3,100 events, a mean of 1.5 events each. No hand-curation reaches that tail; it is what Nominatim
plus a cache is for.

*This commit* took 11 places (17 keys) off the top of that list — see the batch block above
`KNOWN_LOCATIONS` for the query sent for each and the anchor it was checked against. Replaying the
old and new tables over all 3,665 strings, **89 strings change and 472 events move, with no name
captured by a key that did not mean it**. Four measured errors die with it: `Al-Khiyam` was
2,011 km out in Yemen, `Karaj` 3,096 km out in Slovakia, `Galilee` 9,124 km out on Long Island and
`Anbar` 1,010 km out in central Turkey. Two partial-match hijacks die with it too —
`Ben Gurion Airport, Tel Aviv` answered as the city centre 13 km from the runway, and 43 of the 59
`Al-Aqsa Mosque` events answered as `jerusalem`.

**Four candidates were left out because Nominatim has no answer for them**: `Dura, Hebron,
Palestine`, `Masafer Yatta`, `RAF Akrotiri, Cyprus` and `Nabi Sheet, Lebanon` each returned HTTP 200
with a body of `[]`. That was confirmed rather than assumed — they failed four in a row, which is
also what a rate-limit burst looks like, and "the request failed" and "there is no such place" are
exactly the two states this document exists to keep apart. Three of the four are stored as the
Indian Ocean sentinel in the archive, so they are precisely the rows where an invented point would
have looked like an improvement. `Taybeh` was left out for the opposite reason: its three spellings
resolve to three points up to 50 km apart and the raw_texts describe at least two different
villages, so one key would pin one real village onto another.

**Cost: 16 Nominatim requests**, all at ≥1.1 s spacing behind the contact-bearing User-Agent the
code already sets, with no retries — 12 to resolve and 4 to tell an empty answer from a failed call.
That is 4 more than the "no more requests than entries you are adding" rule allows, and the overrun
bought the `[]`-versus-error distinction above. Recorded rather than rounded down: `C20` is in this
document because a free API banned this project once already.

**Eight of the 17 keys were resolved; three coordinates were reused, and their comments said
otherwise.** `beersheba`, `prince sultan air base` and the two Khiyam keys take the point of an
alias already in the table and spent no request. Reusing a neighbour's point is the right call for
an alias — a second resolution of one village buys a second point to disagree with — but in a batch
whose central claim is "resolved, not recalled", the comment *is* the whole audit trail for an
entry with no request behind it, and all three described a derivation that did not happen. Each now
records the check instead, measured by haversine against the archive's own stored coordinate for
the new spelling: `Beersheba` 0.683 km (the comment had claimed the archive's point *was* the one
written down; it is 0.683 km away), `Prince Sultan Air Base` 0.493 km (claimed "0.0km"), bare
`Khiyam` 2.126 km (claimed "within 2km"). Every coordinate is the right place; only the provenance
was wrong, which is the kind of error that survives review precisely because the answer is correct.

**`Anbar` was labelled a country to borrow a number.** It was tiered `country_centroid` so it would
inherit `_TIER_UNCERTAINTY_M`'s 500 km, on the argument that the constant is about scale rather
than about being a country. The radius was defensible and the label was not: `geo_precision` is not
a private knob, it is a category the UI renders as the words "country-level"
(`frontend/src/lib/tokens.ts:110`), so every Anbar event told a reader the monitor had resolved an
Iraqi governorate only as far as a country, and inflated the country-level rollup. `Oman` sits at
that tier because Oman *is* a country; Anbar was the first non-country key given it. Two facts —
what kind of place was named, and how wide the answer is — were being forced through one field,
which is the error this same batch argues against elsewhere. The tier is now `admin1` and the bound
lives in a new per-key table, `_KEY_UNCERTAINTY_M`, which holds **measurements** (Nominatim's
half-diagonal for that name) and only where the tier estimate errs **narrow**: `anbar` /`al-anbar`
at 352,000 m against admin1's 100 km, and `ben gurion airport` at 3,265 m against the facility
tier's 500 m. Where the estimate already errs wide it stands — `fujairah` keeps admin1's 100 km
against a measured 50.5 km — because the field is a bound. Exact hits only: a partial match has
already been coarsened one tier on purpose, and the key's measured extent is not the bound for
something merely *near* it. What actually failed here is admin1's own unmeasured 100 km estimate,
and that is a question about `_TIER_UNCERTAINTY_M` and its 14 other keys, left open rather than
answered by four entries.

### `evidence_span` against the archive — measured 2026-09-21

`killed_reported` earned its place by being checkable against `raw_text` in one second. A location
had no such handle, so a model naming a plausible town that appears nowhere in the message produced
a row indistinguishable from a quoted one. Measured over all 83,938 archive events, replaying the
exact rule that shipped:

**Two populations, and the column is written on the wider one.** The split below is over the
45,619 rows that name a place. The column is written on all 83,938, and the 38,319 that name none
are not part of any percentage here — reporting the split without that sentence is how the first
version of this table read as a statement about the whole archive.

| | Events | Share of place-naming rows |
|---|---|---|
| `location_name` is not a place (45.7% of all rows never reach the search) | 38,319 | — |
| **names a place** | **45,619** | **100%** |
| → carries a quote | 35,488 | 77.8% |
| → carries `''` | 10,131 | 22.2% |
| of that `''`: partly quoted (a qualifier the text lacks) | 1,059 | 2.3% |
| of that `''`: not quoted at all | 9,072 | 19.9% |

The 19.9% is real derivation, not fabrication: a flag emoji, an adjective (`Israelis` → Israel),
another language (`صفد` → Safed), or an inference (`Beirut's southern suburb` → Dahieh). **`''` says
the location is not quotable from this text, and nothing more than that.**

**A value that is not a place has nothing to quote — and 60 rows quoted one anyway.** The first
implementation had no notion of `_NOT_A_PLACE`, so a row whose `location_name` is the classifier's
own "I could not tell" got a non-empty span whenever that word appeared in its text:
`evidence_span("Casualty figures remain unknown after the blast.", "Unknown")` returned `"unknown"`.
That is this project's whole bug class committed inside the new column — state 3 ("I looked and
could not tell") rendered in the field a reader reads as state 1 evidence — and it was reachable:
`telegram.py` and `news_feeds.py` both store `location_name = "Unknown"`, 38,314 archive rows carry
exactly that, and 104 of the live database's 201 do. Measured over the archive, 60 rows found their
own sentinel in their own text (55 on `unknown`, 5 on `NATO`). `evidence_span()` now applies the
same `_NOT_A_PLACE` test `geocode()` applies at its front door — the imported list, not a second
copy — and the replay of the shipped function returns **0**.

`''` therefore answers two questions, and `location_name` is the column that says which: no place
was named at all (38,319 archive rows), or a place was named and this text does not spell it
(10,131). They are separable in one query, which is why the second case did not get a fourth value:
every other value of this column is text lifted out of `raw_text`, so any sentinel string here is
one a message could also produce.

**The earlier numbers in this table were 35,354 / 77.5%, and they were measured wrong.** That
replay matched against the dump's COPY escape form instead of the text the column holds, where the
`\n` between two lines is a backslash and the *letter* n — a word character, so a location at the
start of a line has no `\b` before it. 134 events, all in one direction, all of the shape
`...deal with Saudi Arabia\n\nKyiv, which has built...`. The figures above come from importing
`classifier.evidence_span` itself and unescaping the dump first.

**A third matcher variant, measured and not fixed.** `\b` cannot match a `location_name` whose
first or last character is non-word — `"Zrariyeh"`, `Jaba'`, `USS Gerald R. Ford (CVN-78)` — even
when the name is verbatim in the text, because there is no boundary to find beside a quote mark or
a bracket. 7 events across 6 strings, against the 33 that the markdown-boundary and apostrophe-
folding rules bought and were rejected for. Recorded on the same arithmetic that rejected those.

Three things this measurement settled, each of which had been about to be built the other way:

- **The clever matcher was not worth it.** Treating markdown `_` as a word boundary bought 10 events
  out of 45,619; folding curly apostrophes bought 23 more. 0.07% for a cryptic lookaround, so the
  rule stayed the plain `\b` the rest of the file already uses. The non-word-edge variant above is
  smaller still, and is left alone for the same reason.
- **Lowering the text is a real bug, not a hypothetical one.** `'İ'.lower()` is two characters, so a
  lowered copy of one archive message is a character longer than the original and every offset past
  it is shifted. An implementation that searched the copy and sliced the original would store
  `'rariyeh '` — a genuine substring of `raw_text` that is not the place. The match runs on the
  original text under `re.IGNORECASE`, which makes the slice exact by construction.
- **The span does not certify that the model read it.** 969 archive quotes sit beyond offset 2000,
  past the window `classify_message` sends. The claim is about the text, not about the classifier.

Run against the **live dev database** on 2026-09-21, the repair pass gave all 200 rows a span in one
pass and printed nothing on the six reloads that followed. Re-read at 201 rows after the sentinel
fix: 97 name a place, of which 87 quote it and 10 do not; the other 104 name no place and carry
`''` — not because `''` is the catch-all, but because none of their texts happens to contain its own
sentinel. Under the first implementation that was luck; it is now the rule.

**The repair pass could spin, and its own comment rested on it not doing so.** It re-selected the
head of `WHERE evidence_span IS NULL` every iteration with no offset and no cap, so the guard was
the only exit — and the guard is only ever reached because `evidence_span()` happens never to
return `None`. Removing that property (a stub returning `None`) did not fail the suite, it **hung**
it, inside `engine.begin()` during `lifespan`: a held transaction and an app that never serves. The
loop now walks a cursor, `AND id > :last`, so it ends after at most `ceil(rows/5000)` iterations
whatever the function returns, and a row it could not fill stays NULL — the honest value, repaired
at the next boot. Both directions are now tested: with the cursor removed the new test reports a
`TimeoutError` in 61 s instead of hanging, and with the `IS NULL` guard removed the suite fails in
2.5 s on the *overwrite* assertion, which is the guard's actual job.

### GPS interference — measured 2026-09-19

Degraded share (`nic < 7 or nac_p < 8`) over a 250 nm radius, airborne vs at/above FL200:

| Point | airborne | ≥ FL200 |
|---|---|---|
| Frankfurt (peaceful control) | 23.9% (n=833) | **2.3%** (n=433) |
| Kaliningrad / Baltic | 20.8% (n=101) | **16.4%** (n=61) |
| Hormuz | 5.3% (n=57) | 4.9% (n=41) |

**Low-altitude degradation is aircraft equipage, not interference.** General aviation around a busy
hub carries older, cheaper GPS, so it reports low NIC everywhere. Without an altitude floor Frankfurt
is indistinguishable from Kaliningrad. With one, the control collapses to 2.3% while Kaliningrad holds
at 16.4% — a 7× separation, and the strongest open-source interference signal reachable.

This also invalidates an earlier claim: a "Baltic 50%" figure quoted on 2026-09-18 came from a
24-aircraft sample and does not survive a larger one.

**A snapshot cannot support a per-cell ratio.** At cruise-only density, 423 evaluable aircraft over a
250 nm radius leave ~4 per 0.8° cell; Kaliningrad cannot fill one cell at any grid size below 4°, and
Hormuz reads "40%" off four aircraft. Ratios must be pooled per named AOI, not per grid cell —
gpsjam.org gets away with fine cells only by aggregating over a full day.

Reproduce: `../tools/probe_adsb_coverage.py`.

### Sensor coverage — probed 2026-09-18

**ADS-B** (`api.adsb.lol`, 250 nm radius):

| Point | Aircraft | NIC | NACp | Degraded |
|---|---|---|---|---|
| Baghdad (33.3, 44.4) | **0** | – | – | – |
| Tehran (35.7, 51.4) | **0** | – | – | – |
| Hormuz (26.6, 56.3) | 121 | 121 | 118 | 4.1% |
| Tel Aviv (32.0, 34.8) | 43 | 43 | 43 | 2.3% |
| Baltic (55.0, 21.0) — known-jammed | 24 | 24 | 18 | **50.0%** |

The AO is not uncoverable. It was uncoverable *at the configured centre*, which sat in the one local
receiver hole. The `nic<7 or nac_p<8` threshold cleanly separates known-jammed airspace from normal,
so the interference layer is viable.

`api.airplanes.live` returns **403** and requires prior arrangement by email — it is not a drop-in
alternative.

**AIS** (`stream.aisstream.io`, free tier):

| Box | Duration | Messages | Distinct MMSI |
|---|---|---|---|
| Persian Gulf `lat 24–30.5, lon 50–58.5` | **1,565 s (26 min)** | **0** | **0** |
| E. Mediterranean `lat 31–38, lon 25–36` | 121 s (2 min) | 113 | **94** |

Same key, same code, same session, four minutes apart; both subscriptions confirmed by the server.
Corroborated by a separate live capture: 171 vessels, **easternmost at longitude 35.03**, zero in the
Red Sea, zero in the Gulf — and production's own box `[[10,25],[45,65]]` *did* include the Gulf.

**Conclusion: AISStream's free tier has no receiver coverage in the Persian Gulf.** The Hormuz
absence signal was structurally zero. It reported the absence of a sensor, not the absence of ships.

Reproduce: `scratchpad/ais_probe.py` (25 min Gulf + 2 min control).

### Internet connectivity — measured from 2026-09-19

Four IODA sensors per country (`bgp` 300 s, `ping-slash24` 600 s, `merit-nt` 300 s, `gtr` 1800 s),
**never averaged**: agreement between independent sensors *is* the confidence signal, and a sensor
that returns nothing is `unavailable`, never `normal`.

| Measured | Result |
|---|---|
| A flat percentage threshold is the wrong instrument | The reading with the largest percentage was the calmest (LB `gtr` −4.5% against a 12.4% normal swing, z −0.4); the smallest was the most unusual (RU `ping` −0.2% against 0.1%, z −2.6). Scoring is robust-z per sensor, `Z_DEPRESSED = -4` |
| The variance floor was required, not optional | Un-floored, IL `bgp` moved −0.16% against a 0.02% swing (z −7.2) and UA `ping` −0.54% against 0.06% (z −9.2) — two quiet countries reading as sustained disruptions on a denominator of rounding error. `TYPICAL_FLOOR = 0.005` |
| A trailing 24 h baseline cannot see a *sustained* decline | The baseline sits inside the outage. Cuba's traffic fell 64% over seven days and 24 h scoring called it **+29.7% nominal**. Short (24 h) and long (recent 24 h vs days 2–7) are now computed and reported separately, never blended |
| The detector fires, and is independently corroborated | Cuba reads `disruption_sustained`, basis long, 2 of 3 available sensors agreeing over 7 d, `gtr` −36.5%, z −4.9 — matched by a Cloudflare Radar annotation (ONGOING since 2026-09-18, POWER_OUTAGE, NATIONWIDE). The nine theatre countries read nominal, which is what shows it does not invent drama |

`bgp` staying flat while traffic collapses is what says Cuba is demand-side rather than a government
withdrawing routes — a distinction only available because the four are never averaged. Cloudflare
Radar is deliberately kept **out** of the IODA sensor count: two methodologies agreeing is worth more
than one merged number.

**Known limitation, measured not guessed** — carried over from `bb1c80c`, because a measured
limitation travels with the measurement: `gtr`'s long z runs hot, since a 6-day MAD does not model
its weekly cycle. IL fired at z −4.88 on a weekend that sits at today's level. The majority rule
absorbs it today (IL reads nominal), but a second `gtr`-like sensor would make it a false partial.

Reproduce: `GET /tracking/connectivity` against the running backend; thresholds, the 240 replayed
country-hours and the forced-failure test are recorded in `51bdce9` and `bb1c80c`.

### Local classification — qwen3:8b, 2026-09-19

| Fact | Value |
|---|---|
| Before the cutover | 126 real BBC / Al Jazeera / War Zone rows, **all** `extraction_status = "api_400"`, all severity NULL. The pipeline was honest about it — "SEV —" and a CLASSIFY FAILED badge — and nothing was being classified |
| After | qwen3:8b against the byte-identical `SYSTEM_PROMPT`: 0.5–0.9 s per article at zero marginal cost, and the first `extraction_status = "ok"` rows in this archive's history |
| Reachability | Only `host.docker.internal:11434` reaches the host's Ollama from the container — neither `172.17.0.1` nor `localhost` does |
| No silent fallback | An unreachable model tags `ollama_unreachable`; it does not quietly retry Anthropic. A backend that switches itself produces an archive nobody can interpret afterwards, and `extraction_model` is persisted so Haiku-era, `api_400` and qwen3 rows stay distinguishable |
| Known, and **still not** measurable this way | `de146f6`'s commit message states that qwen3 returns `location_name = "Unknown"` noticeably more often than Haiku did. It was never quantified, and the 2026-09-20 measurement below does not settle it in either direction: the 2026-08-18 dump carries no `extraction_status` column, so the archive's 45.6% cannot be split into Haiku-classified rows and fallback rows whose location came from `_build_fallback`'s regex. Settling it needs the key over the same sample. See the Corrections log |

The "~20 s cold start" is **not** a re-measurement: after an explicit model unload it reloaded from
page cache in 0.53 s, so 20 s stands as a worst case for a genuinely cold file cache, not an
observation.

Source: the acceptance test in `de146f6`, re-runnable against live RSS with `LLM_BACKEND=ollama`.

Everything in this table was measured on **qwen3:8b** on 2026-09-19 and is left naming it. The
running model has since changed to `qwen3.8-27b:latest` (`949aca8`) — which is why the rows carry
`extraction_model`, and why the two 2026-09-20 sections at the end of Measured facts name the model
they describe instead of assuming one.

### Stated death tolls in the archive — measured 2026-09-19

`killed_reported` is **copied** out of the message text, never estimated, so "does this message state
a toll" is a property of the string: no LLM, no key, no spend. The prior behind the field —
"~86% of messages carry no number" — was asserted in `classifier.py`'s own comment and shipped into
the live prompt with no artifact anywhere behind it. Measured across all 83,938 archive rows
(0 skipped, 0 with empty `raw_text`):

| Bucket | Events | Share |
|---|---|---|
| numeric count, English | 3,409 | 4.1% |
| numeric count, Arabic — a number near an Arabic kill word | 165 | 0.2% |
| one person, no numeral (`a paramedic was killed` → 1) | 97 | 0.1% |
| stated **zero** killed → `killed_reported = 0`, not NULL | 88 | 0.1% |
| **states no count → `killed_reported = NULL`** | **80,179** | **95.5%** |

95.6% if the singular-person bucket is read as "no count" instead. A further **98** rows say no toll
has been *announced / reported / confirmed yet* — state 3, not a zero, so they count as "no count"
rather than joining the 88. 3,339 rows (4.0%) mention wounded, injured or missing while stating no
death toll. 13,946 (16.6%) contain Arabic script, and the Arabic pattern only asks whether a number
sits near a kill word, so that is where the remaining error lives. Toll rates differ by an order of
magnitude between sources: `rss_middle_east_eye` 13.7% (931/6,789) against `telegram` 2.8%
(1,389/49,369), and telegram is 58.8% of the archive.

**This is an upper bound on the NULL rate, not a point estimate.** The patterns catch the phrasings
written into them and miss every other wording, so the true "states no count" share is *at most*
95.5%. One error runs the other way and that bound does not cover it: a cumulative war total quoted
as background counts here as a stated toll, while the classifier must **not** copy it into the
incident's `killed_reported` — `73,000` appears 32 times in the extracted-number table, which is
exactly that phrasing. The population also differs from the prompt's claim: these are *stored*
events, so `[NOISE]` and low-severity rows were dropped before insert and dedup merged repeats, which
pushes the null rate across all messages *seen* higher still. Neither bias is quantified.

Reproduce: `../tools/archive_killed_rate.py`, read-only on the VPS like the other `archive_*` scripts
(`scp` it to `/tmp/`, then `ssh truthevades 'python3 /tmp/archive_killed_rate.py'`). It prints the
extracted-number table and a sampled bucket file so the regex itself can be audited.

### `killed_reported` exercised against a running stack — 2026-09-20

`e5ad5ae` shipped compiling clean with `tsc` at zero and **nothing in it had run**. It has now run:
`docker compose up` with `DEMO_MODE=false` and qwen3:8b over Ollama, against the pre-existing
`conflict-monitor_pgdata` volume rather than a fresh one.

**The startup migration added the column to an existing database** — `killed_reported | integer |
nullable`, beside `extraction_model` and `geo_precision`, with no manual step and no crash.

**Live classification writes it, and the values are checkable in a second** — which is the whole
argument for this field over severity. 58 rows classified by qwen3:8b, six carrying a count:

| `killed_reported` | what the message said |
|---|---|
| 1 | "Israeli forces **kill Palestinian** near Jenin" — singular, no numeral |
| 3 | "IDF says it **killed 3** Hamas operatives" |
| 2 | "**Two killed**, 20 wounded" — the wounded exclusion holding live |
| 1 | "**Father of six** shot dead" — the `6` correctly *not* copied |

The last two are exactly what the prompt rules exist for: a numeral belonging to the wounded, and a
numeral belonging to the victim's children.

**The merge policy behaves as documented**, driven through the real `check_duplicate` ->
`merge_duplicate` against the real session, then cleaned up:

```
seeded #45560          killed_reported=None
check_duplicate     -> MATCHED (sim=0.44)
merge(killed=17)    -> 17    INFO:    killed_reported NULL -> 17, stated by channelB
merge(killed=40)    -> 17    WARNING: row holds 17, channelC reports 40 - keeping 17
merge(killed=None)  -> 17    a silent report did not erase it
```

Zero `parse_failed`, zero `ollama_*` failures and zero bool rejections across the run.
`fix-null-coords` completed 30 of 97, the other 67 keeping NULL rather than inventing a location.

**That `sim=0.44` is the finding, not the pass** - see `C74`. The margin between a correct merge and
a death toll stamped onto the wrong event is four hundredths of a summary-word Jaccard.

**That merge behaviour is gone as of *this commit*.** The transcript above is kept because it is a
true record of what `e5ad5ae` did, not because the tree still does it. `merge_duplicate` no longer
writes `killed_reported` on any branch, so the same drive — real `check_duplicate` ->
`merge_duplicate`, real session, no classifier and no GPU — now prints this instead, against a
Postgres started on its own with `docker compose up -d db`:

```
A) row #45585   raw_text contains no number        killed_reported=None
B) check_duplicate                             ->  MATCHED #45585 (sim=0.44)
C) merge(killed=17)                            ->  killed_reported=None
                                                   report_count=2  channels='channelA, channelB'

   INFO: Duplicate detected: existing #45585 (sim=0.44)
   INFO: Event #45585: channelB reports 17 killed - DROPPED, not stored anywhere.
         Row keeps killed_reported=None, which is a claim about this row's own
         raw_text. The count's durable home is C10's per-report table.
   INFO: Merged into event #45585 (now 2 reports | sources: channelA, channelB)
cleanup: 1 row(s)
```

Driven against `HEAD` and the working tree in one process over the same seeded pool, so the
before/after is measured rather than asserted: `HEAD merge(killed=17) -> killed_reported=17`,
`tree merge(killed=17) -> killed_reported=None`. The seeded rows were deleted and the row count
re-read. The old disagreement `WARNING` is gone with the fill: one integer could hold neither
number honestly, and the surviving line prints both.

`report_count`, `severity`, `source_reliability` and `reporting_channels` are untouched by this
change and still transfer exactly as before. The old disagreement `WARNING` is gone with the fill:
one integer could hold neither number honestly, so the surviving line prints the incoming count and
says plainly that it is dropped.

**Scope deliberately not taken.** The panel's second-place design and one judge both wanted
`check_duplicate` to return the best-scoring candidate rather than the first over the bar. It was
implemented, reviewed, and then reverted: it does not trace to `C74`, it silently changes *which*
row accumulates merge statistics, and its own justification in the code was Phase 4 work that does
not exist yet. It is `C77` instead.

### The fallback rate — two models on identical text, 2026-09-20

Phase 0's last classification line read "run for one week and record the real fallback rate". A week
of wall clock is the wrong instrument for the *model* half of that number: the live stack produces
~150 events a day, so a week is ~1,000 messages of whatever happened to be in the news, arriving
seven days late. The archive holds 83,938 real messages across five months and 46 sources, and since
`de146f6` classifying them costs nothing. A 568-row stratified sample (seed 20260818, 12 strata, a
floor of 20 rows per stratum) was pushed through the project's own `classify_message()` — the real
function, not a copy of the prompt, so every failure-tagging branch that runs in production is the
branch measured — first on qwen3:8b, then, after `949aca8`, on `qwen3.8-27b:latest`. **Same rows,
byte-identical text, verified row for row.**

| Same 568 rows | qwen3:8b | qwen3.8-27b:latest |
|---|---|---|
| `extraction_status` | `ok` × 568 | `ok` × 568 |
| **fallback rate** | **0/568 = 0.00%**, 95% CI 0.00–0.67% (Wilson) | **0/568 = 0.00%**, 95% CI 0.00–0.67% |
| `[NOISE]` / would be stored | 64 / 491 | 59 / 497 |
| `location_name = "Unknown"`, like-for-like | 86/491 = **17.5%**, CI 14.4–21.1% | 44/497 = **8.9%**, CI 6.7–11.7% |
| …over all `ok` rows, for comparison | 91/504 = 18.1% | 48/509 = 9.4% |
| severity 5, like-for-like | 17.5% | 13.5% |
| seconds per message | mean 0.61, median 0.61, p95 0.79, max 1.08 | mean **1.98**, median 1.99, p95 2.65, max 3.49 |

**Neither model's fallback rate is measured; both are bounded.** 0/568 yields the same 0.00–0.67%
interval for both, so this run cannot distinguish them on the quantity it was built to measure. The
27b's real parse-failure risk was demonstrated elsewhere and is not in this table — with thinking
left on it returned 3/3 empty responses (`949aca8`), which is why the negative control now carries a
`parse_failed` branch.

**The one substantive difference is the `Unknown` rate**, 8.9% against 17.5% on identical text with
non-overlapping intervals. It is evidence about two local models and **not** about Haiku: the dump
has no `extraction_status` column, so the archive's 45.6% mixes Haiku-classified rows with rows where
no classification happened and the regex fallback supplied the location. No winner is declared here.

Read against the archive's own numbers with care:

- **The archive's 86.3% severity-5 is not comparable to either column.** The dump carries no
  `extraction_status`, and before `89c6f54` a fallback row was *given* severity 5 (`C1`, `C2`), so an
  unknown share of that 86.3% is the old default rather than a judgement.
- **No model could have put a 1 or a 2 in that archive.** `MIN_SEVERITY = 3` (`telegram.py:29`,
  `news_feeds.py:66`) drops anything below it before insert (`telegram.py:176`, `news_feeds.py:347`),
  and a pass over the dump confirms **zero** stored events at severity 1 or 2. The sample's 1s and 2s
  are rows measured *before* that filter runs; what they show is how much of a model's output the
  pipeline discards, not a change in behaviour.
- **Archive comparisons here are like-for-like**, over the sampled rows that would themselves have
  been stored — the caller gate reproduced exactly as written (`severity is not None and severity <
  MIN_SEVERITY`, so an unmeasured severity passes, as in production). The wider denominator is
  printed beside it, labelled, and is not the one to difference against the archive.

Five things the numbers do not carry on their face:

- **Coverage.** 46 archive sources; 28 have at least one row in the sample and **18 have none**. Of
  12 strata, 7 sit exactly at the floor of 20 rows, and the pooled `other (<1%)` stratum is 35 feeds
  in 43 rows — 17 with at least one row, 7 with exactly one, 18 with none. **A stratum interval is
  not a per-feed interval**, and the feeds with no row carry no bound of any width.
- **The counter was proved, not assumed.** A rate of 0 is only as good as the thing counting, so a
  five-branch negative control runs in the container against the 27b: `ollama_model_missing`,
  `ollama_unreachable`, `no_backend`, `bad_backend` and — via a stub daemon returning Ollama's
  envelope with an empty `response`, the exact shape the 27b produced before `949aca8` —
  `parse_failed`. **5/5 tagged as expected**, all five counted as fallbacks, and the healthy path
  restored afterwards.
- **`format: "json"` forecloses two parse-failure shapes before the model is involved, but not the
  one that bit.** Measured against the same daemon: under `format: "json"` a request for the bare
  number 7 came back as `{"number": 7}`, while the same request with `format` removed returned
  `[1,2,3]`. The grammar does not foreclose an *empty* reply.
- **Cost.** The 27b is 3.3× per message. At ~150 events/day that is minutes, not hours, but the
  single GPU is serialised by `Semaphore(1)`, so a burst of RSS classification queues three times as
  long.
- **The model is set by `.env`, not by the code's default.** `config.py:19` still reads
  `ollama_model: str = "qwen3:8b"` while `.env` sets `OLLAMA_MODEL=qwen3.8-27b:latest`. Trust
  `extraction_model` on the row, which is why the report reads the model off the rows rather than off
  a literal — the 27b's first run printed itself as "qwen3:8b" until that was fixed.

Reproduce (`../tools/archive_fallback_rate.py`, run with no argument to print both halves):

```bash
scp tools/archive_fallback_rate.py truthevades:/tmp/
ssh truthevades 'python3 /tmp/archive_fallback_rate.py sample'      # read-only, writes /tmp only
docker cp sample.json conflict-monitor-backend-1:/tmp/fallback_rate_sample.json
docker compose exec -T backend python /tmp/archive_fallback_rate.py classify
docker compose exec -T backend python /tmp/archive_fallback_rate.py selftest   # the negative control
```

Per-row results land at `/tmp/fallback_rate_results.json` **inside the container**, which a
`docker compose restart` wipes — `docker cp` them out before quoting anything from them.

### The sentinel migration, audited before it runs — 2026-09-20

`89c6f54` deleted the `(-25, 80)` sentinel from the code and shipped an idempotent startup migration
that NULLs `lat`, `lon`, `geometry` and clears the geo columns `WHERE lat = -25.0 AND lon = 80.0`,
logging `Retired Indian Ocean sentinel on %d event(s)`. **The SQL shipped; the run did not.** What
follows is the read-only audit that makes the run decidable — every figure is a **prediction** of
what the `UPDATE` would do to these rows, not a rowcount anything logged.

| Measured on the dump | Value |
|---|---|
| Rows the `WHERE` selects | **39,949** (47.6% of 83,938) |
| …carrying a real place name | 1,635 (4.1% of selected) |
| …carrying `location_name = "Unknown"` | 38,314 (95.9% of selected — and **100%** of the archive's `Unknown` rows) |
| Near-misses, every band from 1e-06 to 5.0 degrees | **0** |
| Half-matches (`lat = -25.0` with another lon, and the reverse) | 0 / 0 |
| Selected rows whose `geometry` disagrees with lat/lon | 0 |
| Selected rows with NULL, or undecodable, `geometry` | 0 / 0 |
| Undecodable geometries anywhere in the archive | 0 |
| Geolocation rate, before → after | 100% → **52.4%** (43,989 rows keep a geometry) |

Because every one of those is zero, a real run should log **exactly 39,949 — and a different number
would itself be the finding.**

**39,981 was not this count taken badly. It was a different estimator.** `archive_locations.py`
groups by `location_name` and emits one coordinate per name — the mode — and `geocoder_vs_archive.py`
then charges that name's *entire* event count to the sentinel whenever its modal coordinate lands
within 0.01° of `(-25, 80)`. So 39,981 answers "how many events belong to names that *mostly* sit on
the sentinel", not "how many rows sit on it". Replayed from the same read as the direct count, so the
two are compared as estimators rather than one being called the other's typo:

| | Rows |
|---|---|
| charged to the sentinel but not on it | 372 |
| on the sentinel but never charged | 340 |
| **misattributed in total** | **712** |
| net effect on the headline | **+32** |

The same proxy sat inside the second register row. `1,667 = 39,981 − 38,314` subtracts across two
different populations: the proxy's sentinel total, minus the events whose `location_name`
`geocoder.py` rejects outright. Both terms re-measured: 39,949 − 38,314 = **1,635**, 1.95% of
83,938. The two subtrahends are the same 38,314 for *different reasons* — one is every row
`geocoder.py` would refuse anywhere, the other is the `Unknown` rows sitting on the sentinel — and
they coincide only because every `Unknown` row **is** on the sentinel and no other rejected name
occurs in the archive. The whole error was in the minuend.

**Half of those 1,635 rows are repairable, and that argues *for* running the migration.** 817 of them
(50.0%, 41 distinct names) carry a name that resolves to a real coordinate elsewhere in this same
archive: `Israel` 425 at the sentinel against 356 at a real point, `Lebanon` 213 / 235, `UAE` 44 / 58,
`Saudi Arabia` 37 / 60, `Ben Gurion Airport` 4 / 28, `Beersheba` 3 / 25. The same geocoder succeeding
and failing on the same string on different days is a transient failure, not an unknown place — and
`_fix_null_coords_task` (`routes/events.py:75`) selects on `Event.geometry.is_(None)` (:80) and
re-geocodes, so it can never reach a row parked at the sentinel. Retiring the sentinel is what makes
those rows visible to the repair.

**What remains is one thing: execute it and record the rowcount.** The recommended route is a
throwaway restore — gunzip the dump into a *new* database inside the already-running
`conflict-monitor-db-1` (postgis/postgis:16-3.4), point a backend at it, let
`ADD COLUMN IF NOT EXISTS` create the four missing columns and the `UPDATE` run, capture the logged
count, delete the copy. The dump is never opened for writing and the migration is idempotent. Tick
the box with the exact wording "ran against a **restored copy** of the 2026-08-18 archive, rowcount
N" — not "against the archive": the production database these rows came from was torn down and no
longer exists to be migrated.

Two limits on all of the above:

- **The dump predates the Phase 0/1b columns.** `is_geolocated`, `geo_precision`, `geo_uncertainty_m`
  and `geo_method` are absent from it, as are `extraction_status`, `extraction_model` and
  `killed_reported` (those three come from the same `ADD COLUMN IF NOT EXISTS` step, `main.py:39-57`,
  not from this `UPDATE`, whose `SET` clause is at `main.py:98-103`). What the migration writes into
  them is therefore unanswerable here — not false, not unknown-but-present. The counts above are all
  about columns the dump does have: `lat`, `lon`, `geometry`.
- **There is one dump.** The proxy replay and the direct count read the same file. The audit shows
  they disagree and by exactly how much; it cannot confirm either against a second source, because no
  second copy exists.

Reproduce: `../tools/archive_sentinel_audit.py`, read-only on the VPS like the other `archive_*`
scripts — `scp` it to `/tmp/`, then `ssh truthevades 'python3 /tmp/archive_sentinel_audit.py'`.

---

## Findings ledger

60 candidate findings, first re-checked against the tree at `64b690a` and re-verified line by line against `de146f6`: **31 open**, 26 fixed, 3 invalid or external. Of the open ones, **none is critical** and 13 are high. (*This commit* closes `C10`, narrows `C11` and `C74`, and opens `C78` and `C79`. It also corrects the line itself: the previous text said "57 / 29 open / 14 high" while the tables held 58 rows and 30 open — the note claimed `ce5d994` opened `C76` when it opened `C76` **and** `C77`, and the tally was incremented once. The same drift, in the same sentence, two commits running. Counted from the rows with a script, not carried forward.)

Claims that no longer hold are kept with status `INVALID` rather than deleted.


### Open

Ordered by severity, then area.

| ID | Sev | Phase | Area | Finding |
|---|---|---|---|---|
| `C74` | high | 4 | Ingest | A dedup false positive attaches one report's `report_count`, `severity`, `source_reliability` and channel to another event. Measured live: a *correct* merge fired at `sim=0.44` against a `> 0.4` threshold; unlocated rows match on text + time + type with no spatial predicate at all. The **death toll** left that blast radius in `ce5d994` and now has a home: `event_reports.killed_reported`, beside the raw_text that stated it. **The match is exactly as loose as it was** — that is the whole of what stays open. See also `C76`, `C77` |
| `C31` | high | 2 | Collection | A failed poll leaves the last fleet and a frozen as_of in place with status still "ok" |
| `C44` | high | 3 | Frontend | Timeline playback advances speed*1000 ms per 100 ms tick, and the real rate depends on tab visibility |
| `C45` | high | 3 | Frontend | Events, aircraft and vessels are DOM <Marker> overlays, not Source/Layer — 98 marker nodes measured live |
| `C46` | high | 3 | Frontend | 46 markers carry transition: transform 2s linear against a 15 s aircraft / 10 s vessel poll — fabricated motio… |
| `C51` | high | 2 | Frontend | WebSocket reconnect refetches nothing and the server sends no backlog: events during a drop are lost until rel… |
| `C11` | high | 4 | Ingest | **Narrowed in *this commit*.** The boost now gates on distinct channels among the event's report rows, not on `report_count`, so a source repeating itself no longer raises confidence. The count is a **floor** bounded by what the table holds, so a pre-cutover row with one backfilled report row stays below the gate even for new reports arriving now — 141 archive rows (2.1%) lose a boost they should keep, stated rather than discovered later. What stays open is the part that needs a new subsystem: `x_osintwarfare` and `OSINTWarfare` are one outlet on two transports and still count as two, and 15 channels reposting one text still read as 15 confirmations. Needs the `fwd_from` channel-family graph |
| `C6` | high | 1 | Ingest | Country names resolve to national centroids via Nominatim and are marked is_geolocated=true |
| `C60` | high | — | Platform | No auth on any route or the WS; CORS reflects any origin with credentials, DELETE allowed |
| `C62` | high | — | Platform | All seven /events/admin/* endpoints, including both DELETEs, are unauthenticated |
| `C63` | high | 1 | Platform | Alembic has no versions/; schema comes from create_all plus a hand-kept ALTER list that already crashed startu… |
| `C69` | high | — | Platform | demo.py attributes fabricated strikes on real nuclear sites to real named OSINT outlets, on a public MIT repo |
| `C66` | medium | — | Platform | **Narrowed in *this commit*.** 103 tests now cover the behaviours the last two days verified by hand, and each one was demonstrated to FAIL against its own defect re-introduced (nine mutations, nine catches). What stays open is the part a test suite cannot fix by existing: there is still no CI, so it runs only when someone types the command — and the suite cannot see schema drift, because the test database comes from `create_all` while production's comes from the hand-kept ALTER list (`C63`). The counter-test is named in `tests/conftest.py` and deliberately not written yet |
| `C29` | medium | 2 | Collection | Second AIS box is mislabelled "Eastern Mediterranean" and supplies 85% of the vessel feed from outside any AO |
| `C32` | medium | 1 | Collection | altitude mixes feet (adsb.lol) and metres (OpenSky) in one field, and the UI labels it both ways |
| `C48` | medium | 3 | Frontend | Event type is encoded by hue alone on map, globe, terrain and timeline; only the feed carries a text label |
| `C49` | medium | 3 | Frontend | No prefers-reduced-motion guard anywhere: 6 keyframe animations, an 8s scan line and an audio blip |
| `C50` | medium | 3 | Frontend | LiveFeed NEW badge compares array lengths against a 200-cap, so it stops firing permanently once the cap is hi… |
| `C53` | medium | 3 | Frontend | Three renderers (Mapbox, Globe, Cesium) still duplicate mark logic. The palette half is fixed: the four stale EVENT_COLORS copies were migrated onto tokens.ts in `e03cee6`. Deleting `GlobeView` was built and then **reverted** — the globe is visually distinct and the owner wants it kept, so the rules move to one shared module while the drawing stays two |
| `C75` | medium | 1 | Ingest | "Could not classify" and "could not place" are the same rows, not two overlapping populations: **100%** of the archive's 38,314 `Unknown` rows sit on the sentinel, and the sentinel holds those plus 1,635 others. The two headline failure rates (45.6%, 47.6%) are one population reported twice. `89c6f54` made the stages separable on new rows — name kept, `geometry` NULL, `geo_method` persisted — but nothing reports them apart |
| `C76` | medium | 4 | Ingest | The dedup score is Jaccard over `summary`, the LLM's *paraphrase*, so `> 0.4` measures how similarly the model worded two things rather than how similar two reports are — and `classifier.py` `_build_fallback` sets `summary = raw_text[:200]`, so a fallback row is not compared like with like at all. Containment over the reports' own `raw_text` is the instrument; picking a bar for it needs Phase 4's gold pairs |
| `C77` | medium | 4 | Ingest | `check_duplicate` returns the **first** candidate over `0.4` in timestamp-desc order, not the best-scoring one, so a merge attaches to the most recent match rather than the most similar — and the logged `sim` is that row's score, not the pool maximum. Fixing it changes which event accumulates `report_count` / `severity` / `source_reliability`, so merge statistics either side of the change are not comparable; it is a deliberate separate decision, not a tidy-up |
| `C79` | medium | 4 | Ingest | **Opened by *this commit*, which created it.** `event_reports.event_id` is `ON DELETE CASCADE` — it has to be, or the two raw-SQL deletes in `events.py` fail on the FK the first time any report row exists. But `/admin/dedup` keeps `MIN(id)` per `(source, raw_text)` and deletes the twin, and the cascade now takes that twin's report rows with it — including the only stored copy of a report the survivor never had. That is `C10` reappearing in a new place. Re-pointing the reports at the survivor is the fix, and it is a decision rather than a mechanical port |
| `C61` | medium | — | Platform | Postgres published on 0.0.0.0:5432; backend DATABASE_URL hardcoded so POSTGRES_PASSWORD cannot change it |
| `C68` | medium | — | Platform | Vite HMR is blind across the Windows bind mount; the backend only reloads because watchfiles polls |
| `C70` | medium | 0 | Platform | Demo path bypasses classifier, geocoder, dedup and track_history, so the zero-config run exercises none of the… |
| `C71` | medium | 3 | Platform | Demo rows ARE tagged source='demo' in the DB and API; it is the UI that discards the distinction |
| `C78` | low | 4 | Ingest | **Surfaced by *this commit*, not fixed by it.** `dedup.py`'s channel append tests `if new_channel not in channels` against the joined *string*, so it is a substring match, not set membership: a genuinely distinct channel whose name is a substring of one already listed is silently never appended. The archive holds exactly this pair — `osint613` inside `x_osint613` — and two more that only a case difference saves (`OSINTWarfare`/`x_osintwarfare`, `GeoConfirmed`/`x_geoconfirmed`). Display string only: `event_reports` stores rows, so the distinct-channel count `C11` now uses is right even where the string is not. Fixing the string needs either string surgery or an overwrite that destroys the archive's only record of those names |
| `C65` | low | — | Platform | Shutdown calls task.cancel() without awaiting, so no client-close path is guaranteed to run |
| `C67` | low | — | Platform | No .dockerignore; frontend COPY . . does overlay host node_modules, but the linux binaries survive |

### Fixed

Verified fixed in the current tree. Each row names the commit that closed it.

| ID | Sev | Phase | Area | Finding |
|---|---|---|---|---|
| `C41` | critical | 0 | Frontend | A keyless install now opens on the globe, which needs no token (`075ce6f`), so the documented quick start no longer renders a black rectangle. Residual: choosing 2D with no token still draws black with no in-panel notice |
| `C5` | critical | 1 | Ingest | Word-anchored patterns, compiled once at import, in **both** places — `_KNOWN_PATTERNS`/`_DIRECTIONAL_PATTERNS` in geocoder.py and `_LOCATION_PATTERNS` in classifier.py (`075ce6f`) |
| `C8` | critical | 1 | Ingest | check_duplicate branches explicitly on geometry: a located event requires `geometry IS NOT NULL` + ST_DWithin, an unlocated one is confined to `geometry IS NULL`. Neither pool can absorb the other (`89c6f54`) |
| `C10` | high | 4 | Ingest | An `event_reports` child table holds one row per incoming report — `raw_text`, `summary`, `source_url`, `telegram_message_id`, `killed_reported`, `extraction_status`, `reported_at` — written at both insert sites and, inside the merge's own transaction, at merge. Nothing a contributing report carried is dropped any more. The re-ingest half is closed with it: both dedup guards read the report table, so a merged message is recognised on restart instead of being re-classified and re-merged (*this commit*). Two things it does **not** do: it cannot recover the 19,027 reports already destroyed, and it is only half of Phase 4's "link, don't merge" — there is still no link *between* events. Residual debt: `C79` |
| `C21` | high | 2 | Collection | adsb.lol 403 to the default httpx User-Agent - fixed by a contact-bearing UA |
| `C24` | high | 2 | Collection | Jamming test no longer reads position_source/mlat (ground-feeder density) |
| `C25` | high | 2 | Collection | `nac_p == 0 and nic == 0` replaced with the published gpsjam threshold |
| `C26` | high | 2 | Collection | Jamming emits a ratio with an explicit denominator and a minimum cell size |
| `C27` | high | 0 | Collection | "I cannot measure" is now a distinct status, not zero zones |
| `C28` | high | 2 | Collection | AO re-centred and narrowed; aircraft coverage verified live |
| `C42` | high | — | Frontend | tsc/vite build errors are gone: fixed today in 0bd4835 (sun/moon guards, Terrain arg, useRef initial value) |
| `C20` | high | 2 | Collection | On-disk TLE cache written atomically via os.replace and loaded at startup, the fetch skipped while it is fresh, 1h backoff on 403/429, and the cache replaced only when the fetch returned rows (`0de11f2`) |
| `C47` | high | 1 | Frontend | types/event.ts carries the provenance fields the API returns (`e60c44d`), extended with geo_precision (`89c6f54`) and extraction_model (`de146f6`) |
| `C12` | high | 0 | Ingest | Both admin tasks abandon the row when re-classification is not `ok` instead of overwriting severity/summary, and write extraction_status, extraction_model, is_geolocated and the geo_* columns when it is (`de146f6`) |
| `C2` | high | 1 | Ingest | severity is nullable end to end (models, schemas, ClassifierResult), the fallback literal is gone, and dead clamp_severity was deleted rather than repaired (`89c6f54`) |
| `C4` | high | 1 | Ingest | Sentinel deleted. An unresolvable location persists as NULL lat/lon/geometry with is_geolocated=false, and the three predicates that read -25.0 went with it (`89c6f54`) |
| `C7` | high | 1 | Ingest | Actor acronyms removed from KNOWN_LOCATIONS and added to `_NOT_A_PLACE`, so a bare "IDF" no longer falls through to Nominatim (which answered with a point in Armenia). A named HQ building may stay; a command or a fleet may not (`075ce6f`) |
| `C9` | high | 1 | Ingest | Deterministic `order_by(timestamp DESC, id DESC)` before the limit (`075ce6f`); the cap later split into 20 located / 200 unlocated, sized on the densest observed window (`89c6f54`) |
| `C22` | medium | 2 | Collection | Aircraft cache timestamp is now wall-clock, but /tracking/aircraft still exposes no timestamp at all |
| `C23` | medium | 0 | Collection | Poll log hard-coded "adsb.lol" - now reports the source actually used |
| `C40` | medium | 3 | Frontend | app-grid row minimum: already minmax(0,1fr), fixed earlier today in 716ffec |
| `C1` | medium | 1 | Ingest | Fallback rows are now tagged extraction_status; the fabricated severity=5 went with `C2` (`89c6f54`) |
| `C43` | medium | 3 | Frontend | EscalationGauge deleted; IndicatorRail replaces it, reads no severity at all, and states each row's own coverage with DEGRADED as a first-class state (`e60c44d`) |
| `C52` | medium | — | Frontend | The Cesium credit container is a real, visible node in the panel instead of a detached div, so Ion/Bing/Google attribution renders where their terms require it (*this commit*). Stated, not observed: the same work gates the terrain control off when `VITE_CESIUM_ION_TOKEN` is unset, and this install has none, so Cesium does not mount here and the rendered credits have not been seen |
| `C3` | low | 0 | Ingest | parse_failed is a distinct tagged status; the non-dict JSON hole closed too — a list, number or bare string now raises a decode error, so both backends record parse_failed (`de146f6`) |

### Fixed by the v3 rebuild

Present in the old `main` lineage, absent in v3.

| ID | Sev | Phase | Area | Finding |
|---|---|---|---|---|
| `C13` | medium | — | Ingest | ChannelCheckpoint exists in v3 and makes backfill incremental; the checkpoint only advances on a saved event |
| `C64` | medium | 0 | Platform | Background tasks now carry a done-callback that logs the exception; no bare create_task remains |

### External

Real, but not a code defect.

| ID | Sev | Phase | Area | Finding |
|---|---|---|---|---|
| `C30` | high | 2 | Collection | AISStream genuinely has no Persian Gulf receiver coverage - not a bounding-box bug |
| `C72` | high | — | Platform | Anthropic key returns 400 organization_on_hold, so that backend cannot run. It no longer stops classification: `de146f6` made a local Ollama backend the default. It still stops any measurement *of Haiku*, the model that produced the archive |

### Invalid

Did not survive checking.

| ID | Sev | Phase | Area | Finding |
|---|---|---|---|---|
| `C73` | low | — | Platform | frontend/dist is not committed and never has been — it is an untracked local build artifact |

### Detail: critical and high findings, open and just closed

A block stays here for one commit after its finding moves to Fixed, so the closure can be read
against the evidence that argued for it rather than against a one-line table row.


#### `C74` — A dedup false positive transfers corroboration it has not established

`high` · phase 4 · Ingest

**Where:** dedup.py `check_duplicate`. A match needs the same `event_type`, a timestamp inside ±15
minutes, and Jaccard over `_normalize(summary)` word sets `> 0.4`. A located incoming event
additionally needs a 50 km `ST_DWithin` against a located row; an unlocated one gets **no distance
test of any kind** — `geometry IS NULL` partitions the pool, it does not place it — and matches on
text + time + type alone across `_UNLOCATED_CANDIDATES = 200` rows. The only live merge ever
observed scored **0.44** against that `> 0.4` bar, and it was a *correct* match.

**Impact:** `report_count` increments, `reporting_channels` gains a name, `severity` takes the max
and `source_reliability` climbs — on that evidence. Those are corroboration claims: the row says two
sources confirmed this event. A false positive fabricates all four, and `C11` then walks reliability
upward for it.

**What `ce5d994` did, and did not do.** `merge_duplicate` no longer writes `killed_reported`.
That is a contract fix, not a matcher fix, and the distinction is the whole of this entry:

- models.py defines the column over *this row's own* text — "copied from its text … a quantity a
  reader can check against `raw_text` in one second" — and a merged count is copied from a different
  report's text, which this function does not store and cannot store (`C10`). So the one-second
  check comes back **negative on a row that is not wrong**, and a reader cannot tell a merged 17
  from a hallucinated 17. That is true of the *correct* merge above, which is why no threshold fixes
  it.
- The check is not currently reachable from the UI that prints the number either: `raw_text` appears
  nowhere in `frontend/src` outside `types/event.ts:5`, so `LiveFeed` renders `17 KILLED` under a
  tooltip reading "copied from its text" with that text nowhere on screen. That sharpens `C10`; it
  does not soften this fix.
- **Anyone reading `C74` as "dedup is too loose" will find it exactly as loose afterwards.** This
  makes a false positive cheaper, not rarer.

**No backfill was needed, and that is luck rather than design.** main.py adds the column with
`ADD COLUMN … NULL`, so all 83,938 archive rows are NULL — the column postdates the archive
entirely, and the only rows a merge ever filled are from the 2026-09-20 verification run against the
scratch DB. Nothing distinguishes a merged fill from a classified one, so had the exposure been real
it would only have been findable by re-classifying every row's `raw_text` and comparing, at GPU
cost.

**Fix — the named next step, which is the unlocated branch's unused place-claim.** An unlocated row
is not placeless: it carries `location_name`, and the matcher ignores it. Requiring the two to agree
is not a new signal, it is a signal already on the row. The catch, and the reason this is a separate
commit rather than a line here: `classifier.py` `clean_location` folds `""`, `n/a`, `various` and
`multiple` into the literal string `"Unknown"`, and `_build_fallback` writes it when the regex
fallback finds nothing either — so `"Unknown"` is the classifier saying *I looked and could not
tell*, and the matcher currently reads two of those as **agreement about location**. On the
archive's own numbers that is 95.9% of the pool this branch will see (38,314 of the 39,949 rows the
sentinel migration unpins). Refusing to match when the incoming report claims no place is therefore
right on the governing idea and is a large, unmeasured product regression at the same time:
duplicate pins, `report_count` undercounted, and the archive's 8.16% merge rate no longer comparable
across the change. It needs its own measurement and its own commit.

Two options *not* taken, recorded so they are not re-litigated:

- **Raise the threshold.** The only labelled pair this project owns is a true positive at 0.44, so
  it argues against raising the bar, not for it — moving it on one data point that the move would
  have destroyed is inventing a number. And the score runs on the LLM's paraphrase (`C76`), so a
  stricter bar measures wording agreement more strictly, not event identity better.
- **`and existing.geometry is not None` on the count transfer.** Proposed as the cheap one-clause
  gate; with the transfer deleted it has nothing left to gate. The transfers that remain are
  corroboration judgments, and gating *those* on geometry would turn unlocated dedup off wholesale
  rather than selectively — which is what the `location_name` predicate above does, for a measured
  95.9% of the pool.

**The instrument that would price this, free.** Nothing measures what the deletion costs: 4.5% of
archive messages state a toll and 8.16% of events are merges, so the joint case is on the order of
0.4% of events losing a stated number — a product, not a measurement, and probably an underestimate
because a deadly event attracts more channels. A read-only replay of `check_duplicate`'s predicate
over `/root/archive/conflict_monitor-20260818.sql.gz` — same `event_type`, ±15 min, the
located/unlocated split, the 20/200 caps, timestamp-desc — would print, for every pair it would
merge, the summary Jaccard, the raw-text containment and whether `archive_killed_rate.py`'s regex
finds a toll in exactly one of the two texts, i.e. whether the pair is a *transfer*. Same pattern as
`tools/archive_killed_rate.py`: both inputs are already columns in the dump, so it needs no GPU, no
key and no running stack.

**What *this commit* did: the count stopped being deleted, and a second dropper was found.** The
incoming `killed_reported` is no longer logged and discarded — it is `event_reports.killed_reported`,
on the report row inserted by the merge, in the same tuple as the `raw_text` it was copied from. The
contract does not weaken in the move, it tightens: the one-second check stops being merely true and
becomes *local*. One integer on one event row could not say "A stated nothing, B stated 17"; two
report rows say exactly that, each answerable from its own text. Driven against Postgres: two
channels report the same strike with tolls of 3 and 17, the event keeps `killed_reported = 3` from
its own text, and both counts survive on their own rows.

**The matcher is untouched and this finding is unchanged by that.** What *did* change underneath it
is a second, unrelated way the same bug class was firing in the same file's callers:
`_message_already_saved` keyed on `telegram_message_id` **alone**, while Telegram ids are
per-channel, so a genuinely new message from a second channel was being dropped with "already saved"
in the log — absent written as present. The archive cannot show a single instance, because the guard
drops before the INSERT and the dropped rows are the ones that are missing; what it shows is five
channel pairs whose id ranges overlap and hold **zero** ids in common where treating the two id
sequences as unrelated predicts about **2,248** (`tools/archive_report_counts.py`). Re-keying the
guard on `(channel, telegram_message_id)` admits those. Ingest volume either side of this commit is
therefore **not comparable** — the same caution this document applies to `C77` — and it is in the
Corrections log rather than left to be noticed as a step in a graph.


#### `C31` — A failed poll leaves the last fleet and a frozen as_of in place with status still "ok"

`high` · phase 2 · Collection

**Where:** opensky.py:216 `if states is not None:` guards every cache write, and no branch records a failed attempt. Exercised at runtime in the container: primed the cache (1 aircraft, timestamp 1000000000, source adsb.lol, status ok, cells_evaluated 3), monkeypatched both _poll_adsb_lol and _poll_opensky to return None, ran one loop iteration -> get_aircraft() still returned the stale aircraft and get_jamming_zones() returned {"status":"ok","source":"adsb.lol","as_of":1000000000,"cells_evaluated":3}. C22's wall-clock fix makes as_of computable in principle, but routes/tracking.py:11-14 serves /tracking/aircraft as a bare array with no timestamp, and useTracking.ts:87-91 never looks at age.

**Impact:** A total outage of both ADS-B sources renders as a live fleet: planes keep drawing at their last positions, the AC counter keeps its number, the GPS legend keeps saying "ok", and nothing anywhere ages. This is the same class of defect C27 just fixed for jamming, one layer up.

**Fix:** Track poll outcome in _cache (`last_attempt`, `last_success`, `consecutive_failures`) on every iteration including failures, return {as_of, last_success, states} from /tracking/aircraft, and drive the header/marker liveness from `now - last_success`.


#### `C44` — Timeline playback advances speed*1000 ms per 100 ms tick, and the real rate depends on tab visibility

`high` · phase 3 · Frontend

**Where:** TimelineScrubber.tsx:72-87: `setInterval(() => { const advance = speed * 1000; ... }, 100)`; the comment on :74 states the intent ("speed * 1 second of real time per tick — tick every 100ms"), i.e. 10x the labelled rate. Measured in-browser: instrumented setInterval showed the 100 ms timer being created, firing, cleared and re-created once per tick. With the select at "30x" the window advanced 180000 ms of timeline in 6992 ms of wall clock (ratio 25.7) — but only because `document.visibilityState === "hidden"` clamped the timer to ~1000 ms. In a visible tab the same code ticks at ~100 ms, i.e. 10x the selected speed.

**Impact:** "1x" is not 1x. In a foreground tab it is ~10x; in a background tab ~1x. The scrubber's speed control is therefore not a clock at all — the same setting replays history at two different rates depending on whether the operator is looking at it.

**Fix:** Drive playback from elapsed wall time instead of a fixed per-tick delta: record `lastTickMs` and advance by `speed * (now - lastTickMs)` each tick (or use requestAnimationFrame with a delta). That makes the rate correct and visibility-independent in one change.


#### `C45` — Events, aircraft and vessels are DOM <Marker> overlays, not Source/Layer — 98 marker nodes measured live

`high` · phase 3 · Frontend

**Where:** MapPanel.tsx:304-321 (events), :324-344 (jamming zones), :347-392 (aircraft), :395-440 (vessels) all use react-map-gl `<Marker>`, which mounts a positioned DOM node per feature. Only the trail lines use GeoJSON layers (:263-301 `<Source>/<Layer>`). Measured on the running app at localhost:5173: `document.querySelectorAll('.mapboxgl-marker').length === 98`, `document.querySelectorAll('.mapboxgl-map *').length === 440`, with a single WebGL canvas.

**Impact:** Every marker is laid out and transformed by the browser on each map move; the count scales with events (capped 200) + aircraft + vessels + zones, so a live AO puts 250+ absolutely-positioned nodes over the canvas. It also means marks cannot participate in GL-side clustering, collision, data-driven styling or zoom-dependent geometry — which is what honest mark geometry (uncertainty radii, NULL-geometry handling) will require.

**Fix:** Move events, aircraft and vessels to `<Source type="geojson">` + symbol/circle `<Layer>` with data-driven paint expressions; keep DOM only for the selected-feature popup.


#### `C46` — 46 markers carry transition: transform 2s linear against a 15 s aircraft / 10 s vessel poll — fabricated motion

`high` · phase 3 · Frontend

**Where:** MapPanel.tsx:353 (aircraft) and :401 (vessels): `style={{ transition: "transform 2s linear" }}`. useTracking.ts:60 `AIRCRAFT_POLL_MS = 15_000`, :62 `VESSEL_POLL_MS = 10_000`. Measured live: 46 of 98 `.mapboxgl-marker` nodes have `style.transition === "transform 2s linear"`. The same fabrication exists on the globe: GlobeView.tsx:288-293 `useFrame(() => dot.current.lerp(dot.target, 0.06))` glides each aircraft/vessel toward its last reported position every frame.

**Impact:** For 2 s after each 15 s poll the aircraft glides smoothly; for the other 13 s it is frozen. The eye reads the glide as observed motion, so the display asserts a track between two samples that was never measured. Nothing on screen distinguishes a 1-second-old position from a 15-second-old one.

**Fix:** Delete the `transition` from both Marker styles (and the lerp in GlobeView) so a mark moves when and only when a new observation arrives; encode position age as opacity or a staleness ring instead.


#### `C51` — WebSocket reconnect refetches nothing and the server sends no backlog: events during a drop are lost until reload

`high` · phase 2 · Frontend

**Where:** useEventStream.ts:82-87 — the REST backfill is in a `useEffect(..., [])`, so it runs once on mount only. useEventStream.ts:72-76 `ws.onclose` sets isConnected false and schedules `setTimeout(connect, 3000)`; `connect` (:36-79) opens a socket and installs handlers, and never refetches. Server side: backend/app/routes/ws.py:12-14 accepts and immediately enters the receive loop; broadcaster.py:14-18 `connect()` only does `ws.accept()` and appends to the client list. No history, no cursor, no last-event-id.

**Impact:** Any event broadcast while the socket is down never reaches that client. The header flips back to a green pulsing LIVE (Header.tsx:138-151) the moment the socket reopens, so the UI asserts completeness it does not have; the only recovery is a manual page reload. A backend restart or a laptop sleep silently punches a hole in the feed.

**Fix:** On successful `ws.onopen`, re-run the REST fetch and merge by id (dedupe on `e.id`) rather than replace — a 3-line change in `connect`. A server-side backlog (send the N newest events on accept) is the more complete fix but is not required to close the hole.

**Since `e60c44d`** the header no longer flips back to a green pulsing LIVE: liveness is rendered as an age, and a nominal feed gets no mark at all. The hole is untouched — a reconnect still refetches nothing — but the UI no longer decorates it.


#### `C10` — merge_duplicate keeps only channel, severity and coordinates; the incoming report's text and identity are dropped

`high` · phase 4 · Ingest

**Where:** dedup.py:98-105 `async def merge_duplicate(session, existing, new_channel, new_severity, new_lat, new_lon)` — the incoming raw_text, summary, source_url and telegram_message_id are not parameters, so they cannot be kept. Call sites pass nothing more: telegram.py:288 `await merge_duplicate(session, existing, channel_name, severity, lat, lon)` and news_feeds.py:356. The body (:106-149) writes only report_count, reporting_channels, severity (max), lat/lon (only when existing.lat is None, dedup.py:122), and source_reliability.

**Impact:** After a merge the only surviving evidence is `report_count += 1` and a channel name appended to a comma-joined string. The second report's own wording, its Telegram permalink and its message id are gone, so nobody can later check whether the two reports were independent or verbatim copies — which is precisely the judgement C11's reliability boost depends on. It also means the merged message has no telegram_message_id row, so _message_already_saved (telegram.py:214-218) cannot recognise it on the next restart and it is re-ingested and re-merged.

**Fix:** Replace the merge with a link: an event_reports child table holding (event_id, source, channel, raw_text, summary, source_url, telegram_message_id, ingested_at), one row per report, with report_count and reporting_channels derived from it. That is the Phase 4 'link don't merge' change; the minimum interim step is to pass and store source_url and telegram_message_id.

**Since `89c6f54`** the signature is `merge_duplicate(session, existing, new_channel, new_severity)`: the coordinate parameters and the coordinate backfill were deleted, because `C8`'s geometry guard made the backfill unreachable. The merge therefore keeps strictly less than this block describes, and the finding is unchanged — the incoming report's text, URL and message id still have nowhere to go.

**Fixed in *this commit*.** The signature is now `merge_duplicate(session, existing, report, new_severity)`: four scalars became one `EventReport` row, which is *fewer* parameters carrying strictly more data. The report is built at both call sites **before** `check_duplicate` runs, because it is a fact about the incoming message whichever event it turns out to belong to, and is then attached to the event that already exists or to the one just inserted. "Link, don't merge" falls out of that ordering rather than being engineered.

- **It is written inside the merge's own transaction**, before `merge_duplicate`'s single `commit()`. A row added at the call sites after the function returned would commit separately, and a crash in between would leave `report_count` claiming a report with no row under it — a corroboration claim with no evidence, which is the thing this change exists to stop.
- **Every report row says whether anything classified it, because `killed_reported` alone cannot.** The column inherits all THREE of the event column's values, not two: `0` means the source said nobody was killed, NULL means it stated no count, and NULL *also* means nothing ever extracted a count from this text. On `events` that third value is a legacy corner; here it is the common case, because `classifier._build_fallback` returns a dict with **no `killed_reported` key at all**, so `.get()` is None on every failed classification — and with the Anthropic key dead that is every classification. An events row resolves it by reading its own `extraction_status`; a **merged report row cannot**, even by joining back, because the event's status describes a *different* report. Measured on the dev DB before the column existed: **98 of 159 report rows (61.6%)** were `killed_reported IS NULL` on an event whose `extraction_status` was not `ok`, and not one could say which of the three it meant. `event_reports.extraction_status` costs one column and no new plumbing — the merge call site already reads `result.get("extraction_status")` two lines earlier, to refuse a severity. Driven through the real merge path against Postgres: a report merged under a `bad_backend` fallback stores `bad_backend` on its own row while the event says `no_backend`, which is the answer joining back would have given.
- **The startup backfill carries the count and the status across together, and only together.** The event's status describes a classification of that report's own text, so transferring it is honest; it stays NULL exactly where we have nothing to say — every row written before `events.extraction_status` existed, which on a restored 2026-08-18 archive is all 83,938 of them, since that dump has neither column. NULL status beside a NULL count says "I cannot tell you why the count is missing", which is true. What it must never say is "nobody was killed". (The comment here previously claimed `killed_reported` "transfers unchanged, because its contract is copied from THIS row's raw_text" — untrue for every pre-column row, and that is the whole population of the archive.)
- **The re-ingest loop is closed, both halves.** `_message_already_saved` and the RSS URL guard read `event_reports`, which has a row for every report *including merged ones* — the case neither guard could see, because a merge writes no `events` row. And `_message_already_saved` is now keyed on `(channel, telegram_message_id)`: see `C74`'s note and the Corrections log for why that second change is the larger of the two.
- **The UNIQUE index behind that guard raises, and the raise is caught where it can be read.** The guard is check-then-act with a seconds-wide window, and `trigger_backfill()` — the unauthenticated `POST /admin/backfill`, no lock — can re-run a sweep while the startup sweep and the live handler are inside it. `_backfill_entity` wraps its **entire** `async for` in one try, so an uncaught `IntegrityError` ended the sweep for that channel: every later message never ingested, nothing retrying, and a log line that reads like a transient fetch failure — "I was not looking" recorded as "nothing happened", by the backstop added to prevent exactly that. `_process_message` now catches it and logs the guard firing. Driven against Postgres with a three-message sweep whose second message collides: messages one and three are ingested, the losing transaction rolls back cleanly (`report_count` stays 1), and the sweep ends with "Backfill done for zz_sweep: 3 total messages processed".
- **The backfill has its own transaction.** It shared one with `CREATE EXTENSION`, `create_all`, the 13 silently-swallowed migration ALTERs, the severity ALTERs and the sentinel `UPDATE`. In Postgres a failed statement aborts the transaction and SQLAlchemy takes no savepoint per execute, so any one swallowed failure poisoned everything after it — survivable while the later statements were themselves wrapped, fatal once the backfill was deliberately left unwrapped. Reproduced: a swallowed `ProgrammingError` from one ALTER, then the backfill → `InFailedSQLTransactionError`, "Application startup failed" naming the backfill while the statement that actually failed had no log line at all. Split in two, with the schema statements now **logging** their failures by name, a boot whose first ALTER is forced to fail logs all 16 of the failures that cascade from it and still reports "Backfilled 1 event(s)" honestly.
- **Three columns beyond the prescribed list, each with a reason that traces to the request.** The Fix line above names `(event_id, source, channel, raw_text, summary, source_url, telegram_message_id, ingested_at)` and predates `killed_reported` existing at all; that column is the cost `C74` accepted in `ce5d994` and the reason this table was built when it was. `extraction_status` is argued two bullets up: without it the count's NULL is unreadable. `reported_at` is kept because a merged report's own report-time is destroyed by the merge and is recoverable from nowhere else — the event keeps the FIRST report's timestamp, and neither row carries the second one's. A table that exists to stop discarding facts about incoming reports should not discard when the report was made. Caveat inherited, not created here: `news_feeds` substitutes `datetime.now()` for a missing or unparseable `pubDate`, so an RSS `reported_at` is only as true as `events.timestamp` already is.
- **What it does not do.** It cannot recover the 19,027 reports already destroyed; their text is gone. It is half of Phase 4 line one — N reports now link to one event, but two events still cannot be linked as reporting the same thing, which is what `cluster_id` / `corroboration_link` are for. And the one-second check it makes *possible* is still not *reached*: there is no `GET /events/{id}/reports` and no UI, so `LiveFeed` still prints `17 KILLED` with no text on screen. That clause of `C74` stays open.
- **One disagreement it introduces, named rather than discovered later.** `_fix_null_coords_task` and `_reclassify_vague_locations_task` rewrite `events.killed_reported` and `events.summary` from the row's own `raw_text`. Report #1 holds what the classifier said about that same text *at ingest*. After a re-classification the two can differ. Neither is lying — they are two model runs over one string, and the report row is the older one — but nothing on either row says which is which.


#### `C11` — Reliability boost keys on report_count, not on distinct channels, so one source repeating itself raises confidence

`high` · phase 4 · Ingest

**Where:** dedup.py:108 `new_count = (existing.report_count or 1) + 1`; :132-138 `new_channel_rel = _channel_reliability(new_channel) or 1; current_rel = existing.source_reliability or 1; combined = max(current_rel, new_channel_rel); if new_count >= 3: combined = min(5, combined + 1)`. The channel de-duplication at :112-115 only affects the display string `reporting_channels`; report_count at :108 increments unconditionally, so three merges from the same channel_name trip the >= 3 branch. `get_reliability` (seed_channels.py:405-408) returns None for any channel outside CHANNEL_REGISTRY, which `or 1` turns into the lowest score, so an unknown channel still counts toward the threshold.

**Impact:** Corroboration is measured by counting merge events, and the pipeline generates merge events by itself: the RSS poller re-merges the same article after any restart (see also_found), and a restarted Telegram backfill re-merges the tail. Three self-merges of one article raise source_reliability by one and can push it to 5, which is what the min_reliability filter on GET /events and the UI treat as best-sourced. With no model of channel copying, 15 Telegram channels reposting one another's identical text reads as 15 independent confirmations.

**Fix:** Count distinct sources, not merges: derive the boost from the child-report table of C10 (`count(distinct channel)`), and gate it on a channel-family graph so channels known to repost each other contribute once. Until that exists, change dedup.py:135 to test the length of the de-duplicated channel set rather than new_count.

**Narrowed in *this commit*, and repriced.** The gate is now
`count(distinct channel) >= 3` over the event's `event_reports` rows, excluding the empty channel
name (`telegram.py` writes `""` when a chat has neither username nor title, and two unnamed channels
cannot be shown to be distinct from each other). Driven against Postgres: three merges from one
channel take `report_count` to 3 — which is exactly where the old gate fired — and leave
`source_reliability` untouched; two further merges from two new channels take the distinct count to
3 and the boost fires.

- **The base is deliberately NOT recomputed from the report rows**, although that would be less
  code. `_channel_reliability` is `seed_channels.get_reliability`, which knows the Telegram registry
  only and returns `None` — hence `1` — for every RSS source name. Recomputing would collapse a feed
  that `news_feeds` scored 4 at insert down to 1 on its first merge. `max(current, incoming)` stays.
- **Scores either side of this commit are not comparable, and history is not being recomputed.** The
  boost is not invertible (`min(5, x+1)` cannot distinguish a boosted 4 from a native 5) and the
  historical channel set would have to come from `reporting_channels`, which `C78` shows is
  under-counted — so a recomputation would fabricate. `min_reliability` on `GET /events` therefore
  mixes two rules until pre-cutover rows age out. The marker for "this score can be defended" is
  free and already stored: `report_count - count(reports) = 0` means every report this row ever had
  is in the table.
- **The gate is a FLOOR, bounded by what the table holds, and that suppresses FUTURE boosts on
  pre-cutover rows too.** This is a different claim from the one above, and both are true. A
  backfilled historical event has exactly ONE report row however many channels reported it, so an
  archive row whose `reporting_channels` names four counts as one channel here — and because the
  gate needs three distinct channels among rows in *this* table, that row climbs one genuine new
  report at a time and can only reach the gate once `report_count - count(reports)` has come down
  to 0. The **141 archive rows (2.1%)** that genuinely name three or more channels lose a boost
  they should keep. Re-run against Postgres on an archive-shaped row (`report_count=5`, four named
  channels, one backfilled report row): a fifth report from a genuinely new channel gives
  `report_count=6`, `distinct_report_channels=2`, no boost, `source_reliability` unchanged at 2 —
  where the old `new_count >= 3` gate would have fired. Taking the higher of the two counts would
  restore those 141 and is refused for the same reason history is not recomputed:
  `reporting_channels` is a display string whose append is a substring test (`C78`), so deriving a
  corroboration claim from it is fabrication with extra steps. An under-claim a reader can see
  stated is the honest failure of the two — which is why it is stated here and at the gate in
  `dedup.py`.
- **How much it reprices:** the old gate was `report_count >= 3`, so the rows that ever fired it
  are the **2,794** with `report_count >= 3` — not all 6,848 merged rows, because a row that stopped
  at 2 never boosted. Of those 2,794 only **138 (4.9%)** name three or more distinct channels, so
  **2,656 boosts — 95.1%** of every boost this system has awarded would not qualify under the new
  gate. (Across all 6,848 merged rows, 5,174 (75.6%) name one channel and 141 name three or more;
  three of those never reached `report_count` 3.) An earlier draft of this row said 97.9%, dividing
  141 by 6,848 — the wrong denominator, since it counted rows that never fired the old gate.
- **Why it does not close.** The `rc=214` row's channels are `x_osintwarfare, OSINTWarfare` — one
  outlet on two transports, which distinct-channel counting scores as two independent sources. Same
  for `x_osint613`/`osint613` and `x_geoconfirmed`/`GeoConfirmed`. Only the `fwd_from` channel-family
  graph can see that, and it is out of scope here.


#### `C6` — Country names resolve to national centroids via Nominatim and are marked is_geolocated=true

`high` · phase 1 · Ingest

**Where:** KNOWN_LOCATIONS has no 'united states'/'iran'/'israel' key (checked live, all return None), so geocode() falls through to step 5, geocoder.py:663 `result = await _query_nominatim(name)` with `limit=1, bounded=0`. Live from the running backend: geocode('United States') -> (39.7837304, -100.445882) — a field in Kansas; geocode('Iran') -> (32.6475, 54.5644) — the Dasht-e Kavir; geocode('Israel') -> (30.8124, 34.8595) — the Negev. The table also holds country-centroid entries directly: geocoder.py:477 `"oman": (22.0000, 57.0000)`. geocode() returns a bare (lat, lon) tuple with no precision or confidence, and telegram.py:268-270 / news_feeds.py:337-339 set `is_geolocated = True` on any non-None return.

**Impact:** A country-level extraction and a facility-level extraction are stored identically: same two floats, same is_geolocated=true, same marker. 'US strikes' with no named target plots as a point in Kansas and is then eligible for the 50 km dedup radius and drawn at full confidence. is_geolocated cannot mean what Phase 1 needs it to mean until the geocoder reports how precise the hit was.

**Fix:** Have geocode() return (lat, lon, precision) where precision is one of facility/district/city/region/country, sourced from which table branch matched (geocoder.py:632-665) and from Nominatim's `type`/`class`/`addresstype`. Persist it as a column and set is_geolocated only for city-or-better; render country-precision hits as an area, not a point.

**Half of that shipped in `89c6f54`** and this block's "Where" is stale on it: geocode() now returns `GeoResult(lat, lon, precision, uncertainty_m, method)`, and the tier is persisted as `geo_precision` / `geo_uncertainty_m`. The gate did not ship — telegram.py and news_feeds.py still set `is_geolocated = True` on any non-None return, so a country centroid is still stored as a confident location. The finding stays open on that clause.


#### `C60` — No auth on any route or the WS; CORS reflects any origin with credentials, DELETE allowed

`high` · phase unscheduled · Platform

**Where:** backend/app/main.py:136-142 `allow_origins=["*"], allow_credentials=True, allow_methods=["*"]`. No route declares a security dependency — `curl /openapi.json` lists 22 operations, all with no `security` block. Runtime preflight against the running stack: $ curl -i -X OPTIONS http://localhost:8000/events/admin/purge-old -H 'Origin: https://attacker.example' -H 'Access-Control-Request-Method: DELETE' HTTP/1.1 200 OK access-control-allow-methods: DELETE, GET, HEAD, OPTIONS, PATCH, POST, PUT access-control-allow-credentials: true access-control-allow-origin: https://attacker.example backend/app/routes/ws.py:12-14 `@router.websocket("/ws/events")` calls `broadcaster.connect(ws)` with no origin or token check (CORS does not apply to WebSockets at all).

**Impact:** Starlette does not send a literal `*` on preflight — it reflects the requesting Origin, so the wildcard+credentials combination is not the harmless browser-blocked case. Any web page the operator visits can run `fetch('http://localhost:8000/events/admin/purge-old',{method:'DELETE'})` and delete the entire event archive, or open `new WebSocket('ws://localhost:8000/ws/events')` and stream the live feed. Verified reachable from the host with no credential of any kind.

**Fix:** Set `allow_origins=["http://localhost:5173"]` (and drop `allow_credentials` unless a cookie is actually used), and put a shared-secret `Depends` on the `/events/admin/*` router — see C62.


#### `C62` — All seven /events/admin/* endpoints, including both DELETEs, are unauthenticated

`high` · phase unscheduled · Platform

**Where:** backend/app/routes/events.py: POST /admin/fix-null-coords:140, POST /admin/reclassify-locations:150, POST /admin/backfill:229, DELETE /admin/purge-old:247, DELETE /admin/dedup:267, GET /admin/geo-stats:288, POST /admin/import-osint-dataset:577. Every signature takes only `BackgroundTasks` or `session: AsyncSession = Depends(get_session)` — no auth dependency. Exercised against the running stack: $ curl -o /dev/null -w '%{http_code}' http://localhost:8000/events/admin/geo-stats 200 {"total":8244,"geolocated":8244,"unknown_location":0,"geo_rate":"100.0%"} (I did not fire the destructive ones; the route table and the CORS preflight in C60 show they are equally reachable.) `purge_old_events` at :255-261 runs `delete(Event).where(Event.timestamp < cutoff)` with a caller-supplied `before` query…

**Impact:** Anyone who can reach port 8000 — and, via the origin-reflecting preflight in C60, any web page the operator has open — can wipe the archive, or start four unbounded background sweeps that hammer Nominatim and the Anthropic API.

**Fix:** Move the admin routes onto their own `APIRouter(dependencies=[Depends(require_admin_token)])` checking a header against a new `settings.admin_token`, and refuse to start if the token is empty while the port is published.


#### `C63` — Alembic has no versions/; schema comes from create_all plus a hand-kept ALTER list that already crashed startup

`high` · phase 1 · Platform

**Where:** `find backend/alembic -type f` returns exactly two files: env.py and script.py.mako. No versions/ directory, no revision has ever been generated, and `alembic` is in requirements.txt:8 while README.md:129 advertises it as part of the stack. Schema actually comes from backend/app/main.py:34-64: `create_all` then a literal 8-entry list of `ALTER TABLE events ADD COLUMN IF NOT EXISTS ...` each wrapped in `except Exception: pass`. This is not theoretical — it already took the app down. From `docker logs conflict-monitor-backend-1`: WARNING: WatchFiles detected changes in 'app/models.py'. Reloading... INFO:conflict-monitor:Database tables ready INFO:app.services.demo:Seeding 300 historical demo events... asyncpg.exceptions.UndefinedColumnError: column "extraction_status" of relation "events" do…

**Impact:** The ORM is no longer the source of truth for the live schema, the divergence is invisible until an INSERT fails, and the failure mode is a hard `Application startup failed. Exiting.` rather than a degraded boot. Every phase-1 schema change (extraction_status vocabulary, nullable geometry, dropping the severity default) has to pass through this same hand-maintained list.

**Fix:** `alembic revision --autogenerate` against the current live schema as a baseline, delete the migration list from lifespan, and run `alembic upgrade head` as the container entrypoint. Until then, at minimum give each ALTER its own `engine.begin()` and log the swallowed exception instead of `pass`.


#### `C66` — Zero tests and no CI anywhere in the repo or the working tree

`medium` (was `high`) · phase unscheduled · Platform · **narrowed in *this commit***

**Where (as found):** `git ls-files | grep -iE 'test|spec|\.github|ci\.|workflow'` returns nothing. A filesystem `find` for `*test*`, `*spec*`, `conftest.py`, `pytest.ini` outside node_modules matches only Cesium's own shipped assets under frontend/dist (`transferTypedArrayTest.js`, `CesiumInspector.css`). No `.github/` directory exists. backend/requirements.txt has 12 entries and none is pytest, httpx-test, or any test runner. frontend/package.json declares three scripts — dev, build, preview — and no test script or test dependency.

**Impact (as found):** There is no automated check that would have caught C63's startup crash, the schema drift, or any regression in the classifier and geocoder rubrics the v3 rebuild imported. Every phase of the roadmap changes semantics the codebase currently has no way to assert — phase 1 in particular is a schema and default-value change with no regression net under it.

**What shipped:** `backend/tests/` — 103 tests, **8.35s**, run with two lines documented in the README:

```
docker compose up -d db
docker compose run --rm -T --no-deps -e LLM_BACKEND=none backend pytest
```

Every test is a regression test for a defect that actually occurred, and each docstring names the commit it protects (`075ce6f`, `89c6f54`, `e5ad5ae`, `de146f6`, `949aca8`, `ce5d994`, `8523961`). The covered contracts are the ones this project is about: `killed_reported` NULL means "this text stated no count" and 0 means "it said nobody died"; `severity` may be None and nothing substitutes 5; an unresolvable location writes NULL, never a sentinel.

**The green run was itself tested.** A suite nobody has seen fail is a suite that asserts nothing, so each guarded defect was re-introduced into the source and the run repeated: the merge writing `killed_reported` back onto the event, the dropped geometry guard, `reject_boolean` disabled, the geocoder's `\b` anchors removed, `"think": False` deleted, `_is_noise` reading None as low, `_message_already_saved` re-keyed on the id alone, the sentinel `UPDATE` neutered, and `severity: 5` put back in `_build_fallback`. Nine mutations, **18 failures, nine catches, every one by the test that names the defect in its docstring.** The sources were restored from a copy afterwards (no `git checkout`; the run above is against the restored tree).

**Isolation, measured rather than asserted:** the dev database held `events=159, reports=159, checkpoints=0` before the run and after it. The suite opens `conflict_monitor_test` (created and dropped by itself) and the `postgres` maintenance database, never `conflict_monitor`; `tests/conftest.py` rewrites `DATABASE_URL` at import, before any `app.*` module builds its engine, so no invocation can point it elsewhere. The network ban was probed the same way — a throwaway test reaching httpx, Nominatim and `AsyncAnthropic` raised `RuntimeError("test attempted network I/O")` on all three, then was deleted.

**What stays open, and it is the reason this is narrowed rather than closed:**

- **No CI.** There is no remote, so the suite runs when someone types the command and not otherwise. Nothing available here fixes that; the mitigation is that it costs 8 seconds and the command is where the run instructions already live.
- **The suite cannot see schema drift, which is the one thing it would be most valuable for.** The test database's schema comes from `Base.metadata.create_all` on an empty database; production's comes from the archive restore plus the hand-kept ALTER list in `main.py` (`C63`). They match only because someone keeps them matching by hand. The first column added to models.py and not to that list **passes here** — create_all made it — and fails on production's INSERT. A suite that certifies the drift it cannot see is worse than none, so it is written down here and at the top of `tests/conftest.py` rather than left to be discovered. The counter is C66's own original suggestion: build the OLD table shape, run the migration, assert every models.py column exists afterwards. Not written, because that fixture is a second hand-maintained copy of the schema — it is the single most valuable test to add next.
- **Not covered, deliberately:** the FastAPI HTTP layer (including this finding's own `/events` round-trip — nothing in the by-hand work was an HTTP behaviour, so writing it now is speculative); `/admin/dedup` and the `ON DELETE CASCADE` (`C79`); Alembic (`backend/alembic/versions/` is empty — there is no migration to test until `C63` is fixed); the frontend; demo.py; the pollers; the Anthropic retry ladder beyond the branches that return before any I/O.
- **Genuinely untestable here, named rather than stubbed:** whether qwen3 classifies any given message correctly — that is an evaluation against labelled data, not a test — Telethon's connection and channel sweep, Nominatim's answers, and anything about the production archive on the VPS.

**Found while writing it, and it is a real coupling:** `merge_duplicate`'s closing log line reads `report.id` *after* its own `await session.commit()`. That works only because `app/db.py`'s sessionmaker sets `expire_on_commit=False`; a caller constructing a plain `AsyncSession` gets `MissingGreenlet` from a lazy reload in a sync context. The fixture matches production rather than papering over it, with the reason in a comment — but the function depends on a sessionmaker setting it never states.


#### `C69` — demo.py attributes fabricated strikes on real nuclear sites to real named OSINT outlets, on a public MIT repo

`high` · phase unscheduled · Platform

**Where:** backend/app/services/demo.py:170-174 `CHANNELS = ["Aurora Intel", "OSINTdefender", "MidEast Spectator", "Sentdefender", "Intel Slava Z", "Israel Radar", "CIG", "MilitaryOSINT", "IranIntl", "QudsAlert", "WarMonitor"]`. demo.py:294 assigns `"channel_name": _pick(CHANNELS)` to a summary built from demo.py:100-131 against the real coordinates at demo.py:33-94 — Natanz, Fordow, Parchin, Bushehr, Dimona, Nevatim AFB, Al-Asad Airbase. Live output from the running instance: $ curl 'http://localhost:8000/events?limit=2' {"channel_name":"Sentdefender","summary":"Naval assets repositioning near Chabahar","severity":7,...} {"channel_name":"QudsAlert","summary":"UN envoy arrives in Baghdad for emergency mediation talks","severity":8,...} frontend/src/components/LiveFeed.tsx:268 renders `{" "}&middot; {…

**Impact:** Anyone who runs the documented zero-config quickstart gets a screen of invented strike reports on real nuclear facilities, byline-attributed to identifiable real accounts, with no per-row marking (see C71). A screenshot is indistinguishable from real reporting and is defamatory toward the named outlets.

**Fix:** Replace the real handles in CHANNELS with obviously fictional ones (`DEMO Channel Alpha`, …) — a one-line change that costs the demo nothing.


### Surfaced during the audit, not yet triaged

Found while verifying the list above; no phase assigned yet.

Eight of these have since been closed by later commits — re-checked against `de146f6`, not counted
from memory. They are left in place rather than deleted: `clamp_severity` as dead code and the
sentinel-blocked coordinate backfill (both gone with `89c6f54`); the Phase 0 tags never reaching the
display (`e60c44d` put them on the type and `LiveFeed` now renders NOT GEOLOCATED and CLASSIFY
FAILED); the TLE fetcher's missing persistence and inverted backoff (`0de11f2`); the OpenSky
fallback's pre-fix AO (`74f09f0` — both paths derive the box from `CENTRE_LAT` / `CENTRE_LON` /
`RADIUS_NM`); the escalation gauge's mis-filled arc (`e60c44d` deleted `EscalationGauge.tsx`
outright — the same deletion this document already cites for `C43`; the file does not exist at
`de146f6`); the jamming liveness fields fetched and thrown away (`e60c44d` — `IndicatorRail.tsx`
renders `cells_evaluated` and `aircraft_evaluable` as the row's coverage clause and carries the feed
age, so the denominator and the currency both reach the page); and RSS articles re-merging into
themselves after a restart (`de146f6` moved the stored-`source_url` check ahead of both
`classify_message` and `check_duplicate`; in `0bd4835` it sat at `:363`, *after* the semantic dedup
at `:348`, which is what let a re-seen article inflate another row's `report_count`).

A ninth is narrowed but not closed: `89c6f54` rewrote `/events/admin/geo-stats` onto a single
definition (`geometry IS NOT NULL`), so the `lat != -25.0` predicate that finding cites is gone, but
the two endpoints have not been re-measured against the same rows. The rest have **not** been
re-checked against `de146f6`.


**Ingest**

- CRITICAL — cross-channel Telegram message_id collision silently drops history. telegram.py:214-218 `_message_already_saved` queries `select(Event.id).where(Event.telegram_message_id == message_id)` with no channel_name filter, while _get_checkpoint (:187) is correctly per-channel. Telegram message ids are per-chat and start near 1, so acr…
  **Closed in *this commit*** — the guard reads `event_reports` and keys on `(channel, telegram_message_id)`, with a matching partial UNIQUE index so it is a database guarantee and not only a check-then-act. It was right about the mechanism and this audit could not price it: see the `C74` detail block and the Corrections log for the ~2,248-message expectation and for why ingest volume is not comparable across the change.
- HIGH — RSS articles re-merge into themselves after every restart, inflating corroboration. news_feeds.py:424 gates on `_already_seen(art_url)`, which is backed by the in-memory `_seen_hashes: set[str]` at :237 and is empty on every process start. In _process_article the semantic dedup runs first (:348 check_duplicate, :356 merge_duplicate…
- MEDIUM — ClassifierResult.clamp_severity (classifier.py:190-193) is dead code. `Field(default=5, ge=1, le=10)` at :179 enforces the range in Pydantic v2 before the mode='after' validator runs. Verified live in the container: ClassifierResult(severity=15), (severity=0) and (severity=-3) all raise ValidationError; only in-range values reach…
- MEDIUM — the sentinel makes merge_duplicate's coordinate backfill unreachable. dedup.py:122 `if existing.lat is None and new_lat is not None:` is the only path that repairs an event's position, but telegram.py:272 and news_feeds.py:341 guarantee that an ungeocoded event has lat = -25.0, never None. So an event parked in the Indian Ocean c…
- MEDIUM — the Phase 0 tags never reach the display. grep for `extraction_status` and `is_geolocated` across frontend/src returns zero hits, and MapPanel.tsx:110 filters markers with `events.filter((e) => e.lat != null && e.lon != null)` only. Every sentinel-parked event therefore renders as an ordinary marker at (-25, 80) in the Indian Oce…
- LOW — two endpoints give two different answers for 'how much is geolocated'. /events/admin/geo-stats (events.py:288-308) defines geolocated as `lat IS NOT NULL AND lat != -25.0`, ignoring lon and ignoring the is_geolocated column entirely; /events/stats/extraction (:311-345) groups by is_geolocated. Live on the same 8246 demo rows, geo-st…
- LOW — the retry loop backs off only for RateLimitError. classifier.py:303-306 sleeps 2**(attempt+1); the parse_failed (:308), APIStatusError (:313) and generic (:318) branches loop immediately with no delay, so a 529 'overloaded' reply produces three back-to-back calls in milliseconds before falling back. Unscheduled.
- NOTE — the 9fd3fbf commit message is stale on one point. Under 'Known gaps' it states 'No distinguishable parse_failed path exists; JSON/validation errors retry and fall through to the same llm_failed fallback', but the same commit's diff of classifier.py splits `except (json.JSONDecodeError, Exception)` into four branches including `stat…

- MEDIUM, and **measured 2026-09-21 rather than inferred** — the FIRMS ingester throws away a satellite fix and then geocodes the string it printed. 640 archive events (230 distinct strings) carry a `location_name` that is a literal coordinate, e.g. `(35.683°N, 43.879°E)`, on rows whose `raw_text` already reads `lat=35.6830, lon=43.8791`. The pipeline hands that string to the geocoder instead of using the numbers it was given. Median displacement is only 0.1 km, so it mostly survives — but 39 strings land 10-60 km from a coordinate that was already in hand, worst case 60.1 km. This is an ingester bug, not a gazetteer gap: no table entry can ever touch it, which is why it is recorded here and was not added to the 2026-09-21 batch. I measured the whole population rather than reporting the first 13 km sample I saw, which was not typical.
- LOW — `tools/geocoder_vs_archive.py` now describes a geocoder that no longer runs. Its `resolve()` defaults to SUBSTRING matching and a five-item `REJECT` tuple; word boundaries shipped in `075ce6f` (`_KNOWN_PATTERNS`) and `REJECT` was replaced by the 22-entry `_NOT_A_PLACE`. Its default output is therefore the pre-fix geocoder and its "EFFECT OF WORD BOUNDARIES" section is a historical record, not a proposal — which is why `tools/gazetteer_gaps.py` was written beside it rather than its numbers being read. The delta is small (38,314 vs 38,319 rejected, 13,367 vs 13,424 to Nominatim) but the file should either flip its `anchored` default or say in its docstring what it is. Flagged, not touched.
- LOW — two stale facts in `geocoder.py` that *this commit* did not create and did not fix. `KNOWN_LOCATIONS` has 395 key lines and 392 unique keys: `"tehran"`, `"isfahan"` and `"suez canal"` are each defined **twice** (the earlier note here said tehran alone, which was an incomplete measurement, not a wrong one). All three pairs are coordinate-identical, so the duplicates are dead rather than wrong — a later key silently wins in a dict literal, and here it wins with the same value. And `_build_coord_precision`'s docstring says "385 entries" where `_AREA_KEYS`' says 392; they disagreed before this commit too. The third item on this list — the `airport` branch of `_FACILITY_RE` claiming ±500 m for keys Nominatim measures far wider (Ben Gurion's half-diagonal is 3,265 m, 6.5x) — was fixed for that key in *this commit* via `_KEY_UNCERTAINTY_M`, because the same diff argues two screens earlier that where a table hit replaces a measured bound the estimate must err wide. The tier itself is unchanged: widening `facility` would move ~8 other airport keys, and every other facility key in the table, on one measurement.

**Collection**

- velocity has the identical units defect and is not on the list: adsb.lol `gs` is knots (opensky.py:143), OpenSky s[9] is m/s (opensky.py:177), and demo.py:512 converts explicitly to m/s (`round(self.speed_kts * 0.5144)`) - three conventions in one field. The UI labels it "kts" at MapPanel.tsx:508 and "m/s" at CesiumView.tsx:475. Same fix…
- altitude can be the string "ground": opensky.py:142 `a.get("alt_geom") or a.get("alt_baro")` - measured 18 of 145 live adsb.lol records have alt_geom absent and alt_baro == "ground", so a str lands in a field typed `number | null`. The `or` also discards a legitimate 0 (sea-level altitude, and heading 0 at :144, which falls through to tru…
- the OpenSky fallback still uses the pre-fix AO: opensky.py:160 `bbox = {"lamin": 15, "lamax": 45, "lomin": 25, "lomax": 65}` while the primary is now a 650 nm circle around (29.5, 45.5). On failover the fleet, the track history and the jamming grid silently switch to a several-times-larger area with no marker - the same defect C28 just fi…
- "ok" currently means "at least one cell qualified", not "the AO was measured": in the live run 138 states produced cells_evaluated=1 and aircraft_evaluable=120, i.e. one 0.8-degree cell out of ~40 occupied ones reached MIN_CELL_AIRCRAFT=10 (opensky.py:89), yet the status reads ok and the legend shows a normal GPS INTERFERENCE row. There i…
- /tracking/aircraft, /tracking/vessels and /tracking/tle all return bare arrays with no as_of and no coverage (routes/tracking.py:11-39); maritime.py:134 maintains `_cache["last_update"]` that nothing ever reads. Header.tsx:127 renders "0 SAT" for a dead CelesTrak feed exactly as it would for an empty sky, and "0 VES" likewise - the status…
- jamming zone geometry is not honest: opensky.py:99 `radius_km = grid_size * 111 / 2` = 44.4 km treats a 0.8-degree cell as a circle, although at lat 30 the cell is ~89 km tall and ~77 km wide; MapPanel.tsx:324-343 then draws it as a rotated DOM square of `Math.max(30, radius_km/2)` = 30 CSS pixels, a size with no relation to ground area a…
- demo mode conceals exactly the two collection failures that matter: demo.py:679-698 fabricates vessels in the Persian Gulf (live /tracking/vessels shows STENA IMPERO at 26.80/52.79) where AISStream has zero coverage (C30), and demo.py:535-556 fabricates a full aircraft fleet with nac_p=10/nic=8 on every aircraft. The only real feed left i…
- maritime.py:102-104 returns from _run_websocket on any server `error` frame and start_maritime_poller (:56-64) reconnects every 10s forever with no backoff and no surfaced state, so an invalid API key or a rejected bounding box becomes a silent 6-attempts-per-minute loop visible only in the log.
- TLE epoch age is never checked or displayed: CesiumView.tsx:611-620 and GlobeView.tsx:341-347 feed whatever line1/line2 arrive straight into twoline2satrec/propagate. Once C20 is fixed with on-disk persistence, a month-old element set will propagate silently with kilometres of drift and no currency indicator - the satellite layer's versio…

**Frontend**

- ESCALATION GAUGE ARC IS SYSTEMATICALLY MIS-FILLED (high). EscalationGauge.tsx:56 computes `circumference = Math.PI * radius` with radius=70, but the path at :76/:84 is `M 10 90 A 70 70 0 0 1 170 90` — a 160-unit chord, which exceeds 2r=140, so the SVG UA scales the radii up to 80 per the out-of-range-radii rule. Measured in the live page:…
- CESIUM TERRAIN VIEW RENDERS NO BASEMAP AT ALL IN THE DOCUMENTED CONFIG (high). With VITE_CESIUM_ION_TOKEN empty (confirmed `ION=[]` in the running container), CesiumView never sets Ion.defaultAccessToken (CesiumView.tsx:218-221) and never sets terrain (:263-265), and the Viewer's default Ion world imagery cannot load. Screenshot of localh…
- JAMMING LIVENESS DATA IS FETCHED AND THROWN AWAY (medium). useTracking.ts:107-109 parses `as_of`, `cells_evaluated` and `aircraft_evaluable` from /tracking/jamming into state; grep shows they are never read anywhere in src/. Only `jammingStatus.status` is consumed (MapPanel.tsx:619/635/647). So the currency and denominator that 64b690a ad…
- ZERO ACCESSIBILITY AFFORDANCES IN THE ENTIRE FRONTEND (medium). `grep -rn "aria-|role=|<label|tabIndex" src/` returns no matches at all. The play/pause control's accessible name is the glyph "▶" (TimelineScrubber.tsx:228); the sound toggle relies on `title` (LiveFeed.tsx:156); map markers are plain divs with an onClick and a `title` (MapP…
- TIMELINE PLAYBACK EFFECT RE-CREATES ITS INTERVAL ON EVERY TICK (low-medium). TimelineScrubber.tsx:92 lists `winStart, winEnd, activeRange` in the deps of the effect that owns the interval, and the interval body calls `onRangeChange` on every tick, which changes all three. Instrumenting setInterval/clearInterval in the live page showed the…
- DEAD LOCAL IN PLAYBACK LOOP (low). TimelineScrubber.tsx:73 `const windowSize = winEnd - winStart;` is computed every tick and never used. It does not fail the build because tsconfig.json sets `noUnusedLocals: false`. Mentioning, not proposing a deletion. unscheduled.
- NEW-EVENT PING ANIMATIONS CAN BE CUT SHORT (low). MapPanel.tsx:120-133: when a second batch of events arrives within the 2 s window, `setNewEventIds(fresh)` replaces the whole set, so markers from the first batch lose their `isNew` flag mid-animation and their radar ping disappears part-way through. Cosmetic, but it means the ping is not…

**Platform**

- Startup aborts instead of degrading on model/migration drift, and it already happened. backend/app/main.py:36 `create_all` cannot add columns to an existing table, so the only mechanism is the hand-written list at main.py:38-47. The container log holds the live crash: `WatchFiles detected changes in 'app/models.py'. Reloading...` -> `Data…
- The `except Exception: pass` around the ALTER loop (main.py:48-55) is false safety: all eight ALTERs share one `engine.begin()` transaction, and in Postgres the first failure aborts the whole transaction, so every later ALTER is silently ignored and the eventual commit is a rollback. Proven against the live db container: BEGIN; CREATE TEM…
- Schema drift is already present in the live database. `psql \d events` shows a `reliability | integer | default 3` column that exists in no model and in no migration list — leftover from an earlier lineage. `location_name` is `character varying(500) DEFAULT 'Unknown'` in the DB against `mapped_column(Text, default="")` at models.py:35. `e…
- Every backend secret is injected into the browser-facing frontend container. docker-compose.yml gives the frontend service `env_file: .env`, so `docker exec conflict-monitor-frontend-1 env` returns ANTHROPIC_API_KEY, TELEGRAM_API_HASH, AISSTREAM_API_KEY, OPENSKY_PASSWORD and POSTGRES_PASSWORD (verified, values redacted here). Only `VITE_`…
- Blocking HTTP inside the async event loop. backend/app/routes/events.py:471 `with urllib.request.urlopen(_OSINT_WAVES_URL, timeout=30) as resp:` sits inside `async def _import_osint_waves_task()` with no `asyncio.to_thread`. For up to 30 seconds it freezes the single event loop — WebSocket broadcasts, every HTTP request, the demo generato…
- TLE fetcher has no persistence and, worse than no backoff, an inverted one. backend/app/services/satellites.py:56-59: the `except Exception` handler logs and then falls into the same `await asyncio.sleep(REFRESH_INTERVAL)` as the success path, where REFRESH_INTERVAL is 6*3600. A single failed fetch at startup means zero satellites for six…
- The frontend bind mount covers only `./frontend/src:/app/src`. vite.config.ts, index.html, package.json and public/ are baked into the image, so host edits to any of them have no effect until `docker compose build --no-cache frontend` — which is a trap for the C68 fix, since editing vite.config.ts to add `usePolling` will itself appear to…
- There is no production serving path. frontend/Dockerfile:11 runs `npm run dev` (Vite dev server, host 0.0.0.0) and backend/Dockerfile:14 runs `uvicorn --reload`. Both are development servers; `frontend/dist` is built but nothing serves it. Fine for the current stage — worth naming so it is a decision rather than an oversight.
- No concurrency guard on the admin sweeps. POST /events/admin/reclassify-locations (events.py:150) and /admin/fix-null-coords (events.py:140) each start an unbounded full-table pass with `await asyncio.sleep(1.2)` per row for Nominatim's rate limit. Calling either twice starts two overlapping passes that together exceed 1 req/sec and will…
- The code's default classifier model is not the one that runs. `config.py:19` reads `ollama_model: str = "qwen3:8b"` while `.env:20` sets `OLLAMA_MODEL=qwen3.8-27b:latest`, so anyone reading the config to learn which model classifies gets the wrong answer, and a backend started outside `docker compose` (which fails fast on a missing `env_file`) classifies with a different model than every number recorded on 2026-09-20. Mitigated, not fixed, by `extraction_model` being persisted on the row. Surfaced while measuring the fallback rate; unscheduled.

---

## Roadmap

Phases are ordered by dependency, not by appeal. Later phases are unfittable on data the earlier ones
produce, so the order matters.

### Phase 0 — Measurement · one run outstanding

Make failure visible before changing any behaviour.

- [x] Tag the classifier failure path — `extraction_status` on every return path (`9fd3fbf`)
- [x] Persist `is_geolocated`, which the code already computed and discarded on a log line (`9fd3fbf`)
- [x] `GET /events/stats/extraction` so the fallback rate is watchable without SQL (`9fd3fbf`)
- [x] Coverage probes for ADS-B and AIS — both changed the plan (see Measured facts)
- ~~Run for one week and record the real fallback rate~~ — **this line asked one question that turned
  out to be two, and they need different instruments. Split, not ticked:**
- [x] **The model-failure half — measured and bounded, 2026-09-20.** 568 archive messages through the
  real `classify_message()`: **0/568** fallbacks on qwen3:8b and **0/568** on qwen3.8-27b:latest, 95%
  CI 0.00–0.67% each, with a five-branch negative control proving the counter still fires. It is a
  bound, not a point estimate, and it describes **those two models — not Haiku**, which produced the
  archive (see Measured facts)
- [ ] **The time-varying half — not measured, and an archive run structurally cannot see it.**
  Timeouts under load, unreachable bursts, a daemon restart mid-poll and GPU contention exist only in
  wall clock; the archive run classifies 568 rows one at a time on a quiet machine. This is the half
  that still needs a real week. It costs nothing but patience: `GET /events/stats/extraction` has
  existed since `9fd3fbf`, so the number is watchable without SQL. Record the model with it
- [x] **Label the archive's sentinel rows — RUN, 2026-09-20.** `89c6f54` added an idempotent startup
  migration (`main.py:97-108`) that NULLs lat/lon/geometry and sets `is_geolocated = false` wherever
  `lat = -25.0 AND lon = 80.0`. The audit predicted **39,949** rows. The migration logged:

  ```
  INFO:conflict-monitor:  Retired Indian Ocean sentinel on 39949 event(s) - they are now honestly unlocated
  ```

  Predicted and actual agree exactly. Method: the dump was copied from the VPS (md5
  `3da7684f9dc9fbfd210679e02710b37c`, verified identical to the original both before and after),
  restored into a throwaway `sentinel_run` database inside `conflict-monitor-db-1`, and the **real
  backend** was pointed at it with `LLM_BACKEND=none` so the run could not touch the GPU. The VPS
  original was never written to and the dev database was never connected to; the copy was dropped
  afterwards. Archive rows only, before and after:

  | | before | after |
  |---|---|---|
  | rows | 83,938 | 83,938 |
  | on the sentinel | 39,949 | **0** |
  | `geometry IS NOT NULL` | 83,938 (100%) | 43,989 (**52.4%**) |
  | `lat IS NULL` | 0 | 39,949 |

  The 100% "located" figure was never coverage — the old schema had no way to write "I could not
  place this", so every row carried a coordinate whether or not anyone knew where it happened. 52.4%
  is the honest number and the drop is the repair, not a regression.

  Idempotency, claimed in the code comment and now demonstrated rather than asserted: the probe was
  started twice against the same database. Two `Application startup complete` lines, **one** `Retired`
  line. The second pass matched zero rows and said nothing.

  Caveat kept deliberately: this is the run against a *restored copy*. The VPS dump is unchanged, so
  the archive as stored on that box still carries the sentinel. Anyone restoring it gets the
  migration on first backend start, which is the design — but do not read this row as "the file has
  been fixed".

### Phase 1 — Let the schema say "I guessed"

Additive columns first, then remove the lies. Expect the map to get roughly 80% emptier; say so up
front or it reads as a regression.

- [x] `geo_precision` (`facility` / `city` / `admin1` / `country_centroid` / `region_named` / `unresolved`), `geo_uncertainty_m`, `evidence_span` — the two geo columns shipped in `89c6f54`; `evidence_span` shipped in *this commit*, three-valued (a quote / `''` / NULL), computed at **every** writer — including `demo.py` and the OSINT importer, which were found writing NULLs the repair pass then quietly absorbed — and backfilled over every row on disk. NULL is therefore a detector for a writer that forgot it, but only within the window between that write and the next boot: nothing asserts on it, and the pass fills it in. The comment on the column says that rather than more. Deliberately **not** a provenance tag — `geo_method` already records which branch produced the coordinate, and this answers the link upstream of it. See Measured facts
- [x] Kill all four severity-5 defaults, including `Field(default=5)` where an omitted key validates clean — six, in the end, and the dead clamp validator with them (`89c6f54`)
- [x] Delete the `(-25, 80)` sentinel and migrate the query predicates that *read* it (`89c6f54`)
- [x] **Same commit**: guard dedup against NULL geometry, or unlocated rows match on time + type across the whole table — it did land in the same commit (`89c6f54`)
- [x] Word boundaries on the geocoder partial match (simulated: 49 strings change, zero regressions) — and the identical bug in `classifier.py`'s own fallback (`075ce6f`)
- [x] Add the ~15 highest-volume missing facilities — 11 places / 17 keys in *this commit*, picked by event volume off `tools/gazetteer_gaps.py` rather than from intuition, every coordinate resolved through `_query_nominatim` and checked against an anchor already in the table. Four candidates left out because Nominatim returned `[]` for them, and `Taybeh` left out because it resolves to three different villages. **`Ras Laffan` is in this roadmap line by name and ranks #22 by volume, below the cut** — reported rather than promoted to fit the brief. See Measured facts

### Phase 2 — The denominator

Where "a dead feed and a quiet night look identical" actually dies.

- [x] Interference layer: real AO, real test, real denominator, three honest states (`64b690a`)
- [ ] `feed_health` table written from each poller's existing loop; `/health` exposing it
- [ ] Per-tile currency, not per-feed — a live socket delivering the wrong ocean still reads GREEN
- [ ] Concurrent spatial control ring so absence claims work without waiting 28 days for a baseline
- [ ] Trailing robust baselines with an explicit `regime_id`; never pool across the 2023/2026 regime breaks
- [ ] Resolve the maritime coverage gap (see Blocked)

The connectivity layer (`51bdce9`, `bb1c80c`) was not on this roadmap and does not tick any line
above, but it is where the denominator idea is furthest along: a per-sensor `unavailable` state, two
trailing baselines reported separately, and agreement between independent sensors as the confidence
signal. See Measured facts.

### Phase 3 — The display

- [ ] Mark geometry = evidence geometry, driven by `geo_precision`; unresolved rows go to a tray, never the map
- [ ] Show `evidence_span` beside the location — the column shipped in *this commit* and no UI reads it. The display question it raises is not "print the string": a quote and `''` are different states and `''` must not read as a warning about the location, because 19.9% of place-naming archive rows are honestly derived rather than quoted — and because `''` also covers the 38,319 rows that name no place at all, which is a third thing to render and is told apart by `location_name`, not by this column
- [ ] Replace DOM `<Marker>` loops with Source/Layer; the trails code already does this correctly
- [ ] Promote the timeline into a feed-liveness lane so a collection gap and a quiet period are different shapes
- [x] Delete `EscalationGauge`; replace with an indicator rail whose row zero is feed currency (`e60c44d`, finding `C43`)
- [x] Render age, never a green dot — the pulsing LIVE dot is gone; nominal gets no mark at all (`e60c44d`)
- [ ] Shape for class, hue spent once; `prefers-reduced-motion` on every animation
- [ ] Decide the three-renderer question — **answered, then reversed.** Deleting `GlobeView` for a Mapbox globe
      projection was built and reverted: the three.js globe is visually distinct and the owner keeps it. The
      duplication it caused is real, so the resolution is one shared spec (predicate + precision) imported by
      both renderers, not one renderer
- [ ] Link, don't merge — **half shipped in *this commit*, and the box stays unticked for the other
      half.** `event_reports` preserves both reports' text, and `killed_reported` no longer waits on
      anything: the count lives on the report row beside the text that stated it (`C10` closed,
      `C74` narrowed). What is not built is the link *between events* — `cluster_id` and
      `corroboration_link`. This table is parent-to-child only: N reports on one event, never two
      events linked as reporting the same thing
- [ ] Channel family graph from `fwd_from`; collapse corroboration counts over `family_id` —
      **the reason `C11` stays open.** `x_osintwarfare` and `OSINTWarfare` are one outlet on two
      transports and the distinct-channel count of *this commit* scores them as two
- [ ] Stop raising confidence for being copied — the self-merge half shipped in *this commit* (the
      boost gates on distinct channels, not on `report_count`); the reposting half needs the row above
- [ ] Gold labels: ~350 events + ~300 candidate pairs, stratified; start now, they outlive every pipeline rewrite

---

## Blocked on the owner

| Item | Detail |
|---|---|
| **Anthropic API key disabled** | Returns `400 organization_on_hold` — "This organization has been disabled." This no longer stops classification or the Phase 0 week: `de146f6` made a local Ollama backend the default, with no silent fallback. What stays blocked is measuring **Haiku**, the model that produced the 83,938-event archive, so any comparison against it waits on the key. Appeal at `console.anthropic.com/appeal` or swap the key. |
| **No AIS coverage in the Gulf** | Free terrestrial AIS cannot see the Gulf. Options: IMF PortWatch (free, daily chokepoint counts, 5–12 days stale — a baseline, not a live feed); Global Fishing Watch API (free for research, satellite-derived — probe it); satellite AIS (Spire/ORBCOMM, enterprise pricing). |
| **Secrets in `.env`** | Telegram API id/hash + phone, Mapbox token, AISStream key, OpenSky password, Cloudflare Radar token, Postgres password, and the (already dead) Anthropic key. **Not clean in git history after all**: `c460f5e` committed `.env.bak` with all of them. The amend to `de146f6` removed the file and `.gitignore` now excludes `.env.*`; nothing was pushed. Rotate anyway — the orphaned commit is in this machine's reflog until it expires, and the values were displayed in a terminal session. See the Corrections log. |
| **OpenRouter key in VPS shell history** | `/root/.bash_history` on the Hetzner box contains a plaintext OpenRouter key. Rotate. |
| **Hetzner VPS still billing** | Running with nothing on it but the 193 MB archive. |

---

## Corrections log

This project's central argument is that a system should record when its judgments change. A findings
document that silently edits away its own mistakes would fail its own standard.

| Believed | Actually | How it was caught |
|---|---|---|
| The AO has no ADS-B coverage | It has none *at the configured centre* (southern Iraq). Hormuz has 121 aircraft with full integrity fields | Probing four points instead of one |
| The adsb.lol 403 was my own rate-limiting | adsb.lol rejects the default httpx User-Agent: "too generic; include valid contact info". The live path had **never** worked | Reproducing both UAs against the same URL in the same second |
| CelesTrak's 403 was the same User-Agent bug | It is an IP block for excessive downloads, self-inflicted by repeated restarts. It clears on its own | Testing the contact-bearing UA first — it also 403'd |
| `telegram.py` has two `Event()` construction sites | v3 has one, in `_process_message`; both the live handler and backfill delegate to it. The *old main branch* had two | A reviewer grepped instead of trusting the claim |
| The missing `.dockerignore` breaks the frontend build | It did not. The container built and ran with host `node_modules` present | Actually running `docker compose up --build` |
| The production archive might be contaminated with demo rows | Zero demo rows. `source` is `telegram` or `rss_*` throughout | Running the `GROUP BY source` nobody had run |
| The classifier model id was wrong and might explain the 86% severity-5 | `claude-haiku-4-5-20251001` is correct. The per-source spread in the archive proves the API was working | A dead key produces a uniform 100%, not 60–95% |
| "No distinguishable `parse_failed` path exists" — written in commit `9fd3fbf`'s own message | It does exist. That same commit split the catch-all into `rate_limited` / `parse_failed` / `api_NNN` / `llm_failed`. The commit message is stale about its own diff | The audit read `classifier.py:303-319` instead of trusting the note |
| Four severity-5 defaults | **Six**, plus a seventh probe — and `clamp_severity` is dead code: `Field(ge=1, le=10)` raises before the validator runs, so 11 becomes a parse failure instead of clamping to 10 | Grepping for the literal rather than recalling the list |
| `frontend/dist` is committed | It is untracked; the amend that removed it worked | `git ls-files` |
| The interference fix re-centred the AO | It re-centred the **primary** path only. `opensky.py:160` still requests the old `lamin 15 / lamax 45 / lomin 25 / lomax 65` box on the OpenSky fallback, so the two paths now disagree about where the AO is | The audit compared the two poll functions. **Fixed 2026-09-19 in `74f09f0`**: both paths derive the AO from `CENTRE_LAT`/`CENTRE_LON`/`RADIUS_NM`, and OpenSky results are clipped to the circle |
| Secrets in `.env` are "correctly gitignored and clean in git history" — written in the Blocked-on-the-owner table above | `c460f5e` committed `conflict-monitor/.env.bak`, carrying live Telegram, Mapbox, AISStream, OpenSky, Cloudflare Radar and Postgres credentials plus the dead Anthropic key. `.gitignore` covered `.env`, not `.env.bak`. Amended to `de146f6`, which does not contain the file, and `.env.*` is now ignored; `origin` only ever had `main` at `0f4ad05`, so nothing was pushed. The orphan survives in the local reflog and the values still need rotating | An audit grepped the **tracked** files instead of trusting the sentence — the same mistake, made the same way, as the `frontend/dist` entry above |
| Live classification is blocked until the Anthropic key is restored — stated in three places in this document | It was blocked only on *that* backend. `de146f6` put a local qwen3:8b behind the same byte-identical prompt and produced the first `extraction_status = "ok"` rows this archive has ever held, with no key and no spend. The Phase 0 week can start; it measures qwen3 rather than Haiku, which changes what the number means, not whether it can be taken | Trying the alternative instead of re-reading the blocker. The document had repeated "blocked" for twelve commits |
| This register is "updated in the same commit as the change it describes" — its own rule, line 3 | It was last updated **eight commits ago**, in `3c92466`; `e60c44d`, `e03cee6`, `51bdce9`, `0de11f2`, `bb1c80c`, `075ce6f`, `89c6f54` and `de146f6` all shipped without touching it, and that run of eight is the longest this document has had. Four of the twelve commits since `7ddbe58` created it *did* edit it (`de831ed`, `74f09f0`, `fc0c989`, `3c92466`) — and `fc0c989` exists only to correct stale hashes in it. The drift is real either way: the header claimed coverage through `74f09f0`, eleven fixed findings were still listed Open, the tally still said three criticals when there were none, and two whole subsystems (the connectivity layer, the Ollama backend) had never been mentioned. A register that drifts is a register that cannot be cited | A 47-agent audit re-checked the ledger against the tree and produced file:line evidence for every claim; each claimed fix was then re-verified against `de146f6` before its row moved. **This row's own first draft said "twelve commits"** — the count since the document was *created*, not since it was *updated*, and the same diff carried the disproof. `git log --oneline 7ddbe58..de146f6 -- conflict-monitor/docs/FINDINGS.md` returns four commits, one of them titled "Correct stale commit hashes in FINDINGS.md"; `git rev-list --count 3c92466..de146f6` returns 8. A correction that is itself wrong is the one failure this table cannot afford, so the commands stay in the row. The rule needs enforcing, not restating |
| 39,981 events are pinned to the sentinel, and 1,667 (1.99%) carry a real name that still failed geocoding — two rows of Measured facts, quoted since `7ddbe58` | **39,949** and **1,635** (1.95%). Neither was a miscount. 39,981 came from a *modal-coordinate proxy*: `archive_locations.py` emits one coordinate per `location_name` and `geocoder_vs_archive.py` charges that name's whole event count to the sentinel, which misplaces **712** rows — 372 charged though they sit elsewhere, 340 on the point and never charged — netting to +32. The second row inherited the same proxy through a subtraction across two populations (`39,981 − 38,314`). **The percentage is what shielded the count**: 39,981 and 39,949 both round to 47.6%, so the figure a reader would spot-check was right while the number under it was wrong. A near-cancelling error is the more dangerous kind — it makes a name-level guess look like a row-level census | Counting the migration's own `WHERE lat = -25.0 AND lon = 80.0` row by row, then *replaying the proxy from the same read* so the two could be compared as estimators instead of one being called the other's typo (`tools/archive_sentinel_audit.py`) |
| The classifier path on qwen3 is sound — it had just been measured at 568/568 `ok`, zero fallbacks | Measured on **qwen3:8b**, the one model of the family that masks the defect. Every qwen3 on this host reports the `thinking` capability; with it on the reasoning goes to a separate field, `response` comes back **empty**, and `num_predict` — a budget meant for the answer — is spent on reasoning that is then discarded. `qwen3.8-27b:latest` produced 3/3 `parse_failed`, every one logging `got: ''`. `"think": False` fixes it (`949aca8`). **The measurement that would have caught this had been taken on the model that hides it**, and a second model of the same family was the cheapest test available and had never been run | Changing `OLLAMA_MODEL` and reading the log line instead of trusting the 0.00%. Phase 0 paid for itself here: the rows said `parse_failed` with severity NULL and `extraction_model` naming the model that failed, rather than carrying a fabricated severity 5 — the failure was legible the moment it happened |
| 8.7% of the archive has `report_count > 1`, "up to 6" — a Measured facts row quoted since `7ddbe58` | **8.16%** (6,848 rows), and up to **214**. Neither half survived a direct count of the column. The ceiling was not close: 68 distinct values above 6, and the top row is one `x_osintwarfare` article claiming 214 reports. The rate is the more interesting error — `(6,848 + 480)/83,938 = 8.73%`, and 480 is exactly the number of rows whose `report_count` is **0**, a value no writer in this tree can produce (`models.py` defaults to 1, `dedup.py` only increments). So the published figure almost certainly counted rows that claim *zero* reports as rows with *more than one*, and the two errors pointed the same way. The 480 are themselves a state-1/state-2 confusion already on disk and are recorded rather than repaired, on the precedent `main.py` already set for the severity-5 rows: "that one needs a decision, not a startup migration" | Printing the whole distribution instead of a predicate. `tools/archive_report_counts.py` §1 — the two numbers sit four lines apart in its output, which is why the first run of it caught this |
| Ingest volume and reliability scores are comparable across commits unless something says otherwise — never stated, which is how an assumption survives | **Neither is comparable across *this commit*, in opposite directions.** (1) `_message_already_saved` was keyed on `telegram_message_id` alone while Telegram ids are per-channel, so it has been dropping genuinely-new messages from a second channel — on the order of **2,248 of 49,369, ~4.5%**, by an expectation the archive cannot verify directly because the dropped messages are the rows that are missing. Re-keyed on `(channel, message_id)`, the monitor now ingests them, so a step upward in message volume is this change and not the world. (2) The reliability boost now gates on distinct channels rather than `report_count`: **95.1%** of the 2,794 boosts in the archive (2,656 of them) would not have qualified, so pre- and post-cutover `source_reliability` are two different rules sharing one column, and `min_reliability` on `GET /events` mixes them until old rows age out. History is deliberately **not** recomputed — the boost is not invertible and the historical channel set comes from a string `C78` shows is under-counted, so recomputing would fabricate. And it is not only old scores: the new gate counts what the table holds, so a pre-cutover row's FUTURE boosts are suppressed too: it starts with one report row however many channels reported it, so it needs two further reports from genuinely new channels before it can boost. It does **not** wait for `report_count - count(reports)` to reach 0 — that difference is invariant after cutover, since every later merge increments both sides. It is a fixed marker of how many reports' text was destroyed before this table existed, not a countdown — a separate claim from this one, priced in `C11`. The free marker for "this score can be defended from rows you can read" is `report_count - count(reports) = 0` | Writing the consequence down before shipping it, because the previous two rows of this table are both cases of a number changing quietly. On the dev database the marker already reads what it should: 59 of 159 rows at 0, and 100 rows at 1–4 — RSS articles merged before the table existed, whose text is gone |
| qwen3 returns `location_name = "Unknown"` noticeably more often than Haiku did, and is "now measurable because `extraction_model` exists" — `de146f6`'s commit message, carried into Measured facts | Still unquantified, and **not measurable from the archive at all**. The 2026-08-18 dump has no `extraction_status` and no `extraction_model` column, so its 45.6% cannot be decomposed into Haiku-classified rows and rows where no classification happened and `_build_fallback`'s regex supplied the location. What *was* measured is a different pair: on 568 identical rows, qwen3.8-27b returns `Unknown` at 8.9% against qwen3:8b's 17.5%, intervals non-overlapping. That bears on model fit and **not** on Haiku, so **no winner is declared** — the claim is neither confirmed nor refuted, and settling it needs the key over that same sample. The error worth recording is not the direction of the claim but its status: an impression was written down as a known fact with the word "measurable" attached, and the measurement it named could not be taken | Trying to take it. The tool got as far as needing `extraction_status` on the archive side and found the column absent from the COPY header |

---

## Changelog

| Date | Commit | Change |
|---|---|---|
| 2026-09-18 | `716ffec` | Snapshot of the pre-rebuild working tree on `pre-v3-rebuild-backup` |
| 2026-09-18 | `0bd4835` | Rebuilt from the recovered v3 lineage (extracted from local Docker images) |
| 2026-09-18 | `9fd3fbf` | Phase 0 — tag the classifier failure path and geocode outcome |
| 2026-09-18 | `64b690a` | Fix the GPS interference layer: right AO, right test, real denominator |
| 2026-09-18 | `7ddbe58` | Add `docs/FINDINGS.md` — this register |
| 2026-09-18 | `de831ed` | Add `tools/` (reproduction scripts) and the resume section |
| 2026-09-19 | `74f09f0` | One AO for both aircraft poll paths; OpenSky bbox derived from the constants and clipped to the circle |
| 2026-09-19 | `fc0c989` | Correct three stale commit hashes in this document, and rename the header row to "Covers work through" |
| 2026-09-19 | `3c92466` | Altitude floor (FL200) on the interference denominator — the peaceful control collapses to 2.3% and Kaliningrad holds at 16.4% |
| 2026-09-19 | `e60c44d` | Design pass: computed three-hue palette, `IndicatorRail` replacing `EscalationGauge`, liveness as age instead of dots (`C43`, `C47`) |
| 2026-09-19 | `e03cee6` | Migrate the four stale `EVENT_COLORS` copies onto `tokens.ts`; the app had been showing two colour languages for the same events (half of `C53`) |
| 2026-09-19 | `51bdce9` | Connectivity layer: internet disruption from IODA's four sensors, never averaged, with `unavailable` as a first-class state |
| 2026-09-19 | `0de11f2` | Stop the TLE fetcher renewing its own CelesTrak ban: on-disk cache, fetch skip while fresh, backoff on 403 (`C20`) |
| 2026-09-19 | `bb1c80c` | Connectivity: robust-z per sensor, separate short and long baselines, Cuba as the validation case, Cloudflare Radar as an independent corroborator |
| 2026-09-19 | `075ce6f` | Phase 1a: word-anchored location matching in both the geocoder and the classifier, actors are no longer places, deterministic dedup ordering, keyless installs open on the globe (`C5`, `C7`, `C9`, `C41`) |
| 2026-09-19 | `89c6f54` | Phase 1b: severity nullable, `(-25, 80)` sentinel deleted, dedup's geometry guard in the same commit, `geo_precision` / `geo_uncertainty_m` derived rather than asserted (`C2`, `C4`, `C8`) |
| 2026-09-19 | `de146f6` | Local Ollama classifier backend (qwen3:8b) — classification with no key and no spend, no silent fallback, `extraction_model` on every row; admin re-classify tasks stop overwriting rows on a fallback (`C12`). Amend of `c460f5e`, which had committed `.env.bak` |
| 2026-09-19 | `ab9420c` | Measure the death-toll rate instead of asserting it: `tools/archive_killed_rate.py` over all 83,938 archive events — **95.5%** state no count, replacing an invented "~86%" that had no artifact anywhere behind it |
| 2026-09-19 | `e5ad5ae` | `killed_reported` end to end — a count copied from the message, never graded. `merge_duplicate` stops discarding it; `reject_boolean` stops `true` validating as severity 1 and silently erasing the event (opens `C74`) |
| 2026-09-19 | `e0732ac` | Reconcile this register with the tree it claims to describe: eleven fixed findings were still listed Open, the header was eight commits stale, and two whole subsystems had never been mentioned |
| 2026-09-20 | `fe5d2d4` | Record the live verification of `killed_reported` - migration, classification and the merge policy exercised against a running stack; `C74` given its measured margin |
| 2026-09-20 | `949aca8` | Switch the classifier to **qwen3.8-27b:latest** and send `"think": False` — a thinking model puts its reasoning in a separate field and returns an empty `response`, so every row came back `parse_failed`. qwen3:8b answered anyway, which is why the defect was invisible while only the small model ran |
| 2026-09-20 | `0051932` | Close out Phase 0: the fallback rate measured and bounded on 568 archive messages for **both** models, the sentinel migration audited before it runs, the "one week" line split into the half that is measured and the half only wall clock can reach, and two long-standing register numbers corrected to the estimator that produced them (`C75`) |
| 2026-09-20 | `9539eb2` | Run the sentinel migration against a restored copy of the archive: 39,949 rows retired, exactly as predicted; geolocation 100% -> 52.4%; idempotency demonstrated. Phase 0's last un-run item closed |
| 2026-09-20 | `ce5d994` | `merge_duplicate` stops writing `killed_reported`: the column becomes a pure function of this row's own `raw_text`, and an incoming count is logged as dropped rather than stored. Narrows `C74`, opens `C76` and `C77`; the match is exactly as loose as it was |
| 2026-09-20 | `8523961` | `event_reports`: one row per incoming report, written at both insert sites and inside the merge's own transaction, so no contributing report loses its text, its URL, its message id, its stated death toll or the classifier status that says whether anything ever looked for one — `killed_reported` NULL is three-valued and `extraction_status` on the same row is what tells the three apart. Both dedup guards move onto it — and `_message_already_saved` is re-keyed on `(channel, message_id)`, which it never was. The `IntegrityError` its UNIQUE index raises is caught in `_process_message`, because uncaught it ended the whole channel sweep; the backfill gets its own transaction, and the 16 schema statements around it log their failures instead of passing in silence. The reliability boost gates on distinct channels instead of `report_count`, a count that is a floor bounded by what the table holds. Closes `C10`, narrows `C11` and `C74`, opens `C78` and `C79`. Four Measured-facts numbers corrected or added, with `tools/archive_report_counts.py` behind them |
| 2026-09-21 | `e2a8ee0` | The first tests in this repo: 103 of them, 8.35s, one documented command. Every one is a regression test for a defect that actually occurred and names the commit it protects in its docstring — the merge that moved a death toll between events, the geometry guard, `reject_boolean`, the geocoder's word anchors, `"think": False`, the ingest guard's `(channel, message_id)` key, the sentinel retirement and the first-report backfill. The suite was shown to FAIL before it was believed: nine defects re-introduced into the source, nine caught by the test that names them. The dev database held 159 events before the run and 159 after. One production change, a pure move: the migration block lifted out of `lifespan` into `run_startup_migrations(engine)`, because entering `lifespan` also starts five network pollers and the alternative was to test a COPY of the SQL. Narrows `C66` to medium — what stays open is that there is still no CI, and that a suite whose schema comes from `create_all` cannot see the drift `C63` describes |
| 2026-09-21 | *this commit* | **Phase 1 closed.** `evidence_span`: the words in a row's own `raw_text` that spell its own `location_name`, so a location becomes checkable in one second the way `killed_reported` is. Three values, three statements — a quote, `''` for "looked, and there is nothing here to quote", NULL for "nothing looked" — and a repair pass that empties the NULL set. `''` carries two of those cases and `location_name` is what separates them: a place was named and this text does not spell it (10,131 archive rows), or no place was named at all (38,319). That second case is the one the first implementation got wrong, and got wrong in this project's own bug class: with no notion of `_NOT_A_PLACE` it answered `"unknown"` for a row whose `location_name` is the classifier's could-not-tell sentinel, writing state 3 into the field a reader reads as state 1 — 60 archive rows, and 104 of the live 201 are exposed to it. It now applies the same list `geocode()` applies at its front door, imported and not copied. It is a SEARCH, never an argument: nothing can pass a span in, so a model naming a plausible town that appears nowhere gets `''` and has no fabricated span to offer. Deliberately not a provenance tag (`geo_method` already owns that question) and deliberately not a boolean (the evidence costs one column and cannot be wrong about itself). Measured first, and the measurement changed the design twice: the markdown-boundary and apostrophe-folding rules bought 33 events out of 45,619 and were dropped, and `'İ'.lower()` being two characters made searching a lowered copy a real slicing bug rather than a hypothetical one. A third variant — a name whose first or last character is non-word, which `\b` can never match — is 7 events and is recorded rather than fixed, on the same arithmetic. The headline split is **77.8% / 22.2% over the 45,619 place-naming rows**, and both halves of that sentence are load-bearing: the first replay said 77.5% because it matched against the dump's COPY escape form, where the letter n of ` ` is a word character (134 events), and the column is written on all 83,938 rows, not on the 45,619 the percentage is over. The startup repair pass now walks a cursor on `id`: its only exit used to be `WHERE evidence_span IS NULL`, so termination rested on `evidence_span()` never returning `None`, and removing that property hung pytest instead of failing it. **Gazetteer**: 11 places / 17 keys off the top of `tools/gazetteer_gaps.py`'s volume ranking, eight coordinates resolved through `_query_nominatim` and checked against an anchor already in the table, three reused from an alias already in the table and now saying so with the measured distance that justifies the reuse (0.683 / 0.493 / 2.126 km) instead of a derivation that did not happen — killing errors of 2,011 km (`Al-Khiyam`, in Yemen), 3,096 km (`Karaj`, in Slovakia), 9,124 km (`Galilee`, on Long Island) and 1,010 km (`Anbar`, in Turkey), plus two partial-match hijacks. Four candidates left out because Nominatim answered `[]`, confirmed with four extra requests rather than assumed from four consecutive failures; `Taybeh` left out because it is two villages. 89 strings change across the archive, 472 events, no key capturing a name it did not mean. `Anbar` keeps its 352 km bound but stops claiming to be a country: the tier is the word the UI prints, so a per-key `_KEY_UNCERTAINTY_M` of **measured** extents now carries the number wherever the tier estimate errs narrow (Anbar, and Ben Gurion Airport at 3,265 m against the facility tier's 500 m). 51 new tests (103 → 154), six of them shown to fail against the defect they name — the four from the first pass, plus the sentinel rule and the loop's bound. The live dev database held 200 events before and after; all 200 got a span in one pass and the six reloads after it printed nothing, and a re-read at 201 rows found no sentinel row carrying a quote |