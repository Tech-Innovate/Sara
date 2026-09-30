from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from ..migrations import MIGRATIONS, current_schema_version
from ..understanding_vocabulary import DOSSIER_DOMAIN_SEED_V1, DOSSIER_POLICY_VERSION
from .assessment_policy import DERIVATION_VERSION, derive_domain_assessments
from .core import DossierQueryError, parse_timestamp, resolve_subject, subject
from .surface import build_business_dossier


class DossierAssessmentError(RuntimeError):
    """A persisted dossier assessment cannot be computed or stored safely."""


_READY_STATES = frozenset({"sufficient", "strong", "not_applicable"})


@dataclass(frozen=True)
class DossierAssessmentResult:
    assessment_id: str
    business_entity_id: str
    policy_version: str
    facts_as_of: str
    computed_at: str
    analysis_ready: bool
    blocking_mandatory_domains: tuple[str, ...]
    domains: tuple[dict[str, Any], ...]
    already_assessed: bool


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _require_assessment_schema(conn: sqlite3.Connection) -> None:
    """Fail closed unless the schema carries every migration the writer reads.

    The assessment reads v2 columns (external_identifiers.status_changed_at);
    on a database migrated only through v1 that query would leak a raw
    storage error, so the version is checked before any dossier work.
    An unrecognized future schema version also refuses, mirroring the
    vocabulary seed contract.
    """
    version = current_schema_version(conn)
    known = {migration.version for migration in MIGRATIONS}
    if version not in known or version < 2:
        raise DossierAssessmentError(
            "dossier assessment requires Business Understanding schema v2 "
            "(external identifier status chronology); this database is at "
            f"schema version {version}"
        )


def _validated_timestamp(value: object, *, field: str) -> str:
    try:
        return parse_timestamp(value, field=field).isoformat()
    except DossierQueryError as exc:
        raise DossierAssessmentError(str(exc)) from exc


def _location_owner_resolution_state(
    conn: sqlite3.Connection,
    dossier: dict[str, Any],
) -> list[dict[str, Any]]:
    """Seal Entity redirects that admit Locations through immutable ownership.

    ``business_locations.business_entity_id`` is immutable source-time ownership.
    Some dossier rows are inbound Location aliases whose original owner does not
    resolve to the selected Entity; those rows are admitted by the Location
    redirect path instead, whose lifecycle is sealed separately. Only owner
    chains that actually resolve to the selected Entity participate here.
    """
    canonical_entity_id = str(dossier["business_entity"]["id"])
    result: list[dict[str, Any]] = []
    for location in dossier["locations"]:
        location_id = str(location["id"])
        owner_entity_id = str(location["business_entity_id"])
        try:
            resolved = resolve_subject(conn, owner_entity_id, "business_entity")
        except DossierQueryError as exc:
            raise DossierAssessmentError(
                f"cannot resolve owner Entity for dossier location {location_id}: {exc}"
            ) from exc
        canonical_owner_id = str(resolved["canonical"]["id"])
        if canonical_owner_id != canonical_entity_id:
            continue

        chain: list[dict[str, Any]] = []
        try:
            for owner_subject_id in resolved["chain"]:
                row = subject(conn, str(owner_subject_id), "business_entity")
                chain.append(
                    {
                        "id": str(row["id"]),
                        "record_state": row["record_state"],
                        "merged_into_subject_id": row["merged_into_subject_id"],
                        "created_at": row["created_at"],
                        "updated_at": row["updated_at"],
                        "merged_at": row["merged_at"],
                    }
                )
        except DossierQueryError as exc:
            raise DossierAssessmentError(
                f"cannot read owner Entity redirect state for dossier location {location_id}: {exc}"
            ) from exc
        result.append(
            {
                "location_id": location_id,
                "owner_business_entity_id": owner_entity_id,
                "canonical_business_entity_id": canonical_owner_id,
                "resolution_chain": chain,
            }
        )
    return sorted(result, key=lambda item: str(item["location_id"]))


