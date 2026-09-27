from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

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


def _input_watermark(
    dossier: dict[str, Any],
    location_owner_resolution: list[dict[str, Any]],
) -> str:
    """Return the latest timestamped input represented by the dossier snapshot."""
    candidates: list[datetime] = []

    def add(value: object, field: str) -> None:
        if value in (None, ""):
            return
        candidates.append(parse_timestamp(value, field=field))

    entity = dossier["business_entity"]
    add(entity.get("updated_at"), "business entity updated_at")
    for owner in location_owner_resolution:
        for node in owner["resolution_chain"]:
            owner_id = node["id"]
            add(
                node.get("updated_at"),
                f"location {owner['location_id']} owner Entity {owner_id} updated_at",
            )
            add(
                node.get("merged_at"),
                f"location {owner['location_id']} owner Entity {owner_id} merged_at",
            )
    for location in dossier["locations"]:
        add(location.get("updated_at"), f"location {location['id']} updated_at")
        add(location.get("merged_at"), f"location {location['id']} merged_at")
        for identifier in location["external_identifiers"]:
            identifier_id = identifier["id"]
            add(
                identifier.get("first_observed_at"),
                f"external identifier {identifier_id} first_observed_at",
            )
            add(
                identifier.get("last_observed_at"),
                f"external identifier {identifier_id} last_observed_at",
            )
    for business in dossier["maps_businesses"]:
        add(business.get("last_seen_at"), f"Maps business {business['id']} last_seen_at")
    for fact in dossier["facts"]:
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
        add(evidence.get("retrieved_at"), f"evidence {evidence['id']} retrieved_at")
        for observation in evidence["observations"]:
            add(
                observation.get("observed_at"),
                f"observation {observation['id']} observed_at",
            )
            add(
                observation.get("extracted_at"),
                f"observation {observation['id']} extracted_at",
            )
    for review in dossier["customer_voice"]["reviews"]:
        add(review.get("observed_at"), f"review {review['observation_id']} observed_at")
        add(review.get("extracted_at"), f"review {review['observation_id']} extracted_at")
        add(
            review["evidence"].get("retrieved_at"),
            f"review evidence {review['evidence']['id']} retrieved_at",
        )
    if not candidates:
        raise DossierAssessmentError(
            "dossier has no timestamped state from which to derive facts_as_of"
        )
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
            "first_observed_at": identifier["first_observed_at"],
            "last_observed_at": identifier["last_observed_at"],
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


def _input_signature(
    dossier: dict[str, Any],
    domains: list[dict[str, Any]],
    location_owner_resolution: list[dict[str, Any]],
) -> dict[str, Any]:
    """Capture all state that can change or materially support the v1 assessment."""
    return {
        "location_owner_resolution": location_owner_resolution,
        "locations": [_location_signature(location) for location in dossier["locations"]],
        "facts": [
            {
                "id": fact["id"],
                "subject_id": fact["subject_id"],
                "predicate": fact["predicate"],
                "status": fact["status"],
                "value_hash": fact["value_hash"],
                "valid_from": fact["valid_from"],
                "last_verified_at": fact["last_verified_at"],
                "reconciliation_version": fact["reconciliation_version"],
                "is_stale": bool(fact["freshness"]["is_stale"]),
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
                "evidence_id": review["evidence"]["id"],
                "content_sha256": review["evidence"]["content_sha256"],
                "retrieved_at": review["evidence"]["retrieved_at"],
            }
            for review in dossier["customer_voice"]["reviews"]
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
        facts_as_of = _input_watermark(dossier, location_owner_resolution)
        if parse_timestamp(facts_as_of, field="facts_as_of") > parse_timestamp(
            computed_at, field="computed_at"
        ):
            raise DossierAssessmentError(
                "dossier input watermark is later than the assessment clock; refusing impossible chronology"
            )

        signature = _input_signature(dossier, domains, location_owner_resolution)
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
