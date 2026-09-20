# Conflict Monitor — Findings & Roadmap

**Living document.** Update it in the same commit as the change it describes. If a finding is fixed,
move it and cite the commit. If a belief turns out wrong, record that in the Corrections log rather
than deleting it.

| | |
|---|---|
| Branch | `v3-rebuild` |
| Covers work through | `e5ad5ae` |
| Last updated | 2026-09-19 |

---

## Picking this up again

**State at the last stopping point — 2026-09-19.**

```
branch  v3-rebuild        HEAD e5ad5ae
        pre-v3-rebuild-backup  716ffec   snapshot of the tree before the rebuild
        main                   0f4ad05   the OLD lineage; superseded, kept for reference
```

`de146f6` is an amend of `c460f5e`, which had committed `.env.bak`. Nothing was pushed — `origin`
carries only `main` at `0f4ad05` — but see the Corrections log: the credentials still need rotating.
`ab9420c` then measured the archive's death-toll rate and `e5ad5ae` landed `killed_reported`; both are
described below, and neither has been exercised against a running stack.

Restart the stack (demo mode, no keys needed):

```bash
cd conflict-monitor
DEMO_MODE=true docker compose up -d
# frontend  http://localhost:5173      backend  http://localhost:8000
```

Two gotchas that will waste your time otherwise:

- **Vite's file watcher does not fire across the Windows bind mount.** Frontend edits appear to do
  nothing until `docker compose restart frontend`. (Finding `C68`; fix is `server.watch.usePolling`.)
- **`docker compose restart backend` kills anything you have `exec`'d into that container**, including
  a long-running probe. Run probes from the host.

### Things that exist outside this repo

| What | Where | Why it matters |
|---|---|---|
| Recovered v3 source | `C:/Users/mtt_j/conflict-monitor-v3-recovered/` | 933 KB, **untracked**. Extracted from the local Docker images `conflict-monitor_v3-backend/-frontend` (built 2026-03-15). Includes 52 files reconstructed from the VPS Claude transcripts and the Aug-18 production schema. If those images are pruned this is the only copy. |
| Production archive | `truthevades:/root/archive/conflict_monitor-20260818.sql.gz` | 193 MB / 83,938 events. The only copy of five months of real ingest. Every statistic in this document comes from it. Not backed up anywhere else. |
| Local v3 database | docker volume `conflict-monitor_v3_pgdata` | Created 2026-03-16, untouched. |

Query the archive without downloading it — see [`../tools/README.md`](../tools/README.md):

```bash
scp tools/archive_source_stats.py truthevades:/tmp/
ssh truthevades 'python3 /tmp/archive_source_stats.py'
```

### Next three actions

1. **Exercise `killed_reported` against a running stack.** It landed in `e5ad5ae` — compiling clean,
   `tsc` at zero, validators run against the real `ClassifierResult` — but nothing in it has seen a
   live session or a database. Specifically unverified: that a real duplicate fills an empty count,
   that a disagreement logs both numbers rather than silently keeping one, and that the startup
   migration adds the column to the existing `conflict-monitor_v3_pgdata` volume. There is no test
   suite to catch any of it (`C66`), so this is read-the-logs work, not a green tick.
2. **Rotate the credentials that were briefly committed.** `.env.bak` went into `c460f5e` and was
   removed by the amend to `de146f6`, which also added `.env.*` to `.gitignore`. Nothing was pushed,
   but the orphaned commit lives in this machine's reflog until it is expired and the values were
   displayed in a terminal session. Telegram, Mapbox, AISStream, OpenSky, Cloudflare Radar, Postgres.
3. **Run the Phase 0 fallback-rate week on qwen3.** No longer blocked and no longer costs anything:
   `llm_backend` defaults to `ollama` (`config.py:14`). It measures qwen3:8b rather than Haiku, so it
   answers "what is this system's fallback rate *now*", not "what produced the 86.3% archive" — a
   reason to name the model in the result, not a reason to keep waiting for a key. The archive can be
   re-classified locally for the same reason: no key, no spend.

