# tools/

Reproduction scripts for the measurements in [`../docs/FINDINGS.md`](../docs/FINDINGS.md).
Each one re-derives a claim made there; none of them mutate anything.

| Script | Answers |
|---|---|
| `probe_adsb_coverage.py` | Is there ADS-B coverage at a point, and do aircraft carry `nic`/`nac_p`? |
| `probe_ais_coverage.py` | Does AISStream see the Persian Gulf? (Runs a Gulf box, then an eastern-Med control.) |
| `archive_source_stats.py` | Per-source event counts and severity-5 rate in the production dump |
| `archive_locations.py` | Every distinct `location_name` and the coordinate it resolved to |
| `geocoder_vs_archive.py` | Simulates `geocoder.py` against the archive; shows what word boundaries would change |
| `recover_from_transcripts.py` | Reconstructs source files from Claude Code transcripts (how v3 was partly recovered) |

The three `archive_*` scripts read the production dump and are meant to run **on the VPS**:

```bash
scp tools/archive_source_stats.py truthevades:/tmp/
ssh truthevades 'python3 /tmp/archive_source_stats.py'
```

`probe_ais_coverage.py` reads `AISSTREAM_API_KEY` from `../.env` and needs `pip install websockets`.
Run it outside the backend container — a `docker compose restart` kills an exec'd probe.
