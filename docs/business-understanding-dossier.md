# Business Understanding dossier inspection and assessment

Sara exposes two deliberately separate dossier operations:

- `sara-dossier` is a read-only inspection surface. It never acquires data, migrates schema, seeds vocabulary, synchronizes Maps businesses, creates assessments, or mutates records.
- `sara-dossier-assess` is an explicit bounded writer that computes and seals one policy-versioned dossier assessment from already-retained Business Understanding state. It performs no acquisition, migration, vocabulary seeding, Maps synchronization, or external I/O.

This separation keeps inspection safe while making sufficiency judgments explicit and auditable.

## Read-only usage

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

## Read model

The JSON result identifies its scopes explicitly:

- `fact_scope=current_only`: current facts for the canonical entity and its current locations;
- `evidence_scope=current_fact_provenance`: retained evidence reached through those current facts' support links;
- `customer_voice_scope=review_observations_from_selected_location_lineage_resolving_current`: retained Review Intelligence observations from the selected entity's Location lineage that still resolve to one of its current Locations;
- `unknown_scope=controlled_active_fact_predicates_on_current_subjects`: unresolved controlled predicates eligible for Fact reconciliation, plus explicit `unknown` and `not_observed` fact states.

Controlled predicates whose reconciliation policy is `evidence_only` are intentionally excluded from the Fact-unknown set. Review Intelligence's `reputation.customer_review` represents attributed customer evidence and is not expected to have a current Fact, so its absence from `facts` is not reported as an unresolved factual attribute.

### Customer voice

`customer_voice.reviews` projects the normalized review observation and bounded provenance needed to inspect the customer statement. It does **not** expose the review evidence item's raw `metadata_json`, because retained raw review metadata may contain reviewer display/profile identifiers that are not required for the dossier read model.

Review observations remain attached to their immutable source-time Location. The read model scopes review lookup to the selected entity's already-resolved Location lineage before decoding review state, then requires each source Location to resolve to a selected current Location. Unrelated businesses' review records therefore cannot contaminate a single-entity dossier.

Customer statements remain customer evidence. The dossier does not turn review text, rating, or owner response into an operational Fact about the business.

### Identity history

Locations carry `relationship_to_entity` so identity history remains inspectable:

- `current` is a canonical active Location owned by the selected entity;
- `historical_owned` is a Location historically owned by the selected entity but no longer current;
- `merged_alias` is a historical Location that now redirects into one of the entity's current Locations. Its immutable provider identifiers remain visible on that alias rather than being silently moved.

The surface preserves Sara's semantic distinctions:

- `unknown` means the fact cannot currently be established;
- `not_observed` is only reported when that explicit fact state exists; integrity checks require acquisition provenance and an absence-specific `supports_absence` support edge rather than treating generic research context as evidence of non-observation;
- confirmed `false` remains a supported value and is not converted into an unknown;
- `single_source` is checked against exactly one distinct usable supporting source; multiple observations from that same source still count as one source;
- a controlled Fact-eligible predicate with no current fact is reported as `unresolved`, not fabricated as an `unknown` fact.

Evidence observations include their asserted and normalized values so disagreements can be inspected rather than inferred from support-edge IDs alone. `integrity_issues` exposes broken joins, subject/predicate mismatches, selected-value mismatches, non-usable supporting evidence, malformed conflicts, source-count/status mismatches, missing absence provenance, and customer-review provenance problems rather than silently hiding them.

## Persisted dossier assessment

Run an assessment only after the database has already been migrated, vocabulary-seeded, and synchronized as required by the preceding Business Understanding phases:

```bash
sara-dossier-assess --db data/sara.db --business-id 123 --pretty
```

The assessment command accepts the same three selectors as `sara-dossier`. It opens an existing writable database but does not repair or prepare it implicitly.

The active policy is `business-understanding-v1`, with derivation implementation `dossier-assessment-v1`. An assessment records:

- immutable parent state in `dossier_assessments`;
- exactly one row for every controlled dossier domain in `dossier_domain_assessments`;
- domain state, deterministic reason JSON, fact count, and fresh-fact count;
- `facts_as_of`, the latest timestamped structural/factual/customer-voice input represented by the snapshot;
- `computed_at`, the actual policy-evaluation time;
- `analysis_ready` and the mandatory domains that block it;
- a deterministic input-signature hash and bounded claim ceiling in `summary_json`;
- a seal in `dossier_assessment_seals` after all rows pass foreign-key validation.

