# Sara

Sara is a local-first orchestration and canonical data layer for broad-coverage Google Maps collection. It runs the upstream `gosom/google-maps-scraper` Docker image locally, tracks each crawl, ingests newline-delimited JSON, deduplicates businesses across overlapping grid cells and repeated runs, and measures how many new businesses each pass discovers.

## Design

Sara deliberately keeps scraping and data ownership separate:

- **Acquisition:** pinned `gosom/google-maps-scraper:v1.18.1` Docker image.
- **Planning:** explicit geographic bounding box, cell size, query set, depth and concurrency.
- **Storage:** local SQLite database at `data/sara.db` by default.
- **Identity:** dedupe by `place_id`, then `cid`, then `data_id`, with a stable fallback hash.
- **Coverage:** every run records raw rows, unique businesses seen, and businesses that were new to the canonical database.
- **Raw data:** each crawl keeps its JSONL output under `output/<run_id>/`.

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

## Configure queries

Create `config/queries/businesses.txt`:

```text
dentist
restaurant
pharmacy
```

For grid collection, keep queries focused on the business type. The grid already supplies geography.

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

Sara prints the exact Docker command before execution. Add `--dry-run` to inspect it without starting Docker. Upstream telemetry is disabled by default for Sara-launched containers with `DISABLE_TELEMETRY=1`.

The collector mounts:

- the query file read-only at `/queries.txt`;
- a named Playwright cache volume at `/opt`;
- the run output directory at `/out`;
- an optional proxy file read-only at `/run/secrets/gmaps-proxies`.

Proxy credentials are never placed directly on the command line:

```bash
sara collect ... --proxy-file /secure/path/proxies.txt
```

### Resume an interrupted run

Every collection has a run ID. If Docker or the host is interrupted, reuse that ID so the same output directory and upstream resume sidecar are used:

```bash
sara collect ... --run-id <existing-run-id>
```

Completed run IDs are protected from accidental reuse.

## Coverage strategy

Use progressive passes instead of assuming one crawl is exhaustive:

1. 2 km cells for broad discovery.
2. 1 km cells as the normal comprehensive pass.
3. 0.5 km cells in dense areas if the additional unique-business yield remains worthwhile.
4. Add relevant category/query variants and measure their marginal gain.

The important metric is `new_businesses`, not raw result count. Stop tightening the grid when additional searches produce very few new canonical businesses.

## Inspect coverage

```bash
sara stats
```

Each run records:

- `raw_records`: every JSONL row emitted by the scraper;
- `unique_seen`: canonical businesses observed in that run;
- `new_businesses`: businesses not present in Sara before that run.

## Data model

`businesses` is the canonical table. `run_businesses` records which businesses appeared in each crawl. `runs` stores crawl configuration and coverage metrics.

Sara keeps the most recent raw JSON object for each canonical business, while promoting commonly used fields such as title, category, address, coordinates, phone, website, rating, review count and status into typed columns.

## Notes

- The upstream JSON writer emits one JSON object per line; Sara therefore treats scraper JSON output as JSONL.
- Grid search improves geographic coverage but cannot guarantee every Google Maps listing.
- Keep extra review and email enrichment separate from discovery until the canonical place set is stable; otherwise overlapping cells multiply unnecessary network work.
- `data/` and `output/` are intentionally gitignored.
