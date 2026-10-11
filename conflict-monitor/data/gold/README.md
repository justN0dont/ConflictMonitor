# The gold set

Labelled rows from the production archive, and labelled pairs of them, so that the
pipeline's claims can be measured instead of asserted. `tools/gold_set.py` draws it,
validates it, and scores the archive's stored labels, a classifier run and the dedup
matchers against it. **Nothing here is loaded by the application at runtime**;
`tools/check_policy.py` fails if `backend/app` or `frontend/src` ever references it.

| | |
|---|---|
| Source | `conflict_monitor-20260818.sql.gz`, md5 `3da7684f9dc9fbfd210679e02710b37c` (83,938 events) |
| Drawn | 2026-10-10, seed `20260818`, `python tools/gold_set.py sample <dump>` — deterministic |
| Regime window | 2026-02-01 → 2026-08-18. Six rows carry 2011–2023 timestamps (old content forwarded in 2026) and are excluded; the 640 `satellite_firms` rows are machine-written and excluded |
| Events | **425** rows, target 350 with a floor of 25 per stratum; strata = source family (telegram / rss) × located-or-not (the sentinel row is "unlocated") × military-or-other. `meta.json` carries N_h and n_h; the scorer weights by N_h/N, so a floored stratum never dominates |
| Pairs | **286** pairs of rows with the same `event_type`, the second within 180 min of the first; cells = time (≤15 min = dedup's window, 15–180 min = the link's) × geometry (both ≤50 km, both 50–500 km, both unlocated, one unlocated) × Jaccard-over-summary band (<0.15, 0.15–0.3, 0.3–0.4, >0.4, the live bar being `> 0.4`); 8 / 10 / 10 / 10 drawn per cell by band from 5,160,754 candidate pairs |
| Labelled | 425 of 425 events, 286 of 286 pairs, on 2026-10-10 |
| Labeller | `claude-fable-5.1 · Claude Code session 2026-10-10 · read raw_text[:2000], channel and time only` on every row. **Reviewed by a human: 0 rows.** `reviewed_by` is null everywhere |

## What a label is

The reader saw `raw_text` (the first 2,000 characters, the cut the classifier itself applies),
the channel and the time — and **not** the archive's own `event_type`, `location_name` or
`summary`, and not any similarity score. The labels are independent of what they measure.

**events.jsonl**, one row per archive event, `gold`:

- `is_event` — the text reports a specific occurrence (an attack, a statement, a decision, a
  dated market move). False for analysis, opinion, explainers, features, polls, promotions.
- `event_type` — one of the prompt's five: `military`, `diplomatic`, `economic`,
  `humanitarian`, `cyber`. Statements and negotiations are `diplomatic`. A protest, an
  election result, a court ruling or a gas accident fits none of the five, and is `null`
  with a reason in `cannot_tell`.
- `location_name` — the most specific place **where the event happened**, as the text names
  it (English transliteration for Arabic text). `""` when the text names no such place; a
  country named only as an actor ("Iran says…") is not a location.
- `killed_reported` — the number the text says were killed; `null` when it states none.
  `cannot_tell` carries the cases the prompt does not settle (a cumulative war toll, a
  judicial execution, "found dead").
- `cannot_tell` — `{field: reason}`; a `null` in `event_type` or `location_name` must have one.

**pairs.jsonl**, one row per pair, `gold.label`:

- `same` — the two texts report the same incident, including an update, a correction, a
  repost, or two outlets on one statement.
- `different` — distinct incidents, even when related (two towns bombed the same night).
- `cannot_tell` — the texts fit both readings (a tally that may include the incident, two
  excerpts of one appearance, a strike and the announcement that the campaign began), with
  a reason.

## What it measures, and what it cannot

- The archive's stored labels came from `claude-haiku-4-5` plus the regex fallback, with no
  `extraction_status` to tell which. The scorer measures that stored column against the gold
  labels, weighted to the archive. `killed_reported` is not in the dump; only a run can be
  scored on it.
- **Every pair is two rows the archive kept apart.** The old merge destroyed the second text of
  every pair it *did* merge (19,027 of them), so the pairs hold the matcher's misses and the
  pairs outside its window — never its hits. Recall measured here is recall on that
  population; the matcher's precision at ingest is not in this file and cannot be put there.
- The pairs are drawn by band of the live score, so a pair set measures a matcher's **curve**,
  not a population rate; nothing in the pairs output is weighted.
- The labels are one reader's. Until `reviewed_by` is set on rows, every number is agreement
  with that reader, not with the ground. The reader is a Claude model: **do not evaluate a
  Claude-family classifier on this set**, or the number measures self-agreement.

## Running it