def _structural_subject_state(
    conn: sqlite3.Connection,
    dossier: dict[str, Any],
) -> list[dict[str, Any]]:
    """Seal every Entity/Location subject that materially participates in resolution.

    This is assessment-only state. It intentionally does not change the dossier
    read model or Location-admission semantics. In particular, a historical
    cross-owner Location alias remains admitted through its Location redirect;
    its original owner chain is merely sealed as structural provenance.
    """
    expected_kinds: dict[str, str] = {}

    def add(subject_id: object, kind: str) -> None:
        if subject_id in (None, ""):
            return
        key = str(subject_id)
        previous = expected_kinds.get(key)
        if previous is not None and previous != kind:
            raise DossierAssessmentError(
                f"Understanding subject {key!r} participates with conflicting kinds "
                f"{previous!r} and {kind!r}"
            )
        expected_kinds[key] = kind

    def add_chain(values: object, kind: str) -> None:
        if not isinstance(values, (list, tuple)):
            return
        for value in values:
            add(value, kind)

    add(dossier["business_entity"]["id"], "business_entity")
    selection = dossier.get("selection", {})
    if isinstance(selection, dict):
        add_chain(selection.get("entity_resolution_chain"), "business_entity")
        add_chain(selection.get("location_resolution_chain"), "location")
        add(selection.get("linked_location_id"), "location")
        add(selection.get("canonical_location_id"), "location")

    for location in dossier["locations"]:
        add(location["id"], "location")
        add_chain(location.get("resolution_chain"), "location")
        owner_entity_id = str(location["business_entity_id"])
        try:
            owner = resolve_subject(conn, owner_entity_id, "business_entity")
        except DossierQueryError as exc:
            raise DossierAssessmentError(
                f"cannot resolve owner Entity for dossier location {location['id']}: {exc}"
            ) from exc
        add_chain(owner["chain"], "business_entity")

    for review in dossier["customer_voice"]["reviews"]:
        add(review.get("source_location_id"), "location")
        add(review.get("canonical_location_id"), "location")
        add_chain(review.get("location_resolution_chain"), "location")

    result: list[dict[str, Any]] = []
    try:
        for subject_id, kind in sorted(
            expected_kinds.items(), key=lambda item: (item[1], item[0])
        ):
            row = subject(conn, subject_id, kind)
            result.append(
                {
                    "id": subject_id,
                    "kind": kind,
                    "record_state": row["record_state"],
                    "merged_into_subject_id": row["merged_into_subject_id"],
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                    "merged_at": row["merged_at"],
                }
            )
    except DossierQueryError as exc:
        raise DossierAssessmentError(
            f"cannot read structural Understanding subject state: {exc}"
        ) from exc
    return result


def _provenance_record_state(
    conn: sqlite3.Connection,
    dossier: dict[str, Any],
) -> list[dict[str, Any]]:
    """Seal lifecycle chronology for material Maps and provenance storage rows.

    Storage creation/first-seen timestamps are assessment-only chronology.
    Keeping them here avoids widening the read-only dossier contract while
    preventing an assessment clock from predating records it evaluates.
    """
    maps_business_ids: set[int] = set()
    source_ids: set[str] = set()
    evidence_ids: set[str] = set()
    observation_ids: set[str] = set()

    def collect(value: object, target: set[str]) -> None:
        if value not in (None, ""):
            target.add(str(value))

    for business in dossier["maps_businesses"]:
        maps_business_ids.add(int(business["id"]))

    for location in dossier["locations"]:
        for identifier in location["external_identifiers"]:
            collect(identifier.get("source_id"), source_ids)

    for fact in dossier["facts"]:
        for support in fact["observation_support"]:
            collect(support.get("source_id"), source_ids)
            collect(support.get("evidence_id"), evidence_ids)
            collect(support.get("observation_id"), observation_ids)
        for support in fact["acquisition_support"]:
            collect(support.get("source_id"), source_ids)

    for evidence in dossier["evidence"]:
        collect(evidence.get("id"), evidence_ids)
        collect(evidence.get("source_id"), source_ids)
        for observation in evidence["observations"]:
            collect(observation.get("id"), observation_ids)

    for review in dossier["customer_voice"]["reviews"]:
        collect(review.get("observation_id"), observation_ids)
        evidence = review["evidence"]
        collect(evidence.get("id"), evidence_ids)
        collect(evidence.get("source_id"), source_ids)

    result: list[dict[str, Any]] = []
    for business_id in sorted(maps_business_ids):
        row = conn.execute(
            "SELECT id,first_seen_at FROM businesses WHERE id=?",
            (business_id,),
        ).fetchone()
        if row is None:
            raise DossierAssessmentError(
                f"material Maps business {business_id!r} disappeared during assessment"
            )
        result.append(
            {
                "kind": "maps_business",
                "id": int(row[0]),
                "first_seen_at": row[1],
            }
        )
    for source_id in sorted(source_ids):
        row = conn.execute(
            "SELECT id,source_type,created_at FROM sources WHERE id=?",
            (source_id,),
        ).fetchone()
        if row is None:
            raise DossierAssessmentError(
                f"material provenance source {source_id!r} disappeared during assessment"
            )
        result.append(
            {
                "kind": "source",
                "id": str(row[0]),
                "source_type": row[1],
                "created_at": row[2],
            }
        )
    for evidence_id in sorted(evidence_ids):
        row = conn.execute(
            "SELECT id,created_at FROM evidence_items WHERE id=?",
            (evidence_id,),
        ).fetchone()
        if row is None:
            raise DossierAssessmentError(
                f"material evidence item {evidence_id!r} disappeared during assessment"
            )
        result.append(
            {
                "kind": "evidence_item",
                "id": str(row[0]),
                "created_at": row[1],
            }
        )
    for observation_id in sorted(observation_ids):
        row = conn.execute(
            "SELECT id,created_at FROM observations WHERE id=?",
            (observation_id,),
        ).fetchone()
        if row is None:
            raise DossierAssessmentError(
                f"material observation {observation_id!r} disappeared during assessment"
            )
        result.append(
            {
                "kind": "observation",
                "id": str(row[0]),
                "created_at": row[1],
            }
        )
    return sorted(result, key=lambda item: (str(item["kind"]), str(item["id"])))


