# Conflict Monitor — Findings & Roadmap

**Living document.** Update it in the same commit as the change it describes. If a finding is fixed,
move it and cite the commit. If a belief turns out wrong, record that in the Corrections log rather
than deleting it.

| | |
|---|---|
| Branch | `v3-rebuild` |
| HEAD at last update | `4e59ede` |
| Last updated | 2026-09-19 |

---

## Picking this up again

**State at the last stopping point — 2026-09-18.**

```
branch  v3-rebuild        HEAD 7ddbe58
        pre-v3-rebuild-backup  716ffec   snapshot of the tree before the rebuild
        main                   0f4ad05   the OLD lineage; superseded, kept for reference
```

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

1. **The three open criticals** — `C5` (geocoder substring match; the word-boundary fix is already
   simulated across all 3,665 archive strings with zero regressions), `C8` (dedup drops its spatial
   predicate on NULL coordinates — this one **blocks Phase 1**, it must land in the same commit as
   the sentinel removal), `C41` (the documented quick start renders a black map).
2. **Phase 0's last step** — run for a week and record the real fallback rate. Blocked until the
   Anthropic key is restored.

### Blocked, and not fixable from the code

- **Anthropic key returns `400 organization_on_hold`.** Live classification cannot run at all. Appeal
  at `console.anthropic.com/appeal` or swap the key. Until then the Phase 0 measurement is stalled and
  live mode produces 100% `llm_failed` rows.
- **AISStream has no Persian Gulf coverage.** No code change fixes this; it needs a different source.
- **CelesTrak IP-blocked this host** for excessive downloads on 2026-09-18 (self-inflicted by repeated
  restarts — the TLE fetcher refetches on every process start, finding `C20`). It clears on its own.

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

---

## Findings ledger

54 candidate findings re-checked against the tree at `64b690a`: **37 open**, 14 fixed, 3 invalid or external. Of the open ones, 3 are critical and 20 high.

Claims that no longer hold are kept with status `INVALID` rather than deleted.


### Open

Ordered by severity, then area.

