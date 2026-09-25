# Business Understanding dossier inspection

Phase 5 adds a read-only inspection surface for Business Understanding state. It does not acquire data, migrate schema, seed vocabulary, synchronize Maps businesses, create dossier assessments, or mutate existing records.

## Usage

```bash
sara-dossier --db data/sara.db --business-id 123 --pretty
```

Exactly one selector is required:

```text
--business-id <current canonical Maps business id>
--canonical-key <current canonical Maps business key>
--entity-id <Business Understanding entity id>
```

Maps selectors require the business to have already been synchronized explicitly with `sara-maps-sync`. The dossier command never performs synchronization on the caller's behalf.

`--evaluated-at <timezone-aware ISO-8601 timestamp>` controls only freshness evaluation. Omitting it uses the current UTC time. This is not a historical time-travel query: facts are the records that are current in the database when the command runs.

## Output contract

The JSON result identifies its scopes explicitly:

- `fact_scope=current_only`: current facts for the canonical entity and its current locations;
- `evidence_scope=current_fact_provenance`: retained evidence reached through those current facts' support links;
- `unknown_scope=controlled_active_predicates_on_current_subjects`: unresolved controlled predicates plus explicit `unknown` and `not_observed` fact states.

The surface preserves Sara's semantic distinctions:

- `unknown` means the fact cannot currently be established;
- `not_observed` is only reported when that explicit fact state exists, and the integrity report flags a missing acquisition-support record;
- confirmed `false` remains a supported value and is not converted into an unknown;
- a controlled predicate with no current fact is reported as `unresolved`, not fabricated as an `unknown` fact.

`integrity_issues` exposes broken or semantically mismatched current-fact provenance rather than silently hiding it.

## Dossier status

If a sealed dossier assessment exists for the active dossier policy, the command returns the chronologically latest complete sealed snapshot. A sealed snapshot must contain every controlled dossier domain or the query fails closed.

Persisted assessments are reported as immutable historical snapshots. Phase 5 does not claim that an old `analysis_ready` value remains current after later fact changes.

The `read_only_preview` is intentionally conservative. It may expose states such as `not_started`, `insufficient`, `partial`, `stale`, `conflicted`, or `not_applicable`, but it never promotes a business to `sufficient`, `strong`, or `analysis_ready`. Exact sufficiency policy and persisted assessment computation remain separate work.

## Read-only guarantee

The CLI opens SQLite with Sara's `connect_readonly` path (`mode=ro` plus `query_only` where supported). It never calls schema migrations, vocabulary seeding, Maps synchronization, or an acquisition collector. An older or unsynchronized database is rejected instead of repaired implicitly.
