from __future__ import annotations

from datetime import datetime
from typing import Any

from ..understanding_vocabulary import DOSSIER_DOMAIN_SEED_V1
from .core import parse_timestamp

# v2: Customer Journey moved from permanent-partial to policy over the
# deterministic observable-journey reconstruction (identity-affecting:
# the reason objects seal this version, so old persisted assessments
# cannot masquerade as the new interpretation).
DERIVATION_VERSION = "dossier-assessment-v2"
_VALUE_STATUSES = frozenset({"confirmed", "single_source"})
_CAPABILITY_PREDICATES = (
    "capability.online_booking",
    "capability.online_ordering",
    "capability.whatsapp",
)
_GOOGLE_MAPS_SOURCE_ID = "src_google_maps"
_GOOGLE_MAPS_SOURCE_TYPE = "google_maps"


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
            and support.get("status") == "complete"
            and support.get("target_subject_id") == fact["subject_id"]
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
    if not facts:
        return "not_started", _reason(
            code="no_domain_evidence", facts=facts, unknowns=unknowns
        )
    if any(fact["status"] == "conflicted" for fact in facts):
        return "conflicted", _reason(
            code="current_fact_conflict", facts=facts, unknowns=unknowns
        )
    if not fresh and any(
        fact["status"] == "stale" or fact["freshness"]["is_stale"] for fact in facts
    ):
        return "stale", _reason(
            code="no_fresh_value_fact", facts=facts, unknowns=unknowns
        )
    if all(fact["status"] == "not_applicable" for fact in facts) and not unknowns:
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
    return "insufficient", _reason(
        code="domain_has_evidence_but_no_fresh_supported_value",
        facts=facts,
        unknowns=unknowns,
    )


def _single_predicate_domain(
    dossier: dict[str, Any], domain: str, predicate: str
) -> tuple[str, dict[str, Any]]:
    facts, unknowns, _fresh = _domain_context(dossier, domain)
    if not facts:
        return "not_started", _reason(
            code="required_predicate_not_observed",
            facts=facts,
            unknowns=unknowns,
            extra={"required_predicate": predicate},
        )
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
    return "insufficient", _reason(
        code="required_predicate_not_supported",
        facts=facts,
        unknowns=unknowns,
        extra={"required_predicate": predicate},
    )


