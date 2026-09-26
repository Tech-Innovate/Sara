# Official Website Acquisition Operational Validation

This runbook defines the production-readiness gate for Sara's bounded official-website acquisition.

The gate is intentionally copy-first. It must never use the primary Sara database as its writable target. Run it against a representative, quiescent source snapshot so the report describes one coherent database state.

For the controlled Maps convergence probe, `--source-db` should be the original representative Sara snapshot before Business Understanding bootstrap. The harness creates and migrates its own disposable copies. Do not point the controlled probe at a separate prep copy that has already been backfilled into Understanding.

## Maps source-shape compatibility

Sara's canonical internal field remains `website`, and Business Understanding continues to use `business.website.official`.

The pinned `gosom/google-maps-scraper:v1.18.1` data seen in Sara's real retained records uses the upstream spelling `web_site`. The Maps source adapter therefore accepts either `web_site` or `website` and normalizes that value to Sara's canonical website field for ingestion and Understanding extraction. Raw source evidence is retained in its original shape.

If both spellings are present and their nonblank values disagree after surrounding whitespace is removed, ingestion/extraction fails closed rather than silently choosing one value. Existing legacy `businesses` rows are not rewritten merely to populate the denormalized `website` column during Understanding bootstrap; historical `raw_json.web_site` can support the website fact while the legacy-table preservation checks remain meaningful.

## What the gate proves

A successful run demonstrates, on a representative database copy, that:

1. SQLite backup preserves the source database logically.
2. Business Understanding migration, vocabulary seeding, Maps backfill/synchronization, and website acquisition do not mutate legacy collection or recovery rows, table definitions, indexes, foreign keys, or triggers.
3. Every current Maps business retains exactly one Understanding location anchor before and after website acquisition.
4. Known branches of the same brand remain deliberately separated before and after website acquisition unless strong identity convergence justifies a merge.
5. Selected official websites can be acquired repeatedly with the bounded production collector, with at least one representative acquisition completing successfully; partial outcomes remain visible rather than being treated as success-by-omission.
6. Validation-run acquisition sessions retain immutable configuration hashes, terminal lifecycle states, and database counters that agree with their persisted evidence/observations.
7. Every retained validation evidence artifact remains beneath the validation evidence root, exists on disk, and matches its stored SHA-256.
8. Value facts retain a Fact → Observation → Evidence → Acquisition Session → Source chain.
9. `not_observed` facts have a completed acquisition session carrying `supports_absence`; they are not treated as confirmed absence.
10. Current read-only dossiers can be generated for every representative business without integrity errors, and the Phase-5 preview does not claim `analysis_ready`.
11. Persisted `analysis_ready` dossier assessments do not have a mandatory domain below `sufficient`.
12. Declared single-location samples resolve to one current Maps business and one current Understanding location; historical merged location aliases do not count as extra current locations.
13. SQLite foreign keys remain clean.
14. A controlled Maps identity-convergence probe on a disposable second copy exercises Sara's actual canonicalization path, preserves pre-merge Understanding evidence and observation history, retains the duplicate location as a historical alias, and leaves post-merge Maps anchors, dossiers, and foreign keys valid.

A report is production-ready only when every required check passes.

## Representative sample selection

Choose the sample deliberately. The harness does not infer corporate structure from names or domains.

Provide:

- at least two known single-location Maps business IDs;
- at least one known multi-branch brand represented by two or more Maps business IDs;
- one real Maps business for `--merge-probe-business-id` that carries at least two non-empty strong provider identifiers from `place_id`, `cid`, and `data_id`.

Every selected acquisition target must already have a supported `business.website.official` fact. Domain discovery is outside this gate and outside the Phase-6 collector.

The merge-probe business is not asserted to be a natural duplicate. Only in `merge-probe.sqlite`, the harness partitions that one real business's strong identifiers into two temporary complementary Maps rows before Understanding bootstrap. It then creates separate Understanding anchors/evidence for those rows, reconnects them with the real combined source record through Sara's normal `upsert_business` path, and runs the normal Maps synchronizer. The source database and `validation.sqlite` are never mutated by this synthetic setup.