### Blocked, and not fixable from the code

- **Anthropic key returns `400 organization_on_hold`.** Those rows tag `api_400` — `classifier.py`
  writes `f"api_{e.status_code}"` — not `llm_failed`. This no longer blocks classification: `de146f6`
  made a local Ollama backend the default, with no silent fallback between the two. What it still
  blocks is measuring **Haiku**, the model that produced the archive. Appeal at
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
| `location_name = "Unknown"` | 45.6%; **96.5% of those are also severity 5** — the fallback signature |
| Pinned to the sentinel `(-25, 80)` | 47.6% (39,981) — open ocean SW of Australia |
| Events with a real name that still failed geocoding | 1,667 (**1.99%** — the true geocode failure rate) |
| Dedup merges | 8.7% of events have `report_count > 1`, up to 6 |
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
| Known, and now measurable | qwen3 returns `location_name = "Unknown"` noticeably more often than Haiku did. A prompt/model-fit question, separable now that the model is on the row |

The "~20 s cold start" is **not** a re-measurement: after an explicit model unload it reloaded from
page cache in 0.53 s, so 20 s stands as a worst case for a genuinely cold file cache, not an
observation.

Source: the acceptance test in `de146f6`, re-runnable against live RSS with `LLM_BACKEND=ollama`.

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

---

## Findings ledger

54 candidate findings, first re-checked against the tree at `64b690a` and re-verified line by line against `de146f6`: **26 open**, 25 fixed, 3 invalid or external. Of the open ones, **none is critical** and 13 are high.

Claims that no longer hold are kept with status `INVALID` rather than deleted.


### Open

Ordered by severity, then area.

