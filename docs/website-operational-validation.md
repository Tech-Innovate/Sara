# Official Website Acquisition Operational Validation

This runbook defines the production-readiness gate for Sara's bounded official-website acquisition.

The gate is intentionally copy-first. It must never use the primary Sara database as its writable target.

## What the gate proves

A successful run demonstrates, on a representative database copy, that:

1. SQLite backup preserves the source database logically.
2. Business Understanding migration, vocabulary seeding, Maps backfill/synchronization, and website acquisition do not mutate legacy collection or recovery tables.
3. Every current Maps business retains exactly one Understanding location anchor.
4. Known branches of the same brand remain deliberately separated unless strong identity convergence justifies a merge.
5. Selected official websites can be acquired repeatedly with the bounded production collector.
6. Value facts retain a Fact → Observation → Evidence → Acquisition Session → Source chain.
7. `not_observed` facts have a completed acquisition session carrying `supports_absence`; they are not treated as confirmed absence.
8. Persisted `analysis_ready` dossier assessments do not have a mandatory domain below `sufficient`.
9. SQLite foreign keys remain clean.
10. A real Maps identity-convergence probe on a disposable second copy preserves pre-existing Understanding evidence.

A report is production-ready only when every required check passes.

## Representative sample selection

Choose the sample deliberately. The harness does not infer corporate structure from names or domains.

Provide:

- at least two known single-location Maps business IDs;
- at least one known multi-branch brand represented by two or more Maps business IDs;
- one merge pair whose two current Maps rows can be safely bridged by complementary strong identifiers such as a place ID on one row and a CID/data ID on the other.

Every selected acquisition target must already have a supported `business.website.official` fact. Domain discovery is outside this gate and outside the Phase-6 collector.

The merge pair is exercised only in `merge-probe.sqlite`; it never mutates the main validation copy and never mutates the source database.

## Command

```bash
sara-website-validate \
  --source-db /path/to/representative-sara.sqlite \
  --workspace /path/to/new-validation-workspace \
  --single-location-business-id 101 \
  --single-location-business-id 202 \
  --multi-branch-group 301,302,303 \
  --merge-pair 401,402 \
  --pretty
```

The workspace must be absent or empty. The command creates:

```text
validation.sqlite    writable operational-validation copy
merge-probe.sqlite   disposable copy used only for identity-merge survival

evidence/             retained official-website evidence for validation runs
report.json            machine-readable readiness report
```

By default, every representative website is acquired twice. The repeat run is deliberate: it exercises historical acquisition/session creation and reconciliation without requiring the collector to pretend that a new acquisition is a no-op.

## Interpreting the report

The report schema is:

```text
sara-website-operational-validation-v1
```

`production_ready=true` is emitted only when all checks pass. A non-ready report or CLI exit code `2` is a stop condition for production enablement.

Important failures include:

- any mutation of `runs`, `businesses`, `run_businesses`, or existing recovery tables during migration/acquisition;
- missing or duplicate Maps → Understanding anchors;
- accidental grouping of a declared multi-branch sample;
- failed representative website acquisition;
- missing fact provenance;
- `not_observed` without completed bounded acquisition support;
- an internally inconsistent persisted `analysis_ready` dossier assessment;
- foreign-key violations;
- evidence loss during the Maps identity-merge probe.

## Synthetic adverse HTTP behavior

Timeouts, malformed HTTP, truncated reads, response-size limits, robots blocking, `429`, `503`, `Retry-After`, redirect safety, DNS behavior, TLS failures, retry budgets, and pacing are deterministic CI responsibilities. Do not deliberately provoke third-party production websites to create those conditions during operational validation.

Operational validation complements those tests by exercising Sara's real database, evidence store, identity mappings, provenance graph, and repeated bounded acquisitions.

## Production enablement rule

Passing this gate authorizes consideration of primary-database migration/enablement. It does not authorize:

- autonomous broad web research;
- domain discovery;
- heuristic corporate merging;
- gap analysis or scoring;
- automated outreach or CRM behavior;
- parallel multi-process acquisition without a separately reviewed shared per-origin coordinator.