def _chronology_inputs(
    dossier: dict[str, Any],
    location_owner_resolution: list[dict[str, Any]],
    structural_subject_state: list[dict[str, Any]],
    provenance_record_state: list[dict[str, Any]],
) -> list[dict[str, object]]:
    """Return every timestamped logical input used to derive ``facts_as_of``.

    The returned representation is also sealed into deterministic assessment
    identity so a timestamp can never change beneath a later stable maximum and
    silently reuse an older assessment.
    """
    inputs: list[dict[str, object]] = []

    def add(value: object, field: str) -> None:
        if value in (None, ""):
            return
        inputs.append({"field": field, "value": value})

    entity = dossier["business_entity"]
    add(entity.get("created_at"), "business entity created_at")
    add(entity.get("updated_at"), "business entity updated_at")
    for node in structural_subject_state:
        subject_label = f"{node['kind']} subject {node['id']}"
        add(node.get("created_at"), f"{subject_label} created_at")
        add(node.get("updated_at"), f"{subject_label} updated_at")
        add(node.get("merged_at"), f"{subject_label} merged_at")
    for record in provenance_record_state:
        if record["kind"] == "maps_business":
            add(
                record.get("first_seen_at"),
                f"Maps business {record['id']} first_seen_at",
            )
        else:
            add(
                record.get("created_at"),
                f"{record['kind']} {record['id']} created_at",
            )
    for owner in location_owner_resolution:
        for node in owner["resolution_chain"]:
            owner_id = node["id"]
            add(
                node.get("created_at"),
                f"location {owner['location_id']} owner Entity {owner_id} created_at",
            )
            add(
                node.get("updated_at"),
                f"location {owner['location_id']} owner Entity {owner_id} updated_at",
            )
            add(
                node.get("merged_at"),
                f"location {owner['location_id']} owner Entity {owner_id} merged_at",
            )
    for location in dossier["locations"]:
        add(location.get("created_at"), f"location {location['id']} created_at")
        add(location.get("updated_at"), f"location {location['id']} updated_at")
        add(location.get("merged_at"), f"location {location['id']} merged_at")
        for identifier in location["external_identifiers"]:
            identifier_id = identifier["id"]
            # A NULL transition timestamp is a durable legacy/unknown chronology
            # marker for rows that were already non-active before migration v2.
            # The v2 trigger still requires every post-v2 status transition to
            # provide a strictly advancing timestamp.
            status_changed_at = identifier.get("status_changed_at")
            add(
                identifier.get("created_at"),
                f"external identifier {identifier_id} created_at",
            )
            add(
                identifier.get("first_observed_at"),
                f"external identifier {identifier_id} first_observed_at",
            )
            add(
                identifier.get("last_observed_at"),
                f"external identifier {identifier_id} last_observed_at",
            )
            add(
                status_changed_at,
                f"external identifier {identifier_id} status_changed_at",
            )
    for business in dossier["maps_businesses"]:
        business_id = business["id"]
        add(business.get("last_seen_at"), f"Maps business {business_id} last_seen_at")
        add(business.get("linked_at"), f"Maps business {business_id} linked_at")
    for fact in dossier["facts"]:
        add(fact.get("created_at"), f"fact {fact['id']} created_at")
        add(fact.get("valid_from"), f"fact {fact['id']} valid_from")
        add(fact.get("last_verified_at"), f"fact {fact['id']} last_verified_at")
        add(fact.get("reconciled_at"), f"fact {fact['id']} reconciled_at")
        for support in fact["acquisition_support"]:
            session_id = support["acquisition_session_id"]
            add(
                support.get("started_at"),
                f"fact {fact['id']} acquisition {session_id} started_at",
            )
            add(
                support.get("finished_at"),
                f"fact {fact['id']} acquisition {session_id} finished_at",
            )
    for evidence in dossier["evidence"]:
        evidence_id = evidence["id"]
        session_id = evidence["acquisition_session_id"]
        add(evidence.get("retrieved_at"), f"evidence {evidence_id} retrieved_at")
        add(evidence.get("published_at"), f"evidence {evidence_id} published_at")
        add(
            evidence.get("acquisition_started_at"),
            f"evidence {evidence_id} acquisition {session_id} started_at",
        )
        add(
            evidence.get("acquisition_finished_at"),
            f"evidence {evidence_id} acquisition {session_id} finished_at",
        )
        for observation in evidence["observations"]:
            add(
                observation.get("observed_at"),
                f"observation {observation['id']} observed_at",
            )
            add(
                observation.get("extracted_at"),
                f"observation {observation['id']} extracted_at",
            )
    for outcome in dossier["customer_voice"].get(
        "review_evidence_unavailable", ()
    ):
        add(
            outcome.get("finished_at"),
            f"review outcome {outcome['session_id']} finished_at",
        )
    for review in dossier["customer_voice"]["reviews"]:
        add(review.get("observed_at"), f"review {review['observation_id']} observed_at")
        add(review.get("extracted_at"), f"review {review['observation_id']} extracted_at")
        evidence = review["evidence"]
        evidence_id = evidence["id"]
        session_id = evidence["acquisition_session_id"]
        add(
            evidence.get("retrieved_at"),
            f"review evidence {evidence_id} retrieved_at",
        )
        add(
            evidence.get("published_at"),
            f"review evidence {evidence_id} published_at",
        )
        add(
            evidence.get("acquisition_started_at"),
            f"review evidence {evidence_id} acquisition {session_id} started_at",
        )
        add(
            evidence.get("acquisition_finished_at"),
            f"review evidence {evidence_id} acquisition {session_id} finished_at",
        )
    return sorted(
        inputs,
        key=lambda item: (str(item["field"]), str(item["value"])),
    )