A legacy `--merge-pair A,B` option remains available when a genuine pair with non-conflicting complementary strong identifiers actually exists. It is not required or recommended merely to satisfy the gate, and strong-identifier conflicts are never relaxed to manufacture such a pair.

## Command

```bash
sara-website-validate \
  --source-db /path/to/original-representative-sara.sqlite \
  --workspace /path/to/new-validation-workspace \
  --single-location-business-id 101 \
  --single-location-business-id 202 \
  --multi-branch-group 301,302,303 \
  --merge-probe-business-id 401 \
  --pretty
```

If the console script is not installed in the active environment, the equivalent module invocation is:

```bash
python -m sara.website_validation_cli \
  --source-db /path/to/original-representative-sara.sqlite \
  --workspace /path/to/new-validation-workspace \
  --single-location-business-id 101 \
  --single-location-business-id 202 \
  --multi-branch-group 301,302,303 \
  --merge-probe-business-id 401 \
  --pretty
```

The workspace must be absent or empty. The command creates:

```text
validation.sqlite    writable operational-validation copy
merge-probe.sqlite   disposable copy used only for controlled identity convergence
evidence/             retained official-website evidence for validation runs
report.json            machine-readable readiness report
```

By default, every representative website is acquired twice. The repeat run is deliberate: it exercises historical acquisition/session creation and reconciliation without requiring the collector to pretend that a new acquisition is a no-op.

## Interpreting the report

The report schema is:

```text
sara-website-operational-validation-v1
```

The controlled probe records `probe_kind=controlled_complementary_identifier_partition`, the real source business ID, and the temporary synthetic duplicate ID. Those fields describe the validation setup; they are not evidence that the duplicate Maps rows existed naturally in production.

`production_ready=true` is emitted only when all checks pass. A non-ready report or CLI exit code `2` is a stop condition for production enablement. Ctrl-C is propagated as cancellation semantics and exits with code `130`; it is not converted into an ordinary readiness result.

Important failures include:

- any mutation of legacy collection/recovery rows or schemas during migration/acquisition;
- missing or duplicate Maps → Understanding anchors;
- accidental grouping of a declared multi-branch sample;
- no successful complete acquisition among the representative websites;
- validation session configuration/count drift;
- missing, escaped, or hash-mismatched retained website artifacts;
- missing fact provenance;
- `not_observed` without completed bounded acquisition support;
- dossier generation or provenance-integrity failures;
- an internally inconsistent persisted `analysis_ready` dossier assessment;
- foreign-key violations;
- a merge-probe business with fewer than two usable strong provider identifiers;
- evidence loss, rewritten observation subjects, missing historical aliases, or anchor/dossier corruption during the controlled Maps convergence probe.

Fatal setup/runtime errors after the workspace has been accepted produce a machine-readable `report.json` with `production_ready=false`. An already non-empty workspace is never silently overwritten.

## Synthetic adverse HTTP behavior

Timeouts, malformed HTTP, truncated reads, response-size limits, robots blocking, `429`, `503`, `Retry-After`, redirect safety, DNS behavior, TLS failures, retry budgets, and pacing are deterministic CI responsibilities. Do not deliberately provoke third-party production websites to create those conditions during operational validation.

Operational validation complements those tests by exercising Sara's real database, evidence store, identity mappings, provenance graph, dossier surface, retained artifacts, and repeated bounded acquisitions.

## Production enablement rule

Passing this gate authorizes consideration of primary-database migration/enablement. It does not authorize:

- autonomous broad web research;
- domain discovery;
- heuristic corporate merging;
- gap analysis or scoring;
- automated outreach or CRM behavior;
- parallel multi-process acquisition without a separately reviewed shared per-origin coordinator.
