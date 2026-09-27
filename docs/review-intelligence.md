# Review Intelligence foundation

Sara's first Review Intelligence slice extracts customer-review records that are already retained inside the current Google Maps source snapshot. It is deliberately an evidence-normalization step, not a new network collector.

The command is:

```bash
sara-reviews --db data/sara.db --business-id 123 --pretty
```

or, using the current canonical Maps key:

```bash
sara-reviews --db data/sara.db --canonical-key 'place:...' --pretty
```

Exactly one Maps selector is required. Entity-only extraction is intentionally unsupported in this slice because review evidence is location-scoped and one Business Entity may own multiple locations.

## Source boundary

The extractor reads only the `user_reviews` and `user_reviews_extended` arrays already present in the selected business's current retained Maps `raw_json`. It performs no Google Maps request, Docker execution, website request, or other network action.

The current Maps row must have exact, usable Business Understanding evidence created by the current Maps backfill/synchronization contracts. The extractor binds the selected business, retained Maps evidence ID and content hash, legacy run, and the Maps evidence's frozen source-time Business Entity and Location lineage. The frozen Location must resolve through the current redirect chain to the selected current canonical Location. If the current Maps row has changed without a matching synchronization record, extraction fails closed and asks for `sara-maps-sync`.

The whole selection, evidence resolution, parsing, idempotency verification, and persistence path runs inside one `BEGIN IMMEDIATE` transaction. There is therefore no gap in which another writer can change the current Maps/Understanding identity between source selection and review persistence.

## Evidence and observation semantics

Review Intelligence adds one controlled predicate:

```text
reputation.customer_review
```

It is a `location` / `json` / `multi` predicate with reconciliation policy `evidence_only`.

The predicate is an additive vocabulary extension. Existing Business Understanding readers continue to require the already-deployed foundational vocabulary, so a database does not become unreadable merely because the Review Intelligence predicate has not yet been seeded. If the review predicate is present, existing readers still validate its definition and fail closed on semantic drift. Review extraction itself requires the extension and the `sara-reviews` CLI explicitly seeds/verifies it before writing review state.

Because `evidence_only` predicates are not supposed to produce current Facts, the Phase-5 dossier's controlled-unknown enumeration excludes them. `reputation.customer_review` is therefore not reported as a permanently unresolved factual attribute simply because no Fact row exists. The Phase-5 dossier also does not yet project review observations into its customer-voice output; a review-specific inspection/read model remains a separate slice.

For each retained review evidence record Sara preserves:

- source review identity when supplied by Google Maps;
- source/platform marker when supplied;
- rating and rating scale when available;
- original review text and translated text when available;
- source language and translated language when available;
- exact publication/update timestamps when supplied;
- the source's relative `When` string when no exact time is available;
- owner-response text, language, and exact response timestamps when available;
- the exact canonical JSON review sub-object in evidence metadata;
- source array/index paths showing where the review appeared;
- the immutable parent Maps evidence ID, content hash, source locator, artifact reference, and frozen source-time Entity/Location anchors.

The review observation and review-extraction session remain attached to the **source-time Location subject**, even if that Location later redirects to another canonical Location after Maps identity convergence. This follows Sara's historical-integrity rule: observations are not rewritten merely because canonical identity changes later. Current consumers resolve the historical subject through `knowledge_subjects.merged_into_subject_id` when they need current identity.

This also keeps extraction identity stable across convergence: a later Location merge does not create a second review session for the same retained parent Maps evidence. The command result reports both the source-time Location and the currently resolved canonical Location.

A source-time Location can retain its original immutable Business Entity owner even when its canonical Location later belongs to another provisional Business Entity. That historical owner is preserved as provenance; it is not required to equal the current canonical Entity owner.

The normalized observation intentionally does not repeat reviewer display name, profile-picture URL, or author URL. Those source fields remain available in the retained raw review evidence when present but are not promoted into Sara's normalized customer-voice semantics.

Evidence items use `source_role=customer_generated`. Owner replies remain explicitly nested as `owner_response`; they are not silently converted into customer statements or operational facts.

## Customer statements are not Facts

This slice creates **no `facts` rows and no fact-support links** from review observations.

A customer review is evidence of what a customer stated or reported. It is not automatically evidence that the described operational condition is objectively true. Future theme extraction may derive bounded observations from review evidence, but it must retain links to the reviews that support the theme and must not silently turn inference into a business Fact.

Likewise, an empty retained review array does not establish that the business has no reviews. Sara records a completed bounded extraction session with zero review evidence/observations and creates no `not_observed` or negative Fact.

## Identity and duplicate handling

When a stable source `review_id` exists, Sara uses it as the review identity. Without a stable ID, Sara creates a deterministic semantic fingerprint from the normalized review content.

Exact duplicate raw review objects repeated between `user_reviews` and `user_reviews_extended` are collapsed into one evidence item while retaining all source paths in metadata.

If the same source review ID appears with materially different raw representations in the same retained snapshot, Sara preserves the representations as separate evidence variants rather than choosing one silently. Reconciliation or supersession of review versions is outside this foundation slice.

## Idempotency

A review-extraction session is deterministic for:

- the retained parent Maps evidence;
- that evidence's frozen source-time Location;
- the review collector version.

Re-running the same extraction verifies the complete persisted session, evidence, and observations and returns an idempotent result without creating duplicate state. A later canonical Location convergence does not change that identity. Drift in persisted review provenance fails closed.

A later Maps snapshot receives a different parent evidence identity and therefore creates a separate extraction session. Historical review evidence remains immutable.

## Explicit non-scope

This foundation does **not** implement:

- `-extra-reviews` or any new Google Maps network acquisition;
- review-platform scraping beyond already-retained Maps evidence;
- sentiment scoring;
- positive/negative classification;
- theme extraction or summarization;
- reputation scores;
- gap or opportunity analysis;
- service recommendations;
- review-driven operational Facts;
- broad Business Entity aggregation across locations;
- dossier sufficiency promotion;
- a user-facing customer-voice projection of review observations.

A future bounded review-acquisition slice can request additional review evidence only after its own acquisition, completion, throttling, provenance, and operational-validation contract is reviewed. This foundation establishes the evidence semantics that such a collector must write into.