def _input_watermark(
    dossier: dict[str, Any],
    location_owner_resolution: list[dict[str, Any]],
    structural_subject_state: list[dict[str, Any]],
    provenance_record_state: list[dict[str, Any]],
) -> str:
    """Return the latest timestamped input represented by the dossier snapshot."""
    chronology = _chronology_inputs(
        dossier,
        location_owner_resolution,
        structural_subject_state,
        provenance_record_state,
    )
    if not chronology:
        raise DossierAssessmentError(
            "dossier has no timestamped state from which to derive facts_as_of"
        )
    candidates = [
        parse_timestamp(item["value"], field=str(item["field"])) for item in chronology
    ]
    return max(candidates).astimezone(timezone.utc).isoformat()


def _observation_support_signature(fact: dict[str, Any]) -> list[dict[str, Any]]:
    rows = [
        {
            "support_role": support["support_role"],
            "observation_id": support["observation_id"],
            "evidence_id": support["evidence_id"],
            "source_id": support["source_id"],
            "evidence_status": support["evidence_status"],
            "observation_value_hash": support["observation_value_hash"],
            "acquisition_session_id": support["acquisition_session_id"],
            "collector_name": support["collector_name"],
            "collector_version": support["collector_version"],
            "acquisition_status": support["acquisition_status"],
            "acquisition_started_at": support["acquisition_started_at"],
            "acquisition_finished_at": support["acquisition_finished_at"],
        }
        for support in fact["observation_support"]
    ]
    return sorted(
        rows,
        key=lambda item: (
            str(item["support_role"]),
            str(item["observation_id"]),
            str(item["evidence_id"]),
            str(item["source_id"]),
        ),
    )