The writer holds one `BEGIN IMMEDIATE` transaction across identity resolution, dossier reading, policy derivation, idempotency verification, writes, and sealing. Failure rolls the entire assessment back.

### Determinism and history

The deterministic assessment identity excludes wall-clock time itself. Re-evaluating the same logical input while it remains in the same freshness state reuses the existing sealed snapshot rather than manufacturing duplicate history.

Time still matters semantically. If the same factual input later crosses a freshness threshold, the derived state changes and Sara creates a new immutable assessment even though `facts_as_of` may be unchanged. Older assessments remain sealed historical judgments.

The read-only dossier returns the chronologically latest complete sealed snapshot for the active policy. It does not silently recompute an old assessment after later facts or freshness conditions change.

## Dossier sufficiency policy v1

Sara does not represent understanding as one opaque percentage. Every controlled domain receives one of:

```text
not_started
insufficient
partial
sufficient
strong
stale
conflicted
not_applicable
```

The v1 assessment policy is intentionally conservative:

- `identity` requires a fresh trading-name fact plus at least one active strong Maps Location identifier (`place_id`, `cid`, or `data_id`);
- `classification` requires a fresh primary-category fact;
- `locations` requires current address, latitude, and longitude coverage for every current Location;
- `offerings` requires a fresh service/offering fact;
- `business_model` requires a fresh transaction-model fact;
- `communication` requires a fresh public phone for every current Location;
- `digital_presence` requires a fresh official-website fact;
- `digital_capabilities` requires bounded inspection of online booking, online ordering, and WhatsApp. A current `not_observed` fact counts as inspection only when it has `supports_absence` acquisition provenance; it never becomes confirmed absence;
- `reputation` requires current platform rating/review-count facts plus retained customer-review evidence retrieved within the policy's 30-day review-collection freshness window before it can become `sufficient`;
- `provenance` requires material current facts/customer voice to trace cleanly to retained evidence;
- `unknowns` evaluates whether controlled Fact-eligible unresolved state is explicitly enumerated rather than invented away.

`customer_journey` is deliberately capped below sufficiency in this policy because the current model does not yet have a supported stage-reconstruction contract. Adjacent phone, website, transaction, booking, ordering, or messaging evidence can make that domain `partial`, but cannot prove that the major observable journey was reconstructed.

`competitive_context` remains `not_started` until Sara has a supported peer-context representation. Name/category similarity is not treated as competitive understanding.

These two boundaries intentionally prevent `analysis_ready` from being manufactured before the evidence model supports the mandatory domains defined by the Business Understanding specification.

`strong` is not a synonym for “many observations.” Where the current policy permits it, it requires corroborated supported Facts rather than repeated observations from one source.

`analysis_ready` means sufficiently understood for the bounded downstream analysis represented by the active policy, not completely known. It is true only when every mandatory domain is in a ready state (`sufficient`, `strong`, or a legitimately `not_applicable` state) and the current dossier has no integrity issues.

## Read-only preview

The `sara-dossier` result still includes `read_only_preview`. It remains intentionally non-promoting: it can summarize `not_started`, `insufficient`, `partial`, `stale`, `conflicted`, or `not_applicable`, and it can now acknowledge retained customer voice, but it never emits `sufficient`, `strong`, or `analysis_ready=true`.

The preview is an inspection aid. Only the explicit assessment writer creates a sealed policy judgment.

## Non-scope

This assessment slice does not authorize or implement:

- new network acquisition or live `-extra-reviews` collection;
- review sentiment scoring or thematic extraction;
- peer-set construction or competitive scoring;
- gap detection, opportunity scoring, prospect/lead scoring, or service recommendations;
- automated outreach, CRM integration, or broad autonomous research.

Those remain downstream or separately authorized capabilities. The dossier assessment records what Sara can support now and what remains unresolved.

## Read-only guarantee

`sara-dossier` opens SQLite with Sara's `connect_readonly` path (`mode=ro` plus `query_only` where supported). It never calls schema migrations, vocabulary seeding, Maps synchronization, an acquisition collector, or the assessment writer. An older or unsynchronized database is rejected instead of repaired implicitly.