| ID | Sev | Phase | Area | Finding |
|---|---|---|---|---|
| `C41` | critical | 0 | Frontend | Documented quick start yields a black map panel: empty Mapbox token, no error shown, zero markers |
| `C5` | critical | 1 | Ingest | Geocoder and classifier both do unanchored substring matching; short keys win mid-word |
| `C8` | critical | 1 | Ingest | Dedup drops the spatial predicate whenever the incoming report has no coordinates |
| `C20` | high | 2 | Collection | TLE fetcher: no persistence, no backoff, and a failed fetch wipes the cache to zero |
| `C31` | high | 2 | Collection | A failed poll leaves the last fleet and a frozen as_of in place with status still "ok" |
| `C44` | high | 3 | Frontend | Timeline playback advances speed*1000 ms per 100 ms tick, and the real rate depends on tab visibility |
| `C45` | high | 3 | Frontend | Events, aircraft and vessels are DOM <Marker> overlays, not Source/Layer — 98 marker nodes measured live |
| `C46` | high | 3 | Frontend | 46 markers carry transition: transform 2s linear against a 15 s aircraft / 10 s vessel poll — fabricated motio… |
| `C47` | high | 1 | Frontend | types/event.ts omits six fields the API already returns, including today's extraction_status and is_geolocated |
| `C51` | high | 2 | Frontend | WebSocket reconnect refetches nothing and the server sends no backlog: events during a drop are lost until rel… |
| `C10` | high | 4 | Ingest | merge_duplicate keeps only channel, severity and coordinates; the incoming report's text and identity are drop… |
| `C11` | high | 4 | Ingest | Reliability boost keys on report_count, not on distinct channels, so one source repeating itself raises confid… |
| `C12` | high | 0 | Ingest | Admin fix/reclassify tasks rewrite coordinates, severity and summary without updating the Phase 0 tags |
| `C2` | high | 1 | Ingest | Six independent severity=5 literals, not four; the clamp validator is dead code |
| `C4` | high | 1 | Ingest | (-25,80) sentinel declared in 3 modules, read by 3 query predicates, rendered as a real marker |
| `C6` | high | 1 | Ingest | Country names resolve to national centroids via Nominatim and are marked is_geolocated=true |
| `C7` | high | 1 | Ingest | KNOWN_LOCATIONS maps actor acronyms (idf/iaf/irgc/centcom) to headquarters coordinates |
| `C9` | high | 1 | Ingest | Dedup candidate query is LIMIT 20 with no ORDER BY, so the true duplicate can fall outside the window |
| `C60` | high | — | Platform | No auth on any route or the WS; CORS reflects any origin with credentials, DELETE allowed |
| `C62` | high | — | Platform | All seven /events/admin/* endpoints, including both DELETEs, are unauthenticated |
| `C63` | high | 1 | Platform | Alembic has no versions/; schema comes from create_all plus a hand-kept ALTER list that already crashed startu… |
| `C66` | high | — | Platform | Zero tests and no CI anywhere in the repo or the working tree |
| `C69` | high | — | Platform | demo.py attributes fabricated strikes on real nuclear sites to real named OSINT outlets, on a public MIT repo |
| `C29` | medium | 2 | Collection | Second AIS box is mislabelled "Eastern Mediterranean" and supplies 85% of the vessel feed from outside any AO |
| `C32` | medium | 1 | Collection | altitude mixes feet (adsb.lol) and metres (OpenSky) in one field, and the UI labels it both ways |
| `C43` | medium | 3 | Frontend | Escalation gauge publishes a 1-decimal mean of 20 severities with no provenance and no no-data state |
| `C48` | medium | 3 | Frontend | Event type is encoded by hue alone on map, globe, terrain and timeline; only the feed carries a text label |
| `C49` | medium | 3 | Frontend | No prefers-reduced-motion guard anywhere: 6 keyframe animations, an 8s scan line and an audio blip |
| `C50` | medium | 3 | Frontend | LiveFeed NEW badge compares array lengths against a 200-cap, so it stops firing permanently once the cap is hi… |
| `C52` | medium | — | Frontend | Cesium credits are routed to a detached div, suppressing Ion/Bing/Google attribution required by their terms |
| `C53` | medium | 3 | Frontend | Three renderers duplicate mark logic; the type palette is copied into 5 files — values match, fallbacks and th… |
| `C61` | medium | — | Platform | Postgres published on 0.0.0.0:5432; backend DATABASE_URL hardcoded so POSTGRES_PASSWORD cannot change it |
| `C68` | medium | — | Platform | Vite HMR is blind across the Windows bind mount; the backend only reloads because watchfiles polls |
| `C70` | medium | 0 | Platform | Demo path bypasses classifier, geocoder, dedup and track_history, so the zero-config run exercises none of the… |
| `C71` | medium | 3 | Platform | Demo rows ARE tagged source='demo' in the DB and API; it is the UI that discards the distinction |
| `C65` | low | — | Platform | Shutdown calls task.cancel() without awaiting, so no client-close path is guaranteed to run |
| `C67` | low | — | Platform | No .dockerignore; frontend COPY . . does overlay host node_modules, but the linux binaries survive |

### Fixed

Verified fixed in the current tree.

| ID | Sev | Phase | Area | Finding |
|---|---|---|---|---|
| `C21` | high | 2 | Collection | adsb.lol 403 to the default httpx User-Agent - fixed by a contact-bearing UA |
| `C24` | high | 2 | Collection | Jamming test no longer reads position_source/mlat (ground-feeder density) |
| `C25` | high | 2 | Collection | `nac_p == 0 and nic == 0` replaced with the published gpsjam threshold |
| `C26` | high | 2 | Collection | Jamming emits a ratio with an explicit denominator and a minimum cell size |
| `C27` | high | 0 | Collection | "I cannot measure" is now a distinct status, not zero zones |
| `C28` | high | 2 | Collection | AO re-centred and narrowed; aircraft coverage verified live |
| `C42` | high | — | Frontend | tsc/vite build errors are gone: fixed today in 0bd4835 (sun/moon guards, Terrain arg, useRef initial value) |
| `C22` | medium | 2 | Collection | Aircraft cache timestamp is now wall-clock, but /tracking/aircraft still exposes no timestamp at all |
| `C23` | medium | 0 | Collection | Poll log hard-coded "adsb.lol" - now reports the source actually used |
| `C40` | medium | 3 | Frontend | app-grid row minimum: already minmax(0,1fr), fixed earlier today in 716ffec |
| `C1` | medium | 1 | Ingest | Fallback rows are now tagged extraction_status; the fabricated severity=5 survives |
| `C3` | low | 0 | Ingest | parse_failed is a distinct tagged status; only non-dict JSON still mis-tags as llm_failed |

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
| `C72` | high | — | Platform | Anthropic key returns 400 organization_on_hold, so live classification cannot run at all |

### Invalid

Did not survive checking.

| ID | Sev | Phase | Area | Finding |
|---|---|---|---|---|
| `C73` | low | — | Platform | frontend/dist is not committed and never has been — it is an untracked local build artifact |

### Detail: open critical and high findings


#### `C41` — Documented quick start yields a black map panel: empty Mapbox token, no error shown, zero markers

`critical` · phase 0 · Frontend

**Where:** MapPanel.tsx:8 `const MAPBOX_TOKEN = import.meta.env.VITE_MAPBOX_TOKEN ?? "";` and :107 `useState<"2d"|"globe"|"terrain">("2d")`. README.md:14 tells the user `echo "DEMO_MODE=true" > .env`, which leaves MAPBOX_TOKEN unset; docker-compose.yml maps `VITE_MAPBOX_TOKEN: ${MAPBOX_TOKEN:-}` → empty. Reproduced: built the frontend with no token into the scratchpad and served it on :4599. Screenshot shows header, live feed, gauge, timeline and legend rendering normally over a fully black map area. Console: `Error: An API access token is required to use Mapbox GL ... at Map.setStyle ... at Nd._initialize`. DOM probe: `hasMapboxMap: true, markerCount: 0`. The app does NOT crash — react-map-gl swallows the constructor throw.

**Impact:** On the exact documented quick-start path, the default view is a black rectangle. Every conflict event, aircraft, vessel, trail and jamming zone is invisible, while the legend overlaid on the same black rectangle still asserts "AIRCRAFT (22) VESSELS (34) GPS INTERFERENCE (2)" — the UI claims contents it is not drawing, with no on-screen error.

**Fix:** In MapPanel, branch on `!MAPBOX_TOKEN`: render an explicit in-panel notice ("2D basemap unavailable — VITE_MAPBOX_TOKEN not set") instead of <Map>, and default `viewMode` to "globe" (which needs no token and whose assets are all local) when the token is empty.


#### `C5` — Geocoder and classifier both do unanchored substring matching; short keys win mid-word

`critical` · phase 1 · Ingest

**Where:** geocoder.py:645-657 `for known_name, coords in KNOWN_LOCATIONS.items(): if known_name in normalized and len(known_name) > best_len` — plain `in`, no \b. Same bug in classifier.py:224-228 `if loc_lower in text_lower`. Live, in the running backend: geocode('Maarakeh') -> (34.0975, 49.1947) [key 'arak', geocoder.py:124, Iran]; geocode('Kiryat Shemona') -> (32.0790, 34.7860) [key 'kirya', :227, IDF HQ Tel Aviv]; geocode('Romania') -> (22.0, 57.0) [key 'oman', :477]; partial('Najafabad') -> 'najaf' (:390); partial('Homsi') -> 'homs'; partial('Qomish') -> 'qom'. Classifier fallback: _regex_location_fallback('Analysts note the T4 designation on the airframe.') -> 'T4' (the Syrian T-4 airbase); _regex_location_fallback('Sirens in Kiryat Shemona near the northern border.') -> 'Kirya'.

**Impact:** A named place is silently relocated to a different country. Maarakeh (south Lebanon) plots in Arak, Iran, ~1,100 km away. Kiryat Shemona (Israel's northern border, the town that actually takes rocket fire) plots at IDF headquarters in Tel Aviv. The event is then marked is_geolocated=true, so the row asserts a confident wrong position, and dedup's 50 km ST_DWithin clusters it with whatever is genuinely near the wrong point. The claimed rate (49 strings / 73 events) cannot be re-measured here: all 8246 rows in the demo DB have location_name='' (`select location_name, count(*) from events group by 1` returns one row, the empty string), so the mechanism is confirmed but the count is not.

**Fix:** In geocoder.py:650 and :655 and classifier.py:226, replace `known_name in normalized` with a word-boundary test: `re.search(rf'(?<![\w]){re.escape(known_name)}(?![\w])', normalized)`. Then drop or lengthen the 2-3 char keys ('t4', 'qom', 'idf', 'iaf') that still produce standalone false positives.


#### `C8` — Dedup drops the spatial predicate whenever the incoming report has no coordinates

`critical` · phase 1 · Ingest

**Where:** dedup.py:65-74: `if lat is not None and lon is not None:` guards the whole `ST_DWithin(cast(Event.geometry, Geography), cast(new_geom, Geography), 50000)` clause; when it is skipped the statement is only `event_type == X AND timestamp BETWEEN ts-15m AND ts+15m` (dedup.py:56-62). Proven live against the running DB: taking event 8250 (military, 25.585N 60.910E) and re-submitting its own summary and timestamp — `check_duplicate(..., lat=None, lon=None)` -> None, `check_duplicate(..., 64.0, -22.0)` -> None, `check_duplicate(..., 25.585, 60.910)` -> 8250. Today the branch is never taken because telegram.py:272 and news_feeds.py:341 substitute (-25,80) before calling, which instead puts every ungeocoded event inside one 50 km cluster in the Indian Ocean; and ST_DWithin on a NULL Event.geometry e…

**Impact:** This is the trap that blocks Phase 1 exactly as claimed. The moment the sentinel is replaced by NULL, every ungeocoded report starts matching on time+type alone across the whole table, and any two unrelated military reports 15 minutes apart with jaccard>0.4 on their summaries get merged into one event with an inflated report_count and a boosted source_reliability. The failure is silent — no error, just fewer events that each claim more corroboration.

**Fix:** Make the no-coordinate case explicit rather than permissive: when lat/lon are None, require `Event.geometry.is_(None)` (or `Event.is_geolocated.is_(False)`) plus a tighter time window and a higher jaccard threshold, instead of dropping the predicate. Land this in the same commit as C4's NULL-geometry change.


#### `C20` — TLE fetcher: no persistence, no backoff, and a failed fetch wipes the cache to zero

`high` · phase 2 · Collection

**Where:** backend/app/services/satellites.py:21-59. The loop sleeps REFRESH_INTERVAL (6h) whatever happened: `await asyncio.sleep(REFRESH_INTERVAL)` at :59 is outside the try/except and outside any success test. Lines 53-54 `_tle_cache.clear(); _tle_cache.extend(all_tles)` run even when every group returned non-200, so one 403 replaces a good cache with []. No persistence anywhere: `_tle_cache: list[dict] = []` at :18 is the only store (grep for TLE in models.py/db.py: no hits), and satellites.py has not changed since 187b0fa - the v3 rebuild did not touch it. Live now: `docker logs conflict-monitor-backend-1 | grep -i celestrak` shows 4 process starts today, each issuing GET gp.php?GROUP=military -> 403 Forbidden -> "Total TLEs cached: 0"; re-probed from inside the container with the code's own hea…

**Impact:** After any failed cycle the satellite layer is empty for a full 6 hours - there is no retry short of a restart, and because the fetch is the first thing the task does, a restart loop hammers CelesTrak and extends the IP block (that is how today's block was earned). Frontend TLE_POLL_MS is also 6h (useTracking.ts:63), so a client that loads during an empty window shows 0 SAT for up to 12h. "0 SAT" is indistinguishable from "no satellites".

**Fix:** Three small changes in satellites.py: (1) only clear/extend `_tle_cache` when `all_tles` is non-empty, so a 403 keeps the last good set; (2) write the last good set to disk (e.g. backend/sessions/tle_cache.json) and load it at startup, skipping the fetch when it is <6h old - that also stops restart-storms hitting CelesTrak; (3) on a failed cycle sleep an exponential backoff (60s doubling to ~30min) instead of the full 6h. The 403 itself is an IP block and will clear on its own.


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


#### `C47` — types/event.ts omits six fields the API already returns, including today's extraction_status and is_geolocated

`high` · phase 1 · Frontend

**Where:** frontend/src/types/event.ts:1-15 declares only id, source, channel_name, raw_text, summary, event_type, severity, lat, lon, timestamp, created_at, report_count?, reporting_channels?. `curl http://localhost:8000/events?limit=2` returns additionally: `source_reliability`, `location_name`, `telegram_message_id`, `source_url`, `extraction_status`, `is_geolocated`. The last two were added today by 9fd3fbf (backend/app/schemas.py) and `/events/stats/extraction` is live (`{"total":8245,"by_extraction_status":{"null":8245},...}`).

**Impact:** The frontend type is the reason nothing on screen can say "I guessed". Phase 0 put extraction_status and is_geolocated on the wire and the UI silently drops them — the classifier-failure tag reaches the browser and dies in the JSON parse. Same for source_reliability, which the README advertises as a filterable feature.

**Fix:** Add the six fields to ConflictEvent (`extraction_status: string | null; is_geolocated: boolean | null; location_name: string; source_reliability: number | null; telegram_message_id: number | null; source_url: string;`). That is the one-line prerequisite for every Phase 1 display change.


#### `C51` — WebSocket reconnect refetches nothing and the server sends no backlog: events during a drop are lost until reload

`high` · phase 2 · Frontend

**Where:** useEventStream.ts:82-87 — the REST backfill is in a `useEffect(..., [])`, so it runs once on mount only. useEventStream.ts:72-76 `ws.onclose` sets isConnected false and schedules `setTimeout(connect, 3000)`; `connect` (:36-79) opens a socket and installs handlers, and never refetches. Server side: backend/app/routes/ws.py:12-14 accepts and immediately enters the receive loop; broadcaster.py:14-18 `connect()` only does `ws.accept()` and appends to the client list. No history, no cursor, no last-event-id.

**Impact:** Any event broadcast while the socket is down never reaches that client. The header flips back to a green pulsing LIVE (Header.tsx:138-151) the moment the socket reopens, so the UI asserts completeness it does not have; the only recovery is a manual page reload. A backend restart or a laptop sleep silently punches a hole in the feed.

**Fix:** On successful `ws.onopen`, re-run the REST fetch and merge by id (dedupe on `e.id`) rather than replace — a 3-line change in `connect`. A server-side backlog (send the N newest events on accept) is the more complete fix but is not required to close the hole.


#### `C10` — merge_duplicate keeps only channel, severity and coordinates; the incoming report's text and identity are dropped

`high` · phase 4 · Ingest

**Where:** dedup.py:98-105 `async def merge_duplicate(session, existing, new_channel, new_severity, new_lat, new_lon)` — the incoming raw_text, summary, source_url and telegram_message_id are not parameters, so they cannot be kept. Call sites pass nothing more: telegram.py:288 `await merge_duplicate(session, existing, channel_name, severity, lat, lon)` and news_feeds.py:356. The body (:106-149) writes only report_count, reporting_channels, severity (max), lat/lon (only when existing.lat is None, dedup.py:122), and source_reliability.

**Impact:** After a merge the only surviving evidence is `report_count += 1` and a channel name appended to a comma-joined string. The second report's own wording, its Telegram permalink and its message id are gone, so nobody can later check whether the two reports were independent or verbatim copies — which is precisely the judgement C11's reliability boost depends on. It also means the merged message has no telegram_message_id row, so _message_already_saved (telegram.py:214-218) cannot recognise it on the next restart and it is re-ingested and re-merged.

**Fix:** Replace the merge with a link: an event_reports child table holding (event_id, source, channel, raw_text, summary, source_url, telegram_message_id, ingested_at), one row per report, with report_count and reporting_channels derived from it. That is the Phase 4 'link don't merge' change; the minimum interim step is to pass and store source_url and telegram_message_id.


#### `C11` — Reliability boost keys on report_count, not on distinct channels, so one source repeating itself raises confidence

`high` · phase 4 · Ingest

**Where:** dedup.py:108 `new_count = (existing.report_count or 1) + 1`; :132-138 `new_channel_rel = _channel_reliability(new_channel) or 1; current_rel = existing.source_reliability or 1; combined = max(current_rel, new_channel_rel); if new_count >= 3: combined = min(5, combined + 1)`. The channel de-duplication at :112-115 only affects the display string `reporting_channels`; report_count at :108 increments unconditionally, so three merges from the same channel_name trip the >= 3 branch. `get_reliability` (seed_channels.py:405-408) returns None for any channel outside CHANNEL_REGISTRY, which `or 1` turns into the lowest score, so an unknown channel still counts toward the threshold.

**Impact:** Corroboration is measured by counting merge events, and the pipeline generates merge events by itself: the RSS poller re-merges the same article after any restart (see also_found), and a restarted Telegram backfill re-merges the tail. Three self-merges of one article raise source_reliability by one and can push it to 5, which is what the min_reliability filter on GET /events and the UI treat as best-sourced. With no model of channel copying, 15 Telegram channels reposting one another's identical text reads as 15 independent confirmations.

**Fix:** Count distinct sources, not merges: derive the boost from the child-report table of C10 (`count(distinct channel)`), and gate it on a channel-family graph so channels known to repost each other contribute once. Until that exists, change dedup.py:135 to test the length of the de-duplicated channel set rather than new_count.


#### `C12` — Admin fix/reclassify tasks rewrite coordinates, severity and summary without updating the Phase 0 tags

`high` · phase 0 · Ingest

**Where:** routes/events.py:119-129 (_fix_null_coords_task) writes db_ev.lat, .lon, .geometry, .location_name, .severity, .summary and commits — no extraction_status, no is_geolocated. Same at :203-215 (_reclassify_vague_locations_task): location_name, lat, lon, geometry, severity, summary, no tags. Both call classify_message (:100, :190) which now returns extraction_status, and both discard it (:101-103 and :191 read only location_name/severity/summary). Worse, :102-103 `new_severity = classified.get("severity", ev.severity); new_summary = classified.get("summary", ev.summary)` — if that re-classification falls back, the fabricated severity 5 and `raw_text[:200]` summary overwrite whatever was there, still untagged.

**Impact:** These are the two endpoints most likely to be run over the archive, and they systematically corrupt the instrument Phase 0 just installed. A row that was correctly tagged is_geolocated=false and then successfully re-geocoded keeps is_geolocated=false while holding real coordinates; a row that was tagged 'ok' and is then re-classified by a failing API keeps 'ok' while holding a fabricated severity and a truncated raw-text summary. After one run of either task, /events/stats/extraction no longer describes the table.

**Fix:** In both write blocks set `db_ev.extraction_status = classified.get("extraction_status")` and `db_ev.is_geolocated = True` alongside the coordinate write, and skip the severity/summary overwrite entirely when extraction_status != 'ok' (events.py:102-103 and :211-214).


#### `C2` — Six independent severity=5 literals, not four; the clamp validator is dead code

`high` · phase 1 · Ingest

**Where:** models.py:21 `severity: Mapped[int] = mapped_column(Integer, default=5)`; schemas.py:12 `severity: int = 5`; classifier.py:179 `severity: int = Field(default=5, ge=1, le=10)`; classifier.py:334 `"severity": 5,` in _build_fallback; telegram.py:257 `severity = result.get("severity", 5)`; news_feeds.py:323 same. A seventh probe at telegram.py:251 `_is_noise(raw_text, 5)`. Live: ClassifierResult(severity=15) raises ValidationError (the ge/le constraint runs before the mode='after' clamp_severity at :190-193), so out-of-range never clamps — it becomes parse_failed and lands on the literal 5 at :334.

**Impact:** There is no single place where 'we did not measure severity' can be expressed. A row with severity=5 may mean: the model said 5; the model said 15 and the row fell back; there was no API key; the DB default fired; or a POST omitted the field. All six write the same integer into the same column, and severity drives MIN_SEVERITY filtering, the merge_duplicate 'take higher severity' rule, and the map's mark size/colour.

**Fix:** Make severity nullable end-to-end (models.py:21 drop default, schemas.py:12 `severity: int | None = None`, classifier.py:179 `severity: int | None = None`), delete the literal at classifier.py:334, replace `result.get("severity", 5)` at telegram.py:257 and news_feeds.py:323 with a None-propagating read, and either drop the dead clamp_severity or move the clamping into a mode='before' validator so 11 becomes 10 instead of a parse failure.


#### `C4` — (-25,80) sentinel declared in 3 modules, read by 3 query predicates, rendered as a real marker

`high` · phase 1 · Ingest

**Where:** Declared: routes/events.py:23-24 `_UNKNOWN_LAT = -25.0 / _UNKNOWN_LON = 80.0`; services/news_feeds.py:69-70 `UNKNOWN_LAT/UNKNOWN_LON`; services/telegram.py:24-25 (`# Indian Ocean parking for unresolvable locations`). Written: telegram.py:272, news_feeds.py:341. Read by predicates: events.py:82 `(Event.lat == _UNKNOWN_LAT)` in _fix_null_coords_task, :295 `Event.lat != _UNKNOWN_LAT` and :300 `or_(Event.lat.is_(None), Event.lat == _UNKNOWN_LAT)` in geo_stats. Also read implicitly by dedup.py:122 `if existing.lat is None` (never true for a parked row) and NOT filtered by frontend/src/components/MapPanel.tsx:110 `events.filter((e) => e.lat != null && e.lon != null)`.

**Impact:** Three copies of one magic number that must stay in sync, and the value has become load-bearing: geo_stats' definition of 'unknown' is a float equality test, so a genuine event at latitude -25.0 is miscounted as unknown; a parked event can never be repaired by merge_duplicate because its lat is not None; and MapPanel draws every ungeocoded event as an ordinary marker in the Indian Ocean, i.e. the absence of a location is displayed as a positive geographic claim. Removing the sentinel in Phase 1 silently changes the meaning of all three predicates at once.

**Fix:** Write lat/lon/geometry as NULL when geocoding fails and let is_geolocated carry the fact; replace the three predicates with `Event.is_geolocated.is_(False)` / `.is_(True)`; delete all three constant pairs; add `&& e.is_geolocated !== false` to MapPanel.tsx:110. Must land together with C8/C9 or dedup breaks.


#### `C6` — Country names resolve to national centroids via Nominatim and are marked is_geolocated=true

`high` · phase 1 · Ingest

**Where:** KNOWN_LOCATIONS has no 'united states'/'iran'/'israel' key (checked live, all return None), so geocode() falls through to step 5, geocoder.py:663 `result = await _query_nominatim(name)` with `limit=1, bounded=0`. Live from the running backend: geocode('United States') -> (39.7837304, -100.445882) — a field in Kansas; geocode('Iran') -> (32.6475, 54.5644) — the Dasht-e Kavir; geocode('Israel') -> (30.8124, 34.8595) — the Negev. The table also holds country-centroid entries directly: geocoder.py:477 `"oman": (22.0000, 57.0000)`. geocode() returns a bare (lat, lon) tuple with no precision or confidence, and telegram.py:268-270 / news_feeds.py:337-339 set `is_geolocated = True` on any non-None return.

**Impact:** A country-level extraction and a facility-level extraction are stored identically: same two floats, same is_geolocated=true, same marker. 'US strikes' with no named target plots as a point in Kansas and is then eligible for the 50 km dedup radius and drawn at full confidence. is_geolocated cannot mean what Phase 1 needs it to mean until the geocoder reports how precise the hit was.

**Fix:** Have geocode() return (lat, lon, precision) where precision is one of facility/district/city/region/country, sourced from which table branch matched (geocoder.py:632-665) and from Nominatim's `type`/`class`/`addresstype`. Persist it as a column and set is_geolocated only for city-or-better; render country-precision hits as an area, not a point.


#### `C7` — KNOWN_LOCATIONS maps actor acronyms (idf/iaf/irgc/centcom) to headquarters coordinates

`high` · phase 1 · Ingest

**Where:** geocoder.py:545-552: `"khomeini": (35.6892,51.3890)`, `"central command"/"centcom"/"us central command": (25.1175,51.3150)`, `"fifth fleet": (26.2200,50.5500)`, `"iaf": (31.2083,34.9390) # Israeli AF -> Nevatim`, `"idf": (32.0790,34.7860) # IDF HQ -> Kirya`, `"irgc": (35.7156,51.4063) # IRGC HQ`. classifier.py:54 also has 'Kirya' and :45 'IRGC HQ' in LOCATION_KEYWORDS. Live: geocode('IDF spokesperson confirmed the operation') -> (32.079, 34.786); geocode('IRGC statement') -> (35.7156, 51.4063); geocode('IAF jets') -> (31.2083, 34.939); geocode('CENTCOM said') -> (25.1175, 51.315). Combined with C5's unanchored matching, any message naming the actor and no place is geolocated to that actor's HQ.

**Impact:** The subject of a sentence is converted into the location of the event. 'The IDF said its aircraft struck targets in Syria' resolves to Tel Aviv — the attacker's headquarters, not the target. Because these keys are short (3 chars) they only win when no longer key matches, which is exactly the ungeolocatable messages the sentinel was meant to catch: the tokens convert an honest 'no location' into a confident wrong one, and is_geolocated=true hides it.

**Fix:** Delete 'idf', 'iaf', 'irgc', 'centcom', 'central command', 'us central command', 'fifth fleet', 'khomeini' from KNOWN_LOCATIONS (geocoder.py:545-552) and 'IRGC HQ'/'IDF HQ' from LOCATION_KEYWORDS (classifier.py:45,54). If HQ coordinates are genuinely wanted, keep them behind an explicit key like 'idf headquarters' that only an exact match can reach.


#### `C9` — Dedup candidate query is LIMIT 20 with no ORDER BY, so the true duplicate can fall outside the window

`high` · phase 1 · Ingest

**Where:** dedup.py:76 `result = await session.execute(stmt.limit(20))` — the statement built at :56-74 has no order_by, and the compiled SQL confirms no ORDER BY clause. Proven live: for probe event 8250, the ±15min same-type window holds 26 rows; the unordered LIMIT 20 returns ids [8212,8214,8215,8216,8218,8219,8220,8221,8222,8223,8224,8225,8227,8228,8230,8232,8236,8237,8238,8241] and `8250 in candidates` is False. The jaccard loop at :84-93 then scores only those 20 and returns None.

**Impact:** Dedup is not a function of its inputs. Postgres may return any 20 of the matching rows and is free to change that set between runs after a VACUUM, a plan change, or a parallel scan — so the same message ingested twice can be a duplicate one time and a new event the next, and the demonstration above shows the genuine match being truncated away while 20 non-matches are scored. It also makes any Phase 4 gold-label evaluation of dedup meaningless, and it compounds C8: widening the candidate set by dropping the spatial filter makes truncation more likely, not less.

**Fix:** Add a deterministic ordering that puts the most likely duplicate first, e.g. `stmt.order_by(func.abs(func.extract('epoch', Event.timestamp - timestamp))).limit(20)`, or drop the limit and do the jaccard scoring in SQL. At minimum `order_by(Event.id.desc())` so the result is reproducible.


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
- [ ] Run for one week and record the real fallback rate — **blocked: the Anthropic key is disabled**
- [ ] Label the archive's sentinel rows with a single `UPDATE` ($0, no reprocessing)

### Phase 1 — Let the schema say "I guessed"

Additive columns first, then remove the lies. Expect the map to get roughly 80% emptier; say so up
front or it reads as a regression.

- [ ] `geo_precision` (`facility` / `city` / `admin1` / `country_centroid` / `region_named` / `unresolved`), `geo_uncertainty_m`, `evidence_span`
- [ ] Kill all four severity-5 defaults, including `Field(default=5)` where an omitted key validates clean
- [ ] Delete the `(-25, 80)` sentinel and migrate the query predicates that *read* it
- [ ] **Same commit**: guard dedup against NULL geometry, or unlocated rows match on time + type across the whole table
- [ ] Word boundaries on the geocoder partial match (simulated: 49 strings change, zero regressions)
- [ ] Add the ~15 highest-volume missing facilities (`Prince Sultan Air Base`, `Ras Laffan`, `Ben Gurion`, …)

### Phase 2 — The denominator

Where "a dead feed and a quiet night look identical" actually dies.

- [x] Interference layer: real AO, real test, real denominator, three honest states (`64b690a`)
- [ ] `feed_health` table written from each poller's existing loop; `/health` exposing it
- [ ] Per-tile currency, not per-feed — a live socket delivering the wrong ocean still reads GREEN
- [ ] Concurrent spatial control ring so absence claims work without waiting 28 days for a baseline
- [ ] Trailing robust baselines with an explicit `regime_id`; never pool across the 2023/2026 regime breaks
- [ ] Resolve the maritime coverage gap (see Blocked)

### Phase 3 — The display

- [ ] Mark geometry = evidence geometry, driven by `geo_precision`; unresolved rows go to a tray, never the map
- [ ] Replace DOM `<Marker>` loops with Source/Layer; the trails code already does this correctly
- [ ] Promote the timeline into a feed-liveness lane so a collection gap and a quiet period are different shapes
- [ ] Delete `EscalationGauge`; replace with an indicator rail whose row zero is feed currency
- [ ] Render age, never a green dot
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
| **Anthropic API key disabled** | Returns `400 organization_on_hold` — "This organization has been disabled." Live classification cannot run, so the Phase 0 measurement week cannot start. Appeal at `console.anthropic.com/appeal` or swap the key. |
| **No AIS coverage in the Gulf** | Free terrestrial AIS cannot see the Gulf. Options: IMF PortWatch (free, daily chokepoint counts, 5–12 days stale — a baseline, not a live feed); Global Fishing Watch API (free for research, satellite-derived — probe it); satellite AIS (Spire/ORBCOMM, enterprise pricing). |
| **Secrets in `.env`** | Live Anthropic key, Telegram API hash + phone, Mapbox token, AISStream key, OpenSky password. Correctly gitignored and clean in git history, but worth rotating — they were displayed in a terminal session. |
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
| The interference fix re-centred the AO | It re-centred the **primary** path only. `opensky.py:160` still requests the old `lamin 15 / lamax 45 / lomin 25 / lomax 65` box on the OpenSky fallback, so the two paths now disagree about where the AO is | The audit compared the two poll functions. **Fixed 2026-09-19 in `4e59ede`**: both paths derive the AO from `CENTRE_LAT`/`CENTRE_LON`/`RADIUS_NM`, and OpenSky results are clipped to the circle |

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
| 2026-09-19 | `4e59ede` | One AO for both aircraft poll paths; OpenSky bbox derived from the constants and clipped to the circle |