def _acquisition_support_signature(fact: dict[str, Any]) -> list[dict[str, Any]]:
    rows = [
        {
            "support_role": support["support_role"],
            "acquisition_session_id": support["acquisition_session_id"],
            "source_id": support["source_id"],
            "collector_name": support["collector_name"],
            "collector_version": support["collector_version"],
            "status": support["status"],
            "started_at": support["started_at"],
            "finished_at": support["finished_at"],
            "legacy_run_id": support["legacy_run_id"],
        }
        for support in fact["acquisition_support"]
    ]
    return sorted(
        rows,
        key=lambda item: (
            str(item["support_role"]),
            str(item["acquisition_session_id"]),
            str(item["source_id"]),
        ),
    )


def _location_signature(location: dict[str, Any]) -> dict[str, Any]:
    identifiers = [
        {
            "id": identifier["id"],
            "source_id": identifier["source_id"],
            "namespace": identifier["namespace"],
            "value": identifier["value"],
            "status": identifier["status"],
            "status_changed_at": identifier.get("status_changed_at"),
            "first_observed_at": identifier["first_observed_at"],
            "last_observed_at": identifier["last_observed_at"],
            "created_at": identifier["created_at"],
        }
        for identifier in location["external_identifiers"]
    ]
    return {
        "id": location["id"],
        "business_entity_id": location["business_entity_id"],
        "record_state": location["record_state"],
        "merged_into_subject_id": location["merged_into_subject_id"],
        "merged_at": location["merged_at"],
        "canonical_location_id": location["canonical_location_id"],
        "resolution_chain": list(location["resolution_chain"]),
        "current_for_entity": bool(location["current_for_entity"]),
        "relationship_to_entity": location["relationship_to_entity"],
        "external_identifiers": identifiers,
    }


def understanding_state_fingerprint(
    conn: sqlite3.Connection,
    *,
    entity_id: str,
    evaluated_at: str,
) -> dict[str, Any]:
    """Compute the assessment input signature over CURRENT Understanding state.

    This is the same signature that participates in deterministic
    assessment identity (``input_signature_sha256`` in the sealed
    summary), recomputed over the live dossier for the full resolved
    Entity+Location subject graph. A persisted assessment whose sealed
    signature differs from this value no longer represents current state,
    regardless of which collector or sync changed it. Corrupt state
    timestamps surface as ``unprovable`` rather than being ignored.
    """
    try:
        dossier = build_business_dossier(
            conn, entity_id=entity_id, evaluated_at=evaluated_at)
        location_owner_resolution = _location_owner_resolution_state(conn, dossier)
        structural_subject_state = _structural_subject_state(conn, dossier)
        provenance_record_state = _provenance_record_state(conn, dossier)
        domains = derive_domain_assessments(dossier)
        signature = _input_signature(
            dossier, domains, location_owner_resolution,
            structural_subject_state, provenance_record_state,
        )
        return {
            "unprovable": False,
            "input_signature_sha256": _sha256(_canonical_json(signature)),
        }
    except DossierQueryError as exc:
        return {"unprovable": True, "error": str(exc)}