```bash
python tools/gold_set.py check                 # shape only; runs in CI
python tools/gold_set.py score                 # archive vs gold, pairs vs four matchers
# the current classifier, inside the backend container (Ollama is only reachable there):
MSYS_NO_PATHCONV=1 docker cp data/gold/events.jsonl conflict-monitor-backend-1:/tmp/gold_events.jsonl
MSYS_NO_PATHCONV=1 docker cp tools/gold_set.py  conflict-monitor-backend-1:/tmp/gold_set.py
MSYS_NO_PATHCONV=1 docker compose exec -T backend python /tmp/gold_set.py classify /tmp/gold_events.jsonl /tmp/gold_run.jsonl
MSYS_NO_PATHCONV=1 docker cp conflict-monitor-backend-1:/tmp/gold_run.jsonl runs/qwen3.8-27b-2026-10-10.jsonl
python tools/gold_set.py score --run runs/qwen3.8-27b-2026-10-10.jsonl
```

`sample` refuses to overwrite a file that carries labels; `--force` discards them. To review
a row, change its `gold` if it is wrong and set `reviewed_by`; the scorer prints the review
count on every run.

## Measured 2026-10-10

Full output: `python tools/gold_set.py score --run runs/qwen3.8-27b-2026-10-10.jsonl`.
Weighted = to the archive by stratum; raw = the labelled rows, with a 95% Wilson interval.

**The archive's stored labels against the gold labels**

| | weighted | raw |
|---|---|---|
| Rows whose text reports no incident | 17.5% ±3.7 | 15.8% (67/425) |
| `event_type` agrees, incident rows both sides decided | 55.6% ±5.3 | 64.6% (219/339) — 89 of the 120 disagreements are statements stored as `military` that the text makes `diplomatic` |
| Text names a place | 65.5% ±5.2 | 58.4% (209/358) |
| …and the stored place is the text's place | 27.0% ±4.7 | 33.5% (70/209) |
| Archive says Unknown while the text names a place | 45.1% | 31.6% (66/209) |
| Text names no place, yet the archive stored one | 52.3% | 35.6% (53/149) — `Strait of Hormuz` ×12 and `Iran` ×12 lead: the flag and regex fallbacks |
| Text states a death toll | 7.4% ±3.1 | 6.1% (22/358) |

The last two weighted figures carry no useful interval: the strata are drawn on
located-or-not, which is the very thing those rows measure, so within-stratum variance is
near zero and the raw column is the honest spread.

**The current classifier (`qwen3.8-27b:latest`, `classify_message()`, 425 rows) against the gold labels**

| | weighted | raw |
|---|---|---|
| Status | `ok` on all 425, no fallback | |
| Noise verdict agrees (`is_noise` == not an incident) | 89.2% ±3.1 | 89.6% (381/425) |
| …non-incidents it kept | 48.3% ±15.5 | 55.2% (37/67) |
| …incidents it dropped as noise | 2.3% ±1.7 | 2.0% (7/358) |
| `event_type` agrees, kept incident rows | 95.1% ±2.4 | 94.0% (316/336) |
| Text names a place and the model's place is the text's place | 83.6% ±5.2 | 82.6% (171/207); exactly 73.8% (153/207) |
| Text names no place, yet the model named one | 83.9% ±5.5 | 73.6% (106/144) — the prompt forbids Unknown when any place is mentioned, and the flag fallback adds a country after that |
| `killed_reported` agrees (null == null, or the same number) | 98.5% ±1.4 | 98.6% (346/351) |
| …text states a toll and the model copied it | 93.0% ±6.6 | 95.5% (21/22); null for a stated toll 0/22 |
| …text states no toll and the model invented one | 1.1% ±1.3 | 1.2% (4/329) |

**Four matchers on the 286 pairs** (112 same, 157 different, 17 cannot_tell)

| matcher | bar | P | R | F1 |
|---|---|---|---|---|
| jaccard_summary (live) | 0.4 | 0.58 | 0.29 | 0.39 |
| jaccard_summary | 0.2 | 0.58 | 0.85 | 0.69 |
| containment_raw_clean | 0.4 | 0.61 | 0.80 | 0.69 |
| containment_raw_clean | 0.6 | 0.67 | 0.50 | 0.57 |
| jaccard_raw | 0.2 | 0.54 | 0.62 | 0.58 |

No text score clears P 0.70 on this population at any bar with R above 0.30. The false
positives are one outlet's template with a different place in it ("Breaking | Israeli
occupation forces storm the town of X"), which is why a bar on text alone cannot be the
whole rule: the place name has to sit beside it. The live rule, on the pairs it can see at
all (≤15 min, both ≤50 km or both unlocated): TP 1, FP 1, FN 28. **83 of the 112
same-incident pairs lie where it can never look** — beyond 15 minutes, with one side
unlocated, or 50–500 km apart.
