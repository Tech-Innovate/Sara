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

This prints the grid rows, columns, cells and total planned searches (`cells × queries`). The planner mirrors the pinned upstream grid implementation: origins begin half a cell from each minimum edge and are emitted only while the origin remains inside the maximum edge. Very small boxes can therefore contain zero search origins for a chosen cell size; `collect` rejects such a grid instead of claiming a crawl completed.

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

Sara prints the exact Docker command before execution. Add `--dry-run` to inspect the intended command without starting Docker. Dry-run is non-mutating: it does not create or overwrite run files or database state.

Upstream telemetry is disabled by default for Sara-launched containers with `DISABLE_TELEMETRY=1`.

The collector mounts:

- the normalized run query snapshot read-only at `/queries.txt`;
- a named Playwright cache volume at `/opt`;
- the run output directory at `/out`;
- an optional proxy file read-only at `/run/secrets/gmaps-proxies`.

Before Docker starts a real crawl, Sara pre-creates the result file as the host user. On POSIX systems it uses mode `0600`. This prevents a rootful upstream container from creating a root-owned result file that the host-side Sara process cannot immediately ingest. The upstream resume sidecar can still be created as root with mode `0600`; when the host cannot read it directly, Sara reads only that file through the configured scraper image using a read-only bind mount, `--network none`, and `/bin/cat`.

Proxy credentials are never placed directly on the command line:

```bash
sara collect ... --proxy-file /secure/path/proxies.txt
```

## Run identity, completion, and resume safety

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

A successful Docker exit is not treated as proof that a crawl finished. The pinned upstream scraper can terminate gracefully with exit code `0` after receiving a signal. In resume mode, however, upstream persists deterministic IDs only for query/cell inputs whose discovery and discovered-result persistence have fully completed. Sara independently reproduces the expected deterministic input IDs for the planned grid and requires the resume sidecar's `completed_inputs` set to match exactly before canonical ingestion begins.

If Docker exits `0` but completion evidence is missing or incomplete, Sara preserves the raw JSONL and upstream resume state, marks the run `interrupted`, skips canonical ingestion, and returns an interruption result. Reuse the same ID with exactly the same configuration to continue:

```bash
sara collect ... --run-id <existing-run-id>
```

If the resume sidecar contains input IDs that do not belong to the current run, Sara fails closed rather than ingesting ambiguous output. A changed resume configuration is rejected before Docker starts. Completed run IDs are protected from accidental reuse. A resume sidecar without its corresponding results file is also rejected; Sara will not recreate an empty results file underneath existing completion evidence.

`collect` currently requires upstream resume mode; `--no-resume` is rejected because the pinned upstream process does not expose an equally strong independent completion receipt outside resume mode. A custom `--image` must preserve the v1.18.1 grid and resume-state contract or Sara's completion verification will fail closed.

Sara also places a per-run local lock beside the output so two processes cannot write the same results/resume files concurrently; a lock left by a dead local process is recognized as stale on the next attempt. The liveness check is platform-specific so the Windows implementation does not use `os.kill(pid, 0)`.

If a run ID has scraper output on disk but no corresponding database row, `collect` refuses to adopt those files silently. This prevents unrelated old output from becoming part of a new run.

## Coverage strategy

Use progressive passes instead of assuming one crawl is exhaustive:

1. 2 km cells for broad discovery.
2. 1 km cells as the normal comprehensive pass.
3. 0.5 km cells in dense areas if the additional unique-business yield remains worthwhile.
4. Add relevant category/query variants and measure their marginal gain.

The important metric is `new_businesses`, not raw result count. Stop tightening the grid when additional searches produce very few new canonical businesses.

Identity convergence is handled retroactively: if two provisional rows later prove to be the same business through strong identifiers, Sara merges them and refreshes historical `unique_seen` / `new_businesses` counts so the coverage history remains canonical. Conflicting non-empty strong identifiers are treated as an ingestion error rather than silently replacing canonical identity.

## Inspect coverage

```bash
sara stats
```

Each run records:

- `raw_records`: every JSONL row emitted by the scraper after a verified-complete run is ingested;
- `accepted_records`: rows accepted into canonical processing (duplicates included before canonical collapse);
- `out_of_bounds_records`: coordinate-bearing rows excluded by strict bounds;
- `unlocated_records`: rows without usable coordinates when strict bounds are enabled;
- `unidentified_records`: rows that could not be assigned even a fallback identity;
- `unique_seen`: canonical businesses associated with the run;
- `new_businesses`: canonical businesses whose earliest retained discovery is this run.