def _input_signature(
    dossier: dict[str, Any],
    domains: list[dict[str, Any]],
    location_owner_resolution: list[dict[str, Any]],
    structural_subject_state: list[dict[str, Any]],
    provenance_record_state: list[dict[str, Any]],
) -> dict[str, Any]:
    """Capture all state that can change or materially support the v1 assessment."""
    return {
        "chronology_inputs": _chronology_inputs(
            dossier,
            location_owner_resolution,
            structural_subject_state,
            provenance_record_state,
        ),
        "location_owner_resolution": location_owner_resolution,
        "structural_subject_state": structural_subject_state,
        "provenance_record_state": provenance_record_state,
        "locations": [_location_signature(location) for location in dossier["locations"]],
        "facts": [
            {
                "id": fact["id"],
                "subject_id": fact["subject_id"],
                "predicate": fact["predicate"],
                "status": fact["status"],
                "value_hash": fact["value_hash"],
                "created_at": fact["created_at"],
                "valid_from": fact["valid_from"],
                "last_verified_at": fact["last_verified_at"],
                "reconciliation_version": fact["reconciliation_version"],
                "freshness": dict(fact["freshness"]),
                "observation_support": _observation_support_signature(fact),
                "acquisition_support": _acquisition_support_signature(fact),
            }
            for fact in dossier["facts"]
        ],
        "customer_voice": [
            {
                "observation_id": review["observation_id"],
                "source_location_id": review["source_location_id"],
                "canonical_location_id": review["canonical_location_id"],
                "location_resolution_chain": list(review["location_resolution_chain"]),
                "value_hash": review["value_hash"],
                "observed_at": review["observed_at"],
                "extracted_at": review["extracted_at"],
                "evidence_id": review["evidence"]["id"],
                "source_id": review["evidence"]["source_id"],
                "content_sha256": review["evidence"]["content_sha256"],
                "retrieved_at": review["evidence"]["retrieved_at"],
                "published_at": review["evidence"]["published_at"],
                "acquisition_session_id": review["evidence"]["acquisition_session_id"],
                "acquisition_target_subject_id": review["evidence"][
                    "acquisition_target_subject_id"
                ],
                "collector_name": review["evidence"]["collector_name"],
                "collector_version": review["evidence"]["collector_version"],
                "acquisition_status": review["evidence"]["acquisition_status"],
                "acquisition_started_at": review["evidence"]["acquisition_started_at"],
                "acquisition_finished_at": review["evidence"]["acquisition_finished_at"],
            }
            for review in dossier["customer_voice"]["reviews"]
        ],
        "customer_voice_unavailable": [
            {
                "session_id": outcome["session_id"],
                "target_subject_id": outcome["target_subject_id"],
                "finished_at": outcome["finished_at"],
                "source_evidence_id": outcome["source_evidence_id"],
                "source_content_sha256": outcome["source_content_sha256"],
            }
            for outcome in dossier["customer_voice"].get(
                "review_evidence_unavailable", ()
            )
        ],
        "unknowns": [
            {
                key: item.get(key)
                for key in (
                    "domain",
                    "predicate",
                    "subject_id",
                    "state",
                    "fact_id",
                    "reason",
                )
            }
            for item in dossier["unknowns"]
        ],
        "integrity_issues": dossier["integrity_issues"],
        "domain_outcomes": [
            {
                "domain": item["domain"],
                "state": item["state"],
                "reason": item["reason"],
                "fact_count": item["fact_count"],
                "fresh_fact_count": item["fresh_fact_count"],
                "unresolved_count": item["unresolved_count"],
            }
            for item in domains
        ],
    }