def _identity_domain(dossier: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    facts, unknowns, _fresh = _domain_context(dossier, "identity")
    if not facts:
        return "not_started", _reason(
            code="identity_not_attempted", facts=facts, unknowns=unknowns
        )
    if any(fact["status"] == "conflicted" for fact in facts):
        return "conflicted", _reason(
            code="identity_fact_conflict", facts=facts, unknowns=unknowns
        )
    names = [
        fact
        for fact in _predicate_facts(dossier, "business.name.trading")
        if _fresh_value(fact)
    ]
    strong_identifiers = sorted(
        {
            f"{identifier['source_id']}:{identifier['namespace']}:{identifier['value']}"
            for location in dossier["locations"]
            if location["current_for_entity"]
            for identifier in location["external_identifiers"]
            if identifier["status"] == "active"
            and identifier["source_id"] == _GOOGLE_MAPS_SOURCE_ID
            and identifier["source_type"] == _GOOGLE_MAPS_SOURCE_TYPE
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
    if any(
        fact["status"] == "stale" or fact["freshness"]["is_stale"] for fact in facts
    ):
        return "stale", _reason(
            code="identity_evidence_stale", facts=facts, unknowns=unknowns
        )
    return "insufficient", _reason(
        code="identity_evidence_does_not_resolve_business",
        facts=facts,
        unknowns=unknowns,
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
                extra={
                    "current_location_ids": current_locations,
                    "required_predicates": list(required),
                },
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
    if not facts:
        return "not_started", _reason(
            code="no_public_contact_evidence", facts=facts, unknowns=unknowns
        )
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
    if any(
        fact["status"] == "stale" or fact["freshness"]["is_stale"] for fact in facts
    ):
        return "stale", _reason(
            code="public_contact_stale", facts=facts, unknowns=unknowns
        )
    return "insufficient", _reason(
        code="public_contact_evidence_not_supported",
        facts=facts,
        unknowns=unknowns,
        extra={"missing_location_ids": missing},
    )


def _digital_capabilities_domain(dossier: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    facts, unknowns, _fresh = _domain_context(dossier, "digital_capabilities")
    if not facts:
        return "not_started", _reason(
            code="digital_capabilities_not_inspected",
            facts=facts,
            unknowns=unknowns,
            extra={"missing_predicates": list(_CAPABILITY_PREDICATES)},
        )
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
    if any(
        fact["status"] == "stale" or fact["freshness"]["is_stale"] for fact in facts
    ):
        return "stale", _reason(
            code="digital_capability_evidence_stale", facts=facts, unknowns=unknowns
        )
    return "insufficient", _reason(
        code="digital_capability_evidence_without_valid_inspection",
        facts=facts,
        unknowns=unknowns,
        extra={"missing_predicates": missing},
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
    unavailable_outcomes = list(
        dossier["customer_voice"].get("review_evidence_unavailable") or []
    )
    unavailable_current: list[dict[str, Any]] = []
    for outcome in unavailable_outcomes:
        # R8-05: an outcome is current when its SOURCE is current —
        # measured from the retained snapshot's retrieval time, exactly
        # like customer-review observations — never from the extraction
        # wall-clock. Mining an old snapshot today does not make the
        # underlying source current.
        retrieved = outcome.get("source_retrieved_at")
        if not isinstance(retrieved, str):
            continue
        outcome_session_id = str(outcome.get("session_id"))
        source_retrieved_at = parse_timestamp(
            retrieved,
            field=(
                f"review outcome session "
                f"{outcome_session_id} source_retrieved_at"
            ),
        )
        outcome_age_days = (
            evaluated_at - source_retrieved_at
        ).total_seconds() / 86400
        if 0 <= outcome_age_days <= 30:
            unavailable_current.append(outcome)
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
    if rating and review_count and unavailable_current:
        # F-01: the retained snapshot was mined and contains zero review
        # records. Current platform metrics plus an explicit bounded
        # unavailability of customer voice satisfy reputation instead of
        # looping the same no-op extraction forever.
        return "sufficient", _reason(
            code="current_platform_metrics_review_evidence_explicitly_unavailable",
            facts=facts,
            unknowns=unknowns,
            extra={
                "review_evidence_unavailable_outcomes": len(unavailable_current),
                "review_retrieval_freshness_days": 30,
            },
        )
    if rating or review_count or current_voice:
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
    stale_metrics = any(
        fact["status"] == "stale" or fact["freshness"]["is_stale"] for fact in facts
    )
    expired_voice = bool(voice) and not current_voice
    if stale_metrics or expired_voice:
        return "stale", _reason(
            code="reputation_evidence_stale",
            facts=facts,
            unknowns=unknowns,
            extra={
                "has_stale_platform_metrics": stale_metrics,
                "expired_review_observation_count": len(voice) - len(current_voice),
                "total_review_observation_count": len(voice),
                "review_retrieval_freshness_days": 30,
            },
        )
    if facts or voice:
        return "insufficient", _reason(
            code="reputation_evidence_not_current_or_supported",
            facts=facts,
            unknowns=unknowns,
        )
    if unavailable_current:
        # No reputation evidence at all beyond the explicit outcome: the
        # review source is bounded-unavailable, nothing is assessable,
        # and no absence fact is fabricated.
        return "not_applicable", _reason(
            code="review_evidence_explicitly_unavailable",
            facts=facts,
            unknowns=unknowns,
            extra={
                "review_evidence_unavailable_outcomes": len(unavailable_current)
            },
        )
    return "not_started", _reason(
        code="no_reputation_or_customer_voice_evidence",
        facts=facts,
        unknowns=unknowns,
    )


def _customer_journey_domain(dossier: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Policy over the deterministic observable-journey reconstruction.

    The reconstruction is the understanding layer; this policy only maps
    it to a sufficiency state and never judges journey quality. Hard
    invariants carried by the reconstruction: ordering never implies
    payment, booking/order never implies receipt, and reviews never
    establish operational stages. No `strong` or `not_applicable` is
    emitted in v2 — no evidence-backed definition exists for either.
    """
    journey = dossier.get("customer_journey") or {}
    stages = {
        str(stage.get("stage")): str(stage.get("evidence_state"))
        for stage in journey.get("stages", ())
    }
    facts, unknowns, _fresh = _domain_context(dossier, "customer_journey")
    conflicted_stages = sorted(
        stage for stage, state in stages.items() if state == "conflicted"
    )
    if conflicted_stages:
        return "conflicted", _reason(
            code="customer_journey_stage_conflict",
            facts=facts,
            unknowns=unknowns,
            extra={"conflicted_stages": conflicted_stages},
        )
    current = {stage for stage, state in stages.items() if state == "observed"}
    ever_observed = {
        stage for stage, state in stages.items()
        if state in ("observed", "stale")
    }
    if not ever_observed:
        return "not_started", _reason(
            code="no_customer_journey_reconstruction_evidence",
            facts=facts,
            unknowns=unknowns,
        )
    if not current:
        return "stale", _reason(
            code="customer_journey_evidence_stale",
            facts=facts,
            unknowns=unknowns,
            extra={"observed_stage_count": journey.get("observed_stage_count")},
        )
    handoffs = journey.get("handoffs") or []
    # RCJ-05: hand-offs now carry a currency flag (historical ones are
    # retained on stale stages). Sufficiency counts CURRENT hand-offs
    # only — a historical booking hand-off is not a live route.
    external_action_handoff = any(
        str(handoff.get("from")) == "official_website"
        and str(handoff.get("to")) in ("booking", "ordering")
        and bool(handoff.get("current"))
        for handoff in handoffs
    )
    unmet = [
        label
        for label, ok in (
            ("entry_or_evaluation_stage", bool(current & {"discover", "evaluate"})),
            ("customer_action_stage", bool(current & {"contact", "book_order"})),
            (
                "later_stage_or_action_handoff",
                bool(current & {"pay", "receive", "support", "return"})
                or external_action_handoff,
            ),
        )
        if not ok
    ]
    if not unmet:
        return "sufficient", _reason(
            code="observable_journey_stages_reconstructed",
            facts=facts,
            unknowns=unknowns,
            extra={
                "current_stages": sorted(current),
                "reconstruction_version": journey.get("reconstruction_version"),
            },
        )
    return "partial", _reason(
        code="journey_partially_reconstructed",
        facts=facts,
        unknowns=unknowns,
        extra={
            "current_stages": sorted(current),
            "unmet_conditions": unmet,
            "reconstruction_version": journey.get("reconstruction_version"),
        },
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