An interrupted run can have partial JSONL on disk while these database ingestion counters remain zero. Raw-file existence is evidence of acquisition progress, not evidence of a completed canonical run.

## Re-ingest a run

A recorded raw file can be re-ingested idempotently, for example after improving canonicalization logic:

```bash
sara ingest --run-id <run-id> --file output/<run-id>/results.jsonl
```

For provenance safety, the supplied file must resolve to the run's recorded `raw_path`. Re-ingestion also re-verifies the run's recorded resume completion evidence before touching canonical storage, so an interrupted or otherwise unverifiable raw file cannot bypass the collection-time completion gate. If acquisition completed but a prior canonical-ingestion attempt failed, a successful verified re-ingest promotes that run to `complete` while preserving the scraper exit-code evidence already recorded for the run.

Re-ingestion uses the run's recorded strict-bounds policy and is blocked while the same run is actively collecting. Historical re-ingestion is chronology-aware: an older run can establish an earlier `first_run_id` / `first_seen_at`, but it cannot overwrite newer canonical business fields, `raw_json`, `last_run_id`, or `last_seen_at`.

## Data model

`businesses` is the canonical table. `run_businesses` records which businesses appeared in each accepted crawl result set. `runs` stores crawl configuration, lifecycle state, errors and coverage metrics.

Sara keeps the raw JSON object from the latest retained run for each canonical business, while promoting commonly used fields such as title, category, address, coordinates, phone, website, rating, review count and status into typed columns. The original run JSONL remains the raw evidence for rows that were excluded from canonical storage or superseded by later observations.

After completion evidence is verified, canonical ingestion, coverage metrics, and the transition to `status=complete` are committed in one SQLite transaction. Interrupts or failures before that commit roll back canonical mutations instead of exposing a partially ingested run as completed.

## Validation boundary

CI validates Sara's Python orchestration and state-management contracts on Ubuntu and Windows with Python 3.11 and 3.12, including configuration validation, command construction, query normalization, cross-platform run locking, resume provenance, exact upstream-compatible grid planning, deterministic completion IDs, streaming completion comparison, incomplete-run rejection, root-owned resume-sidecar handling, completion-gated re-ingestion, interrupt rollback, atomic lifecycle finalization, exit-code provenance, transactional ingestion, canonical identity convergence, chronology-aware re-ingestion, strong-identity conflict handling, output-file preparation, re-ingestion idempotence, and strict bounding-box accounting.

CI deliberately does **not** perform a live Google Maps scrape. A successful CI run therefore does not prove that Google's current page shape, anti-bot behavior, network path, proxy provider, the persistent `/opt` Playwright cache, or the pinned upstream scraper image will succeed together at collection time. Validate the first real crawl with a small representative query set before scaling the grid.

## Notes and current boundaries

- The upstream JSON writer emits one JSON object per line; Sara therefore treats scraper JSON output as JSONL.
- Grid search improves geographic coverage but cannot guarantee every Google Maps listing.
- Completion verification proves that the pinned upstream resume contract reports every planned query/cell input complete; it does not protect against later manual tampering with run files.
- Run rows written by older Sara revisions are not retroactively reclassified at database-open time. Collection and every re-ingest now enforce completion evidence prospectively; historical status labels should not be treated as newly verified merely because the software was upgraded.
- The `v1.18.1` image is pinned by version tag, not immutable registry digest; record/lock a digest separately if byte-for-byte container reproducibility is required.
- Completion-ID verification intentionally depends on v1.18.1-compatible grid/identity semantics. An overridden or retagged image with incompatible semantics will fail closed rather than be silently trusted.
- Upstream v1.18.1 rewrites, sorts, syncs, and atomically replaces the full `completed_inputs` resume-state list whenever another input completes. Very large single-run grids therefore incur increasing resume-state I/O; broad discovery should be partitioned into bounded geographic runs/tiles instead of one country-sized run.
- Sara streams expected completion IDs through the loaded upstream completion set, avoiding a second full expected-ID set during verification. The upstream resume sidecar itself remains an unavoidable per-run scaling cost.
- The upstream project currently recommends a persistent `gmaps-playwright-cache:/opt` volume even though v1.18.1 also contains its browser/driver under `/opt`. Sara follows the upstream invocation for now; validate an existing/stale cache volume during the first live smoke test before relying on it operationally.
- Fallback identity is heuristic when Google strong IDs are absent. Raw JSONL should be retained for later reconciliation.
- Keep extra review and email enrichment separate from discovery until the canonical place set is stable; otherwise overlapping cells multiply unnecessary network work.
- `data/` and `output/` are intentionally gitignored.