def _verify_existing_assessment(
    conn: sqlite3.Connection,
    *,
    assessment_id: str,
    expected_parent: tuple[object, ...],
    expected_domains: list[dict[str, Any]],
) -> str | None:
    """Verify an existing deterministic snapshot exactly or fail closed."""
    row = conn.execute(
        "SELECT business_entity_id,policy_version,facts_as_of,analysis_ready,computed_at,summary_json "
        "FROM dossier_assessments WHERE id=?",
        (assessment_id,),
    ).fetchone()
    if row is None:
        return None
    actual_parent = (row[0], row[1], row[2], row[3], row[5])
    if actual_parent != expected_parent:
        raise DossierAssessmentError(
            f"existing deterministic dossier assessment {assessment_id} has incompatible parent state"
        )

    stored_facts_as_of = _validated_timestamp(
        row[2], field=f"existing dossier assessment {assessment_id} facts_as_of"
    )
    stored_computed_at = _validated_timestamp(
        row[4], field=f"existing dossier assessment {assessment_id} computed_at"
    )
    if parse_timestamp(stored_facts_as_of, field="stored facts_as_of") > parse_timestamp(
        stored_computed_at, field="stored computed_at"
    ):
        raise DossierAssessmentError(
            f"existing deterministic dossier assessment {assessment_id} has impossible chronology"
        )

    seal = conn.execute(
        "SELECT sealed_at FROM dossier_assessment_seals WHERE assessment_id=?",
        (assessment_id,),
    ).fetchone()
    if seal is None:
        raise DossierAssessmentError(
            f"existing deterministic dossier assessment {assessment_id} is unsealed"
        )
    sealed_at = _validated_timestamp(
        seal[0], field=f"existing dossier assessment {assessment_id} sealed_at"
    )
    if parse_timestamp(sealed_at, field="stored sealed_at") < parse_timestamp(
        stored_computed_at, field="stored computed_at"
    ):
        raise DossierAssessmentError(
            f"existing deterministic dossier assessment {assessment_id} was sealed before computation"
        )

    rows = conn.execute(
        "SELECT domain,state,reason_json,fact_count,fresh_fact_count "
        "FROM dossier_domain_assessments WHERE assessment_id=? ORDER BY domain",
        (assessment_id,),
    ).fetchall()
    actual = [tuple(item) for item in rows]
    expected = sorted(
        (
            item["domain"],
            item["state"],
            _canonical_json(item["reason"]),
            item["fact_count"],
            item["fresh_fact_count"],
        )
        for item in expected_domains
    )
    if actual != expected:
        raise DossierAssessmentError(
            f"existing deterministic dossier assessment {assessment_id} domain state has drifted"
        )
    return stored_computed_at


