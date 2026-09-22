# Sara

Sara is a local-first orchestration and canonical data layer for broad-coverage Google Maps collection. It runs the upstream `gosom/google-maps-scraper` Docker image locally, tracks each crawl, ingests newline-delimited JSON, deduplicates businesses across overlapping grid cells and repeated runs, and measures how many new businesses each pass discovers.

## Design

Sara deliberately keeps scraping and data ownership separate:

- **Acquisition:** version-tag-pinned `gosom/google-maps-scraper:v1.18.1` Docker image.
- **Planning:** explicit geographic bounding box, cell size, query set, depth and concurrency.
- **Storage:** local SQLite database at `data/sara.db` by default.
- **Identity:** dedupe by `place_id`, then `cid`, then `data_id`, with a conservative fallback hash when strong IDs are absent.
- **Coverage:** every run records raw rows, accepted rows, excluded rows, unique businesses seen, and businesses that are new to the canonical database.
- **Raw data:** each crawl keeps its JSONL output and normalized query snapshot under `output/<run_id>/`.

No SaaS component is required.

## Requirements

- Python 3.11+
- Docker with the daemon running

## Install

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
# For development/tests:
pip install -e ".[dev]"
```

## Configure an area

Create an area file such as `config/areas/my-city.json`:

```json
{
  "name": "my-city",
  "bbox": {
    "min_lat": 21.45,
    "min_lon": 39.10,
    "max_lat": 21.75,
    "max_lon": 39.35
  }
}
```

The bounding box is `minLat,minLon,maxLat,maxLon`.

By default Sara uses the box twice: first to place grid search origins, then as a strict coordinate filter during ingestion. Results outside the rectangle are retained in raw JSONL but are not added to canonical storage. Records without coordinates are also retained raw and reported as `unlocated` rather than silently counted as in-area businesses.

Use `--include-out-of-bounds` only when you intentionally want all returned places canonicalized regardless of coordinates.

## Configure queries

Create `config/queries/businesses.txt`:

```text
# Comments and blank lines are ignored by Sara.
dentist
restaurant
pharmacy
```

For grid collection, keep queries focused on the business type. The grid already supplies geography.

Sara removes comments, blank lines and exact duplicate query lines, then writes `output/<run_id>/queries.txt`. That normalized snapshot is what the upstream scraper receives, so planning and execution use the same query set.

## Estimate a crawl before running it

```bash
sara plan \
  --area config/areas/example.json \
  --queries config/queries/example.txt \
  --cell-km 1.0
```

This prints the estimated grid rows, columns, cells and total planned searches (`cells × queries`).

## Run a local collection

```bash
sara collect \
  --area config/areas/example.json \
  --queries config/queries/example.txt \
  --cell-km 1.0 \
  --depth 5 \
  --concurrency 4 \
  --browser-pool-size 1 \
  --pages-per-browser 4 \
  --lang en
```

Sara prints the exact Docker command before execution. Add `--dry-run` to inspect it without starting Docker. Dry-run still writes the normalized query snapshot for the generated/provided run ID.

Upstream telemetry is disabled by default for Sara-launched containers with `DISABLE_TELEMETRY=1`.

The collector mounts:

- the normalized run query snapshot read-only at `/queries.txt`;
- a named Playwright cache volume at `/opt`;
- the run output directory at `/out`;
- an optional proxy file read-only at `/run/secrets/gmaps-proxies`.

Proxy credentials are never placed directly on the command line:

```bash
sara collect ... --proxy-file /secure/path/proxies.txt
```

## Run identity and resume safety

Every collection has a run ID. Generated IDs are safe path components; user-provided IDs may contain only letters, digits, `.`, `_` and `-` and are limited to 64 characters.

Sara fingerprints the complete crawl configuration used for resume safety, including:

- area and bounding box;
- normalized queries;
- cell size and depth;
- language and zoom;
- concurrency and browser capacity;
- scraper image tag;
- resume mode;
- strict-bounds mode;
- SHA-256 fingerprint of the proxy file when one is used.

If Docker or the host is interrupted, reuse the same ID with the same configuration:

```bash
sara collect ... --run-id <existing-run-id>
```

A changed resume configuration is rejected. Completed run IDs are protected from accidental reuse. Sara also places a per-run local lock beside the output so two processes cannot write the same results/resume files concurrently; a lock left by a dead local process is recognized as stale on the next attempt.

If a run ID has scraper output on disk but no corresponding database row, `collect` refuses to adopt those files silently. This prevents unrelated old output from becoming part of a new run.

## Coverage strategy

Use progressive passes instead of assuming one crawl is exhaustive:

1. 2 km cells for broad discovery.
2. 1 km cells as the normal comprehensive pass.
3. 0.5 km cells in dense areas if the additional unique-business yield remains worthwhile.
4. Add relevant category/query variants and measure their marginal gain.

The important metric is `new_businesses`, not raw result count. Stop tightening the grid when additional searches produce very few new canonical businesses.

Identity convergence is handled retroactively: if two provisional rows later prove to be the same business through strong identifiers, Sara merges them and refreshes historical `unique_seen` / `new_businesses` counts so the coverage history remains canonical.

## Inspect coverage

```bash
sara stats
```

Each run records:

- `raw_records`: every JSONL row emitted by the scraper;
- `accepted_records`: rows accepted into canonical processing (duplicates included before canonical collapse);
- `out_of_bounds_records`: coordinate-bearing rows excluded by strict bounds;
- `unlocated_records`: rows without usable coordinates when strict bounds are enabled;
- `unidentified_records`: rows that could not be assigned even a fallback identity;
- `unique_seen`: canonical businesses associated with the run;
- `new_businesses`: canonical businesses whose earliest retained discovery is this run.

## Re-ingest a run

A recorded raw file can be re-ingested idempotently, for example after improving canonicalization logic:

```bash
sara ingest --run-id <run-id> --file output/<run-id>/results.jsonl
```

For provenance safety, the supplied file must resolve to the run's recorded `raw_path`. Re-ingestion uses the run's recorded strict-bounds policy and is blocked while the same run is actively collecting.

## Data model

`businesses` is the canonical table. `run_businesses` records which businesses appeared in each accepted crawl result set. `runs` stores crawl configuration, lifecycle state, errors and coverage metrics.

Sara keeps the most recent raw JSON object for each canonical business, while promoting commonly used fields such as title, category, address, coordinates, phone, website, rating, review count and status into typed columns. The original run JSONL remains the immutable raw evidence for rows that were excluded from canonical storage.

## Notes and current boundaries

- The upstream JSON writer emits one JSON object per line; Sara therefore treats scraper JSON output as JSONL.
- Grid search improves geographic coverage but cannot guarantee every Google Maps listing.
- The `v1.18.1` image is pinned by version tag, not immutable registry digest; record/lock a digest separately if byte-for-byte container reproducibility is required.
- Fallback identity is heuristic when Google strong IDs are absent. Raw JSONL should be retained for later reconciliation.
- Keep extra review and email enrichment separate from discovery until the canonical place set is stable; otherwise overlapping cells multiply unnecessary network work.
- `data/` and `output/` are intentionally gitignored.
