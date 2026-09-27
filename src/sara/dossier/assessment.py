from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from ..understanding_vocabulary import DOSSIER_DOMAIN_SEED_V1, DOSSIER_POLICY_VERSION
from .core import DossierQueryError, parse_timestamp
from .surface import build_business_dossier


class DossierAssessmentError(RuntimeError):
    """A persisted dossier assessment cannot be computed or stored safely."""


DERIVATION_VERSION = "dossier-assessment-v1"
_VALUE_STATUSES = frozenset({"confirmed", "single_source"})
_READY_STATES = frozenset({"sufficient", "strong", "not_applicable"})
_CAPABILITY_PREDICATES = (
    "capability.online_booking",
    "capability.online_ordering",
    "capability.whatsapp",
)


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


def _fresh_value(fact: dict[str, Any]) -> bool:
    return (
        fact["status"] in _VALUE_STATUSES
        and not bool(fact["freshness"]["is_stale"])
    )


def _absence_inspection_current(fact: dict[str, Any]) -> bool:
    return (
        fact["status"] == "not_observed"
        and not bool(fact["freshness"]["is_stale"])
        and any(
            support["support_role"] == "supports_absence"
            for support in fact["acquisition_support"]
        )
    )


def _usable_support_sources(fact: dict[str, Any]) -> set[str]:
    return {
        str(item["source_id"])
        for item in fact["observation_support"]
        if item["support_role"] == "supports" and item["evidence_status"] == "usable"
    }


def _all_corroborated(facts: list[dict[str, Any]]) -> bool:
    return bool(facts) and all(
        fact["status"] == "confirmed" and len(_usable_support_sources(fact)) >= 2
        for fact in facts
    )