def persist_dossier_assessment(
    conn: sqlite3.Connection,
    *,
    business_id: int | None = None,
    canonical_key: str | None = None,
    entity_id: str | None = None,
    now: Callable[[], str] = _utc_now,
) -> DossierAssessmentResult:
    """Compute and seal one immutable dossier sufficiency snapshot.

    Current database state and the assessment clock are frozen under one writer
    transaction. The operation performs no acquisition, migration, vocabulary
    seeding, synchronization, or external I/O. Customer reviews remain
    evidence-only inputs. A repeated run with the same logical input and
    freshness state reuses the same deterministic snapshot; a later freshness
    transition creates a new immutable snapshot even when the underlying factual
    watermark is unchanged.
    """
    if conn.in_transaction:
        raise DossierAssessmentError(
            "dossier assessment requires a connection with no active transaction"
        )
    conn.execute("PRAGMA foreign_keys=ON")
    if conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise DossierAssessmentError(
            "SQLite foreign-key enforcement must be enabled for dossier assessment"
        )

    _require_assessment_schema(conn)

    try:
        conn.execute("BEGIN IMMEDIATE")
        computed_at = _validated_timestamp(now(), field="dossier assessment time")
        dossier = build_business_dossier(
            conn,
            business_id=business_id,
            canonical_key=canonical_key,
            entity_id=entity_id,
            evaluated_at=computed_at,
        )
        entity = str(dossier["business_entity"]["id"])
        location_owner_resolution = _location_owner_resolution_state(conn, dossier)
        structural_subject_state = _structural_subject_state(conn, dossier)
        provenance_record_state = _provenance_record_state(conn, dossier)
        domains = derive_domain_assessments(dossier)
        mandatory = {
            seed.name
            for seed in DOSSIER_DOMAIN_SEED_V1
            if seed.mandatory_for_initial_analysis
        }
        blocking = tuple(
            sorted(
                item["domain"]
                for item in domains
                if item["domain"] in mandatory
                and item["state"] not in _READY_STATES
            )
        )
        analysis_ready = not blocking and not dossier["integrity_issues"]
        facts_as_of = _input_watermark(
            dossier,
            location_owner_resolution,
            structural_subject_state,
            provenance_record_state,
        )
        if parse_timestamp(facts_as_of, field="facts_as_of") > parse_timestamp(
            computed_at, field="computed_at"
        ):
            raise DossierAssessmentError(
                "dossier input watermark is later than the assessment clock; refusing impossible chronology"
            )

        signature = _input_signature(
            dossier,
            domains,
            location_owner_resolution,
            structural_subject_state,
            provenance_record_state,
        )
        identity_payload = {
            "business_entity_id": entity,
            "policy_version": DOSSIER_POLICY_VERSION,
            "facts_as_of": facts_as_of,
            "analysis_ready": analysis_ready,
            "blocking_mandatory_domains": list(blocking),
            "input_signature": signature,
        }
        assessment_id = "da_" + _sha256(_canonical_json(identity_payload))[:32]
        summary = {
            "schema": "sara-dossier-assessment-summary-v1",
            "derivation_version": DERIVATION_VERSION,
            "policy_version": DOSSIER_POLICY_VERSION,
            "fact_count": len(dossier["facts"]),
            "customer_review_observation_count": int(
                dossier["customer_voice"]["review_count"]
            ),
            "review_evidence_unavailable_count": len(
                dossier["customer_voice"].get("review_evidence_unavailable")
                or []
            ),
            "controlled_unresolved_count": len(dossier["unknowns"]),
            "integrity_issue_count": len(dossier["integrity_issues"]),
            "integrity_issue_codes": sorted(
                {str(item["code"]) for item in dossier["integrity_issues"]}
            ),
            "mandatory_domains": sorted(mandatory),
            "blocking_mandatory_domains": list(blocking),
            "input_signature_sha256": _sha256(_canonical_json(signature)),
            "claim_ceiling": (
                "sufficiently_understood_for_bounded_analysis"
                if analysis_ready
                else "not_analysis_ready"
            ),
        }
        summary_json = _canonical_json(summary)
        expected_parent = (
            entity,
            DOSSIER_POLICY_VERSION,
            facts_as_of,
            int(analysis_ready),
            summary_json,
        )

        existing_computed_at = _verify_existing_assessment(
            conn,
            assessment_id=assessment_id,
            expected_parent=expected_parent,
            expected_domains=domains,
        )
        if existing_computed_at is not None:
            conn.commit()
            return DossierAssessmentResult(
                assessment_id=assessment_id,
                business_entity_id=entity,
                policy_version=DOSSIER_POLICY_VERSION,
                facts_as_of=facts_as_of,
                computed_at=existing_computed_at,
                analysis_ready=analysis_ready,
                blocking_mandatory_domains=blocking,
                domains=tuple(domains),
                already_assessed=True,
            )

        existing_clocks = [
            parse_timestamp(
                _validated_timestamp(
                    row[0], field="existing dossier assessment computed_at"
                ),
                field="existing dossier assessment computed_at",
            )
            for row in conn.execute(
                "SELECT computed_at FROM dossier_assessments WHERE business_entity_id=?",
                (entity,),
            )
        ]
        if existing_clocks and parse_timestamp(
            computed_at, field="computed_at"
        ) <= max(existing_clocks):
            raise DossierAssessmentError(
                "a new dossier assessment snapshot must be computed strictly "
                "after every previously persisted assessment for this entity; "
                "refusing non-advancing assessment chronology"
            )
        conn.execute(
            "INSERT INTO dossier_assessments("
            "id,business_entity_id,policy_version,facts_as_of,analysis_ready,computed_at,summary_json"
            ") VALUES (?,?,?,?,?,?,?)",
            (
                assessment_id,
                entity,
                DOSSIER_POLICY_VERSION,
                facts_as_of,
                int(analysis_ready),
                computed_at,
                summary_json,
            ),
        )
        for item in domains:
            conn.execute(
                "INSERT INTO dossier_domain_assessments("
                "assessment_id,domain,state,reason_json,fact_count,fresh_fact_count"
                ") VALUES (?,?,?,?,?,?)",
                (
                    assessment_id,
                    item["domain"],
                    item["state"],
                    _canonical_json(item["reason"]),
                    item["fact_count"],
                    item["fresh_fact_count"],
                ),
            )
        conn.execute(
            "INSERT INTO dossier_assessment_seals(assessment_id,sealed_at) VALUES (?,?)",
            (assessment_id, computed_at),
        )
        violations = list(conn.execute("PRAGMA foreign_key_check"))
        if violations:
            raise DossierAssessmentError(
                f"foreign-key violations after dossier assessment: {violations!r}"
            )
        conn.commit()
        return DossierAssessmentResult(
            assessment_id=assessment_id,
            business_entity_id=entity,
            policy_version=DOSSIER_POLICY_VERSION,
            facts_as_of=facts_as_of,
            computed_at=computed_at,
            analysis_ready=analysis_ready,
            blocking_mandatory_domains=blocking,
            domains=tuple(domains),
            already_assessed=False,
        )
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise