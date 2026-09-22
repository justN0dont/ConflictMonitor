# tools/

Reproduction scripts for the measurements in [`../docs/FINDINGS.md`](../docs/FINDINGS.md).
Each one re-derives a claim made there; none of them mutate anything.

| Script | Answers |
|---|---|
| `probe_adsb_coverage.py` | Is there ADS-B coverage at a point, and do aircraft carry `nic`/`nac_p`? |
| `probe_ais_coverage.py` | Does AISStream see the Persian Gulf? (Runs a Gulf box, then an eastern-Med control.) |
| `archive_source_stats.py` | Per-source event counts and severity-5 rate in the production dump |
| `archive_locations.py` | Every distinct `location_name` and the coordinate it resolved to |
| `archive_fallback_rate.py` | The Phase 0 fallback rate, by re-classifying a stratified archive sample with the real `classify_message()` |
| `archive_sentinel_audit.py` | What the Indian Ocean sentinel migration would actually do to the archive, counted row by row |
| `geocoder_vs_archive.py` | Simulates `geocoder.py` against the archive; shows what word boundaries would change |
| `gazetteer_gaps.py` | Which location names the tables miss, ranked by event volume — the shortlist for new entries |
| `recover_from_transcripts.py` | Reconstructs source files from Claude Code transcripts (how v3 was partly recovered) |

The `archive_*` scripts read the production dump and are meant to run **on the VPS**:

```bash
scp tools/archive_source_stats.py truthevades:/tmp/
ssh truthevades 'python3 /tmp/archive_source_stats.py'
```

`archive_fallback_rate.py` is the exception: only its `sample` half runs there. Classification has to
run inside the backend container, because Ollama is reachable at `host.docker.internal:11434` from
there and nowhere else. Run it with no argument and it prints every command for both halves.

Three things about reading its output:

* **It describes one model**, and it names that model from the `extraction_model` recorded on the
  rows rather than from a constant in the script. Changing `OLLAMA_MODEL` means re-running the
  `classify` half over the *same* sample file before quoting any of the numbers again.
* **Archive comparisons are like-for-like.** The 83,938 stored events all passed two gates the
  classifier's output has not — the noise drop and `MIN_SEVERITY` — so rates set against them are
  computed over the sampled rows that would themselves have been inserted. The wider denominator is
  printed next to it, labelled, and is not the one to difference against the archive.
* **A stratum interval is not a per-feed interval.** The `COVERAGE` block prints, by name, the
  sources with no row in the sample at all; nothing in the report bounds those.

`selftest` is the negative control. It drives five failure branches — four infrastructure ones and
`parse_failed`, the only one a healthy stack can reach, reproduced with a stub daemon returning the
empty `response` that qwen3.8-27b really produced before `949aca8`.

`probe_ais_coverage.py` reads `AISSTREAM_API_KEY` from `../.env` and needs `pip install websockets`.
Run it outside the backend container — a `docker compose restart` kills an exec'd probe.