def _domain_context(
    dossier: dict[str, Any], domain: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    facts = [fact for fact in dossier["facts"] if fact["domain"] == domain]
    unknowns = [item for item in dossier["unknowns"] if item["domain"] == domain]
    fresh = [fact for fact in facts if _fresh_value(fact)]
    return facts, unknowns, fresh


def _reason(
    *,
    code: str,
    facts: list[dict[str, Any]],
    unknowns: list[dict[str, Any]],
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "derivation_version": DERIVATION_VERSION,
        "code": code,
        "fact_ids": sorted(str(fact["id"]) for fact in facts),
        "unresolved_count": len(unknowns),
    }
    if extra:
        result.update(extra)
    return result


def _predicate_facts(
    dossier: dict[str, Any], predicate: str, *, subject_id: str | None = None
) -> list[dict[str, Any]]:
    return [
        fact
        for fact in dossier["facts"]
        if fact["predicate"] == predicate
        and (subject_id is None or fact["subject_id"] == subject_id)
    ]


def _generic_domain(
    dossier: dict[str, Any], domain: str
) -> tuple[str, dict[str, Any]]:
    facts, unknowns, fresh = _domain_context(dossier, domain)
    if any(fact["status"] == "conflicted" for fact in facts):
        return "conflicted", _reason(
            code="current_fact_conflict", facts=facts, unknowns=unknowns
        )
    if facts and not fresh and any(
        fact["status"] == "stale" or fact["freshness"]["is_stale"] for fact in facts
    ):
        return "stale", _reason(
            code="no_fresh_value_fact", facts=facts, unknowns=unknowns
        )
    if facts and all(fact["status"] == "not_applicable" for fact in facts) and not unknowns:
        return "not_applicable", _reason(
            code="all_current_domain_facts_not_applicable", facts=facts, unknowns=unknowns
        )
    if fresh and not unknowns:
        return (
            "strong" if _all_corroborated(fresh) else "sufficient",
            _reason(code="fresh_supported_domain_evidence", facts=facts, unknowns=unknowns),
        )
    if fresh:
        return "partial", _reason(
            code="fresh_evidence_with_unresolved_items", facts=facts, unknowns=unknowns
        )
    if facts or unknowns:
        return "insufficient", _reason(
            code="domain_has_no_fresh_supported_value", facts=facts, unknowns=unknowns
        )
    return "not_started", _reason(code="no_domain_evidence", facts=facts, unknowns=unknowns)


def _single_predicate_domain(
    dossier: dict[str, Any], domain: str, predicate: str
) -> tuple[str, dict[str, Any]]:
    facts, unknowns, _fresh = _domain_context(dossier, domain)
    if any(fact["status"] == "conflicted" for fact in facts):
        return "conflicted", _reason(
            code="required_predicate_conflicted",
            facts=facts,
            unknowns=unknowns,
            extra={"required_predicate": predicate},
        )
    matches = _predicate_facts(dossier, predicate)
    supported = [fact for fact in matches if _fresh_value(fact)]
    if supported:
        return (
            "strong" if _all_corroborated(supported) else "sufficient",
            _reason(
                code="required_predicate_supported",
                facts=facts,
                unknowns=unknowns,
                extra={"required_predicate": predicate},
            ),
        )
    if matches and any(
        fact["status"] == "stale" or fact["freshness"]["is_stale"] for fact in matches
    ):
        return "stale", _reason(
            code="required_predicate_stale",
            facts=facts,
            unknowns=unknowns,
            extra={"required_predicate": predicate},
        )
    if facts:
        return "insufficient", _reason(
            code="required_predicate_not_supported",
            facts=facts,
            unknowns=unknowns,
            extra={"required_predicate": predicate},
        )
    return "not_started", _reason(
        code="required_predicate_not_observed",
        facts=facts,
        unknowns=unknowns,
        extra={"required_predicate": predicate},
    )


def _identity_domain(dossier: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    facts, unknowns, _fresh = _domain_context(dossier, "identity")
    names = [
        fact
        for fact in _predicate_facts(dossier, "business.name.trading")
        if _fresh_value(fact)
    ]
    if any(fact["status"] == "conflicted" for fact in facts):
        return "conflicted", _reason(
            code="identity_fact_conflict", facts=facts, unknowns=unknowns
        )
    strong_identifiers = sorted(
        {
            f"{identifier['source_id']}:{identifier['namespace']}:{identifier['value']}"
            for location in dossier["locations"]
            if location["current_for_entity"]
            for identifier in location["external_identifiers"]
            if identifier["status"] == "active"
            and identifier["namespace"] in {"place_id", "cid", "data_id"}
        }
    )
    if names and strong_identifiers:
        return (
            "strong" if _all_corroborated(names) else "sufficient",
            _reason(
                code="fresh_name_and_strong_location_identity_anchor",
                facts=facts,
                unknowns=unknowns,
                extra={"strong_external_identifiers": strong_identifiers},
            ),
        )
    if names:
        return "partial", _reason(
            code="fresh_name_without_strong_location_identity_anchor",
            facts=facts,
            unknowns=unknowns,
        )
    if facts and any(
        fact["status"] == "stale" or fact["freshness"]["is_stale"] for fact in facts
    ):
        return "stale", _reason(code="identity_evidence_stale", facts=facts, unknowns=unknowns)
    return (
        "insufficient" if facts or unknowns else "not_started",
        _reason(code="identity_not_resolved", facts=facts, unknowns=unknowns),
    )


def _locations_domain(dossier: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    facts, unknowns, _fresh = _domain_context(dossier, "locations")
    current_locations = [
        str(item["id"]) for item in dossier["locations"] if item["current_for_entity"]
    ]
    required = ("location.address", "location.latitude", "location.longitude")
    if any(fact["status"] == "conflicted" for fact in facts):
        return "conflicted", _reason(
            code="location_fact_conflict", facts=facts, unknowns=unknowns
        )
    if not current_locations:
        return "insufficient", _reason(
            code="no_current_location_subject",
            facts=facts,
            unknowns=unknowns,
            extra={"required_predicates": list(required)},
        )
    missing: list[dict[str, str]] = []
    basis: list[dict[str, Any]] = []
    for location_id in current_locations:
        for predicate in required:
            matches = [
                fact
                for fact in _predicate_facts(dossier, predicate, subject_id=location_id)
                if _fresh_value(fact)
            ]
            if matches:
                basis.extend(matches)
            else:
                missing.append({"location_id": location_id, "predicate": predicate})
    if not missing:
        return (
            "strong" if _all_corroborated(basis) else "sufficient",
            _reason(
                code="all_current_locations_have_core_geography",
                facts=facts,
                unknowns=unknowns,
                extra={"current_location_ids": current_locations, "required_predicates": list(required)},
            ),
        )
    if basis:
        return "partial", _reason(
            code="current_location_geography_incomplete",
            facts=facts,
            unknowns=unknowns,
            extra={"missing": missing},
        )
    if facts and any(
        fact["status"] == "stale" or fact["freshness"]["is_stale"] for fact in facts
    ):
        return "stale", _reason(
            code="current_location_geography_stale",
            facts=facts,
            unknowns=unknowns,
            extra={"missing": missing},
        )
    return "insufficient", _reason(
        code="current_location_geography_unresolved",
        facts=facts,
        unknowns=unknowns,
        extra={"missing": missing},
    )


def _communication_domain(dossier: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    facts, unknowns, _fresh = _domain_context(dossier, "communication")
    if any(fact["status"] == "conflicted" for fact in facts):
        return "conflicted", _reason(
            code="communication_fact_conflict", facts=facts, unknowns=unknowns
        )
    current_locations = [
        str(item["id"]) for item in dossier["locations"] if item["current_for_entity"]
    ]
    missing: list[str] = []
    basis: list[dict[str, Any]] = []
    for location_id in current_locations:
        phones = [
            fact
            for fact in _predicate_facts(dossier, "location.phone", subject_id=location_id)
            if _fresh_value(fact)
        ]
        if phones:
            basis.extend(phones)
        else:
            missing.append(location_id)
    if current_locations and not missing:
        return (
            "strong" if _all_corroborated(basis) else "sufficient",
            _reason(
                code="all_current_locations_have_public_phone",
                facts=facts,
                unknowns=unknowns,
                extra={"current_location_ids": current_locations},
            ),
        )
    if basis:
        return "partial", _reason(
            code="public_contact_incomplete_across_locations",
            facts=facts,
            unknowns=unknowns,
            extra={"missing_location_ids": missing},
        )
    if facts and any(
        fact["status"] == "stale" or fact["freshness"]["is_stale"] for fact in facts
    ):
        return "stale", _reason(code="public_contact_stale", facts=facts, unknowns=unknowns)
    return (
        "insufficient" if facts or unknowns else "not_started",
        _reason(
            code="no_fresh_public_contact",
            facts=facts,
            unknowns=unknowns,
            extra={"missing_location_ids": missing},
        ),
    )


def _digital_capabilities_domain(dossier: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    facts, unknowns, _fresh = _domain_context(dossier, "digital_capabilities")
    if any(fact["status"] == "conflicted" for fact in facts):
        return "conflicted", _reason(
            code="digital_capability_conflict", facts=facts, unknowns=unknowns
        )
    inspected: list[str] = []
    missing: list[str] = []
    basis: list[dict[str, Any]] = []
    bounded_not_observed: list[str] = []
    for predicate in _CAPABILITY_PREDICATES:
        matches = _predicate_facts(dossier, predicate)
        supported = [fact for fact in matches if _fresh_value(fact)]
        searched = [fact for fact in matches if _absence_inspection_current(fact)]
        if supported:
            inspected.append(predicate)
            basis.extend(supported)
        elif searched:
            inspected.append(predicate)
            bounded_not_observed.append(predicate)
        else:
            missing.append(predicate)
    if not missing:
        state = "sufficient"
        if not bounded_not_observed and _all_corroborated(basis):
            state = "strong"
        return state, _reason(
            code="core_digital_capabilities_inspected",
            facts=facts,
            unknowns=unknowns,
            extra={
                "inspected_predicates": inspected,
                "bounded_not_observed_predicates": bounded_not_observed,
                "semantic_note": "not_observed records bounded inspection, not confirmed absence",
            },
        )
    if inspected:
        return "partial", _reason(
            code="digital_capability_inspection_incomplete",
            facts=facts,
            unknowns=unknowns,
            extra={"inspected_predicates": inspected, "missing_predicates": missing},
        )
    if facts and any(
        fact["status"] == "stale" or fact["freshness"]["is_stale"] for fact in facts
    ):
        return "stale", _reason(
            code="digital_capability_evidence_stale", facts=facts, unknowns=unknowns
        )
    return (
        "insufficient" if facts or unknowns else "not_started",
        _reason(
            code="digital_capabilities_not_inspected",
            facts=facts,
            unknowns=unknowns,
            extra={"missing_predicates": missing},
        ),
    )


def _reputation_domain(
    dossier: dict[str, Any], evaluated_at: datetime
) -> tuple[str, dict[str, Any]]:
    facts, unknowns, _fresh = _domain_context(dossier, "reputation")
    if any(fact["status"] == "conflicted" for fact in facts):
        return "conflicted", _reason(
            code="reputation_metric_conflict", facts=facts, unknowns=unknowns
        )
    rating = [
        fact for fact in _predicate_facts(dossier, "reputation.rating") if _fresh_value(fact)
    ]
    review_count = [
        fact
        for fact in _predicate_facts(dossier, "reputation.review_count")
        if _fresh_value(fact)
    ]
    voice = list(dossier["customer_voice"]["reviews"])
    current_voice: list[dict[str, Any]] = []
    for review in voice:
        retrieved = review["evidence"].get("retrieved_at")
        if not isinstance(retrieved, str):
            continue
        retrieved_at = parse_timestamp(
            retrieved,
            field=f"review evidence {review['evidence']['id']} retrieved_at",
        )
        age_days = (evaluated_at - retrieved_at).total_seconds() / 86400
        if 0 <= age_days <= 30:
            current_voice.append(review)
    if rating and review_count and current_voice:
        return "sufficient", _reason(
            code="current_platform_metrics_and_customer_voice_present",
            facts=facts,
            unknowns=unknowns,
            extra={
                "current_review_observation_count": len(current_voice),
                "total_review_observation_count": len(voice),
                "review_retrieval_freshness_days": 30,
            },
        )
    if voice or rating or review_count:
        return "partial", _reason(
            code="reputation_evidence_incomplete",
            facts=facts,
            unknowns=unknowns,
            extra={
                "has_fresh_rating": bool(rating),
                "has_fresh_review_count": bool(review_count),
                "current_review_observation_count": len(current_voice),
                "total_review_observation_count": len(voice),
            },
        )
    if facts and any(
        fact["status"] == "stale" or fact["freshness"]["is_stale"] for fact in facts
    ):
        return "stale", _reason(code="reputation_evidence_stale", facts=facts, unknowns=unknowns)
    return "not_started", _reason(
        code="no_reputation_or_customer_voice_evidence", facts=facts, unknowns=unknowns
    )


def _customer_journey_domain(dossier: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    facts, unknowns, _fresh = _domain_context(dossier, "customer_journey")
    supporting_predicates = {
        "business.model.transaction_type",
        "location.phone",
        "business.website.official",
        *_CAPABILITY_PREDICATES,
    }
    supporting = [
        fact
        for fact in dossier["facts"]
        if fact["predicate"] in supporting_predicates
        and (_fresh_value(fact) or _absence_inspection_current(fact))
    ]
    if any(fact["status"] == "conflicted" for fact in facts):
        return "conflicted", _reason(
            code="customer_journey_fact_conflict", facts=facts, unknowns=unknowns
        )
    if facts or supporting:
        return "partial", _reason(
            code="customer_interaction_evidence_exists_without_stage_reconstruction_contract",
            facts=facts,
            unknowns=unknowns,
            extra={"supporting_fact_ids": sorted(str(fact["id"]) for fact in supporting)},
        )
    return "not_started", _reason(
        code="no_customer_journey_reconstruction_evidence", facts=facts, unknowns=unknowns
    )


def _provenance_domain(dossier: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    facts, unknowns, _fresh = _domain_context(dossier, "provenance")
    traceable_items = len(dossier["facts"]) + int(dossier["customer_voice"]["review_count"])
    if traceable_items == 0:
        return "not_started", _reason(
            code="nothing_material_to_trace", facts=facts, unknowns=unknowns
        )
    if dossier["integrity_issues"]:
        return "insufficient", _reason(
            code="provenance_integrity_issues_present",
            facts=facts,
            unknowns=unknowns,
            extra={
                "integrity_issue_codes": sorted(
                    {str(item["code"]) for item in dossier["integrity_issues"]}
                )
            },
        )
    return "sufficient", _reason(
        code="material_current_state_traces_to_retained_evidence",
        facts=facts,
        unknowns=unknowns,
        extra={"traceable_item_count": traceable_items},
    )


def _unknowns_domain(dossier: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    facts, unknowns, _fresh = _domain_context(dossier, "unknowns")
    return "sufficient", _reason(
        code="controlled_fact_unknowns_enumerated_without_invented_values",
        facts=facts,
        unknowns=unknowns,
        extra={"controlled_unresolved_count": len(dossier["unknowns"])},
    )


def derive_domain_assessments(dossier: dict[str, Any]) -> list[dict[str, Any]]:
    evaluated_at = parse_timestamp(dossier["evaluated_at"], field="dossier evaluated_at")
    derived: dict[str, tuple[str, dict[str, Any]]] = {
        "identity": _identity_domain(dossier),
        "classification": _single_predicate_domain(
            dossier, "classification", "business.category.primary"
        ),
        "locations": _locations_domain(dossier),
        "offerings": _single_predicate_domain(
            dossier, "offerings", "business.offering.service"
        ),
        "customer_market": _single_predicate_domain(
            dossier, "customer_market", "business.customer_segment.stated"
        ),
        "business_model": _single_predicate_domain(
            dossier, "business_model", "business.model.transaction_type"
        ),
        "communication": _communication_domain(dossier),
        "digital_presence": _single_predicate_domain(
            dossier, "digital_presence", "business.website.official"
        ),
        "digital_capabilities": _digital_capabilities_domain(dossier),
        "customer_journey": _customer_journey_domain(dossier),
        "reputation": _reputation_domain(dossier, evaluated_at),
        "competitive_context": (
            "not_started",
            _reason(
                code="peer_context_not_yet_established_by_supported_dossier_inputs",
                facts=[],
                unknowns=[],
            ),
        ),
        "provenance": _provenance_domain(dossier),
        "unknowns": _unknowns_domain(dossier),
    }
    for domain in ("scale", "marketing", "technology", "people", "operations", "change"):
        derived[domain] = _generic_domain(dossier, domain)

    result: list[dict[str, Any]] = []
    for seed in DOSSIER_DOMAIN_SEED_V1:
        state, reason = derived[seed.name]
        domain_facts, domain_unknowns, domain_fresh = _domain_context(dossier, seed.name)
        result.append(
            {
                "domain": seed.name,
                "mandatory_for_initial_analysis": bool(seed.mandatory_for_initial_analysis),
                "state": state,
                "reason": reason,
                "fact_count": len(domain_facts),
                "fresh_fact_count": len(domain_fresh),
                "unresolved_count": len(domain_unknowns),
            }
        )
    return result


def _input_watermark(dossier: dict[str, Any]) -> str:
    candidates: list[datetime] = []

    def add(value: object, field: str) -> None:
        if value in (None, ""):
            return
        candidates.append(parse_timestamp(value, field=field))

    entity = dossier["business_entity"]
    add(entity.get("updated_at"), "business entity updated_at")
    for location in dossier["locations"]:
        add(location.get("updated_at"), f"location {location['id']} updated_at")
    for business in dossier["maps_businesses"]:
        add(business.get("last_seen_at"), f"Maps business {business['id']} last_seen_at")
    for fact in dossier["facts"]:
        add(fact.get("valid_from"), f"fact {fact['id']} valid_from")
        add(fact.get("last_verified_at"), f"fact {fact['id']} last_verified_at")
        add(fact.get("reconciled_at"), f"fact {fact['id']} reconciled_at")
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


def _input_signature(
    dossier: dict[str, Any], domains: list[dict[str, Any]]
) -> dict[str, Any]:
    return {
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
            }
            for fact in dossier["facts"]
        ],
        "customer_voice": [
            {
                "observation_id": review["observation_id"],
                "source_location_id": review["source_location_id"],
                "canonical_location_id": review["canonical_location_id"],
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
                for key in ("domain", "predicate", "subject_id", "state", "fact_id", "reason")
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
    seal = conn.execute(
        "SELECT sealed_at FROM dossier_assessment_seals WHERE assessment_id=?",
        (assessment_id,),
    ).fetchone()
    if seal is None:
        raise DossierAssessmentError(
            f"existing deterministic dossier assessment {assessment_id} is unsealed"
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
    return str(row[4])


def persist_dossier_assessment(
    conn: sqlite3.Connection,
    *,
    business_id: int | None = None,
    canonical_key: str | None = None,
    entity_id: str | None = None,
    now: Callable[[], str] = _utc_now,
) -> DossierAssessmentResult:
    """Compute and seal one immutable dossier sufficiency snapshot.

    Current database state is frozen under one writer transaction. The operation
    performs no acquisition, migration, vocabulary seeding, synchronization, or
    external I/O. Customer reviews remain evidence-only inputs. A repeated run
    with the same logical input and freshness state reuses the same deterministic
    snapshot; a later freshness transition creates a new immutable snapshot even
    when the underlying factual watermark is unchanged.
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

    computed_at = _validated_timestamp(now(), field="dossier assessment time")
    try:
        conn.execute("BEGIN IMMEDIATE")
        dossier = build_business_dossier(
            conn,
            business_id=business_id,
            canonical_key=canonical_key,
            entity_id=entity_id,
            evaluated_at=computed_at,
        )
        entity = str(dossier["business_entity"]["id"])
        domains = derive_domain_assessments(dossier)
        mandatory = {
            seed.name for seed in DOSSIER_DOMAIN_SEED_V1 if seed.mandatory_for_initial_analysis
        }
        blocking = tuple(
            sorted(
                item["domain"]
                for item in domains
                if item["domain"] in mandatory and item["state"] not in _READY_STATES
            )
        )
        analysis_ready = not blocking and not dossier["integrity_issues"]
        facts_as_of = _input_watermark(dossier)
        if parse_timestamp(facts_as_of, field="facts_as_of") > parse_timestamp(
            computed_at, field="computed_at"
        ):
            raise DossierAssessmentError(
                "dossier input watermark is later than the assessment clock; refusing impossible chronology"
            )

        signature = _input_signature(dossier, domains)
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
            "customer_review_observation_count": int(dossier["customer_voice"]["review_count"]),
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
        conn.rollback()
        raise