| ID | Sev | Phase | Area | Finding |
|---|---|---|---|---|
| `C74` | high | 4 | Ingest | A dedup false positive now stamps one report's death toll onto another event; unlocated rows match on text + time + type with no spatial predicate at all |
| `C31` | high | 2 | Collection | A failed poll leaves the last fleet and a frozen as_of in place with status still "ok" |
| `C44` | high | 3 | Frontend | Timeline playback advances speed*1000 ms per 100 ms tick, and the real rate depends on tab visibility |
| `C45` | high | 3 | Frontend | Events, aircraft and vessels are DOM <Marker> overlays, not Source/Layer — 98 marker nodes measured live |
| `C46` | high | 3 | Frontend | 46 markers carry transition: transform 2s linear against a 15 s aircraft / 10 s vessel poll — fabricated motio… |
| `C51` | high | 2 | Frontend | WebSocket reconnect refetches nothing and the server sends no backlog: events during a drop are lost until rel… |
| `C10` | high | 4 | Ingest | merge_duplicate keeps only channel and severity; the incoming report's text and identity are dropped |
| `C11` | high | 4 | Ingest | Reliability boost keys on report_count, not on distinct channels, so one source repeating itself raises confid… |
| `C6` | high | 1 | Ingest | Country names resolve to national centroids via Nominatim and are marked is_geolocated=true |
| `C60` | high | — | Platform | No auth on any route or the WS; CORS reflects any origin with credentials, DELETE allowed |
| `C62` | high | — | Platform | All seven /events/admin/* endpoints, including both DELETEs, are unauthenticated |
| `C63` | high | 1 | Platform | Alembic has no versions/; schema comes from create_all plus a hand-kept ALTER list that already crashed startu… |
| `C66` | high | — | Platform | Zero tests and no CI anywhere in the repo or the working tree |
| `C69` | high | — | Platform | demo.py attributes fabricated strikes on real nuclear sites to real named OSINT outlets, on a public MIT repo |
| `C29` | medium | 2 | Collection | Second AIS box is mislabelled "Eastern Mediterranean" and supplies 85% of the vessel feed from outside any AO |
| `C32` | medium | 1 | Collection | altitude mixes feet (adsb.lol) and metres (OpenSky) in one field, and the UI labels it both ways |
| `C48` | medium | 3 | Frontend | Event type is encoded by hue alone on map, globe, terrain and timeline; only the feed carries a text label |
| `C49` | medium | 3 | Frontend | No prefers-reduced-motion guard anywhere: 6 keyframe animations, an 8s scan line and an audio blip |
| `C50` | medium | 3 | Frontend | LiveFeed NEW badge compares array lengths against a 200-cap, so it stops firing permanently once the cap is hi… |
| `C52` | medium | — | Frontend | Cesium credits are routed to a detached div, suppressing Ion/Bing/Google attribution required by their terms |
| `C53` | medium | 3 | Frontend | Three renderers (Mapbox, Globe, Cesium) still duplicate mark logic. The palette half is fixed: the four stale EVENT_COLORS copies were migrated onto tokens.ts in `e03cee6` |
| `C61` | medium | — | Platform | Postgres published on 0.0.0.0:5432; backend DATABASE_URL hardcoded so POSTGRES_PASSWORD cannot change it |
| `C68` | medium | — | Platform | Vite HMR is blind across the Windows bind mount; the backend only reloads because watchfiles polls |
| `C70` | medium | 0 | Platform | Demo path bypasses classifier, geocoder, dedup and track_history, so the zero-config run exercises none of the… |
| `C71` | medium | 3 | Platform | Demo rows ARE tagged source='demo' in the DB and API; it is the UI that discards the distinction |
| `C65` | low | — | Platform | Shutdown calls task.cancel() without awaiting, so no client-close path is guaranteed to run |
| `C67` | low | — | Platform | No .dockerignore; frontend COPY . . does overlay host node_modules, but the linux binaries survive |

### Fixed

Verified fixed in the current tree. Each row names the commit that closed it.

| ID | Sev | Phase | Area | Finding |
|---|---|---|---|---|
| `C41` | critical | 0 | Frontend | A keyless install now opens on the globe, which needs no token (`075ce6f`), so the documented quick start no longer renders a black rectangle. Residual: choosing 2D with no token still draws black with no in-panel notice |
| `C5` | critical | 1 | Ingest | Word-anchored patterns, compiled once at import, in **both** places — `_KNOWN_PATTERNS`/`_DIRECTIONAL_PATTERNS` in geocoder.py and `_LOCATION_PATTERNS` in classifier.py (`075ce6f`) |
| `C8` | critical | 1 | Ingest | check_duplicate branches explicitly on geometry: a located event requires `geometry IS NOT NULL` + ST_DWithin, an unlocated one is confined to `geometry IS NULL`. Neither pool can absorb the other (`89c6f54`) |
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

### Detail: open critical and high findings


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


#### `C11` — Reliability boost keys on report_count, not on distinct channels, so one source repeating itself raises confidence

`high` · phase 4 · Ingest

**Where:** dedup.py:108 `new_count = (existing.report_count or 1) + 1`; :132-138 `new_channel_rel = _channel_reliability(new_channel) or 1; current_rel = existing.source_reliability or 1; combined = max(current_rel, new_channel_rel); if new_count >= 3: combined = min(5, combined + 1)`. The channel de-duplication at :112-115 only affects the display string `reporting_channels`; report_count at :108 increments unconditionally, so three merges from the same channel_name trip the >= 3 branch. `get_reliability` (seed_channels.py:405-408) returns None for any channel outside CHANNEL_REGISTRY, which `or 1` turns into the lowest score, so an unknown channel still counts toward the threshold.

**Impact:** Corroboration is measured by counting merge events, and the pipeline generates merge events by itself: the RSS poller re-merges the same article after any restart (see also_found), and a restarted Telegram backfill re-merges the tail. Three self-merges of one article raise source_reliability by one and can push it to 5, which is what the min_reliability filter on GET /events and the UI treat as best-sourced. With no model of channel copying, 15 Telegram channels reposting one another's identical text reads as 15 independent confirmations.

**Fix:** Count distinct sources, not merges: derive the boost from the child-report table of C10 (`count(distinct channel)`), and gate it on a channel-family graph so channels known to repost each other contribute once. Until that exists, change dedup.py:135 to test the length of the de-duplicated channel set rather than new_count.


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

`high` · phase unscheduled · Platform

**Where:** `git ls-files | grep -iE 'test|spec|\.github|ci\.|workflow'` returns nothing. A filesystem `find` for `*test*`, `*spec*`, `conftest.py`, `pytest.ini` outside node_modules matches only Cesium's own shipped assets under frontend/dist (`transferTypedArrayTest.js`, `CesiumInspector.css`). No `.github/` directory exists. backend/requirements.txt has 12 entries and none is pytest, httpx-test, or any test runner. frontend/package.json declares three scripts — dev, build, preview — and no test script or test dependency.

**Impact:** There is no automated check that would have caught C63's startup crash, the schema drift, or any regression in the classifier and geocoder rubrics the v3 rebuild imported. Every phase of the roadmap changes semantics the codebase currently has no way to assert — phase 1 in particular is a schema and default-value change with no regression net under it.

**Fix:** Add pytest + httpx.ASGITransport and start with two tests that pay for themselves immediately: boot the lifespan against a throwaway Postgres and assert every models.py column exists in the live table; and assert `/events` round-trips a row with `extraction_status` set. Then a three-line GitHub Actions workflow.


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
- HIGH — RSS articles re-merge into themselves after every restart, inflating corroboration. news_feeds.py:424 gates on `_already_seen(art_url)`, which is backed by the in-memory `_seen_hashes: set[str]` at :237 and is empty on every process start. In _process_article the semantic dedup runs first (:348 check_duplicate, :356 merge_duplicate…
- MEDIUM — ClassifierResult.clamp_severity (classifier.py:190-193) is dead code. `Field(default=5, ge=1, le=10)` at :179 enforces the range in Pydantic v2 before the mode='after' validator runs. Verified live in the container: ClassifierResult(severity=15), (severity=0) and (severity=-3) all raise ValidationError; only in-range values reach…
- MEDIUM — the sentinel makes merge_duplicate's coordinate backfill unreachable. dedup.py:122 `if existing.lat is None and new_lat is not None:` is the only path that repairs an event's position, but telegram.py:272 and news_feeds.py:341 guarantee that an ungeocoded event has lat = -25.0, never None. So an event parked in the Indian Ocean c…
- MEDIUM — the Phase 0 tags never reach the display. grep for `extraction_status` and `is_geolocated` across frontend/src returns zero hits, and MapPanel.tsx:110 filters markers with `events.filter((e) => e.lat != null && e.lon != null)` only. Every sentinel-parked event therefore renders as an ordinary marker at (-25, 80) in the Indian Oce…
- LOW — two endpoints give two different answers for 'how much is geolocated'. /events/admin/geo-stats (events.py:288-308) defines geolocated as `lat IS NOT NULL AND lat != -25.0`, ignoring lon and ignoring the is_geolocated column entirely; /events/stats/extraction (:311-345) groups by is_geolocated. Live on the same 8246 demo rows, geo-st…
- LOW — the retry loop backs off only for RateLimitError. classifier.py:303-306 sleeps 2**(attempt+1); the parse_failed (:308), APIStatusError (:313) and generic (:318) branches loop immediately with no delay, so a 529 'overloaded' reply produces three back-to-back calls in milliseconds before falling back. Unscheduled.
- NOTE — the 9fd3fbf commit message is stale on one point. Under 'Known gaps' it states 'No distinguishable parse_failed path exists; JSON/validation errors retry and fall through to the same llm_failed fallback', but the same commit's diff of classifier.py splits `except (json.JSONDecodeError, Exception)` into four branches including `stat…

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

---

## Roadmap

Phases are ordered by dependency, not by appeal. Later phases are unfittable on data the earlier ones
produce, so the order matters.

### Phase 0 — Measurement · mostly done

Make failure visible before changing any behaviour.

- [x] Tag the classifier failure path — `extraction_status` on every return path (`9fd3fbf`)
- [x] Persist `is_geolocated`, which the code already computed and discarded on a log line (`9fd3fbf`)
- [x] `GET /events/stats/extraction` so the fallback rate is watchable without SQL (`9fd3fbf`)
- [x] Coverage probes for ADS-B and AIS — both changed the plan (see Measured facts)
- [ ] Run for one week and record the real fallback rate — no longer blocked: `de146f6` made Ollama the default backend, so this runs with no key and no spend. It measures **qwen3:8b, not Haiku**; record which model the number describes
- [ ] Label the archive's sentinel rows with a single `UPDATE` ($0, no reprocessing) — **the code
  shipped, the run did not**: `89c6f54` added an idempotent startup migration (`main.py`, logging
  "Retired Indian Ocean sentinel on %d event(s)") that NULLs lat/lon/geometry and sets
  `is_geolocated = false` wherever `lat = -25.0 AND lon = 80.0`. It runs against whatever database
  the backend starts on; nothing evidences it having run against the 83,938-event VPS archive, which
  is what "the archive" means everywhere else in this document. Outstanding is the run and its
  rowcount, not the SQL

### Phase 1 — Let the schema say "I guessed"

Additive columns first, then remove the lies. Expect the map to get roughly 80% emptier; say so up
front or it reads as a regression.

- [ ] `geo_precision` (`facility` / `city` / `admin1` / `country_centroid` / `region_named` / `unresolved`), `geo_uncertainty_m`, `evidence_span` — **partial**: the two geo columns shipped in `89c6f54`, derived from the matching table branch and from Nominatim's bounding box. `evidence_span` does not exist anywhere in the backend, so this stays unticked
- [x] Kill all four severity-5 defaults, including `Field(default=5)` where an omitted key validates clean — six, in the end, and the dead clamp validator with them (`89c6f54`)
- [x] Delete the `(-25, 80)` sentinel and migrate the query predicates that *read* it (`89c6f54`)
- [x] **Same commit**: guard dedup against NULL geometry, or unlocated rows match on time + type across the whole table — it did land in the same commit (`89c6f54`)
- [x] Word boundaries on the geocoder partial match (simulated: 49 strings change, zero regressions) — and the identical bug in `classifier.py`'s own fallback (`075ce6f`)
- [ ] Add the ~15 highest-volume missing facilities (`Ras Laffan`, `Ben Gurion`, …; `Prince Sultan Air Base` is now in the table)

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
- [ ] Replace DOM `<Marker>` loops with Source/Layer; the trails code already does this correctly
- [ ] Promote the timeline into a feed-liveness lane so a collection gap and a quiet period are different shapes
- [x] Delete `EscalationGauge`; replace with an indicator rail whose row zero is feed currency (`e60c44d`, finding `C43`)
- [x] Render age, never a green dot — the pulsing LIVE dot is gone; nominal gets no mark at all (`e60c44d`)
- [ ] Shape for class, hue spent once; `prefers-reduced-motion` on every animation
- [ ] Decide the three-renderer question (recommendation: delete `GlobeView`, keep Mapbox, demote Cesium to an explicit terrain/LOS action)

### Phase 4 — Corroboration

- [ ] Link, don't merge — `corroboration_link` + `cluster_id`, preserving both reports' text
- [ ] Channel family graph from `fwd_from`; collapse corroboration counts over `family_id`
- [ ] Stop raising confidence for being copied
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
