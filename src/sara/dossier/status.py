from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

from ..understanding_vocabulary import DOSSIER_DOMAIN_SEED_V1, DOSSIER_POLICY_VERSION
from .core import DossierQueryError, NULL_STATUSES, json_value, parse_timestamp, row_dict


def persisted_assessment(conn: sqlite3.Connection, entity_id: str) -> dict[str, Any] | None:
    cursor = conn.execute(
        "SELECT id,business_entity_id,policy_version,facts_as_of,analysis_ready,computed_at,"
        "summary_json,sealed_at FROM finalized_dossier_assessments "
        "WHERE business_entity_id=? AND policy_version=?",
        (entity_id, DOSSIER_POLICY_VERSION),
    )
    candidates: list[tuple[datetime, str, dict[str, Any]]] = []
    for row in cursor.fetchall():
        item = row_dict(cursor, row)
        computed = parse_timestamp(item["computed_at"], field=f"dossier {item['id']} computed_at")
        candidates.append((computed, str(item["id"]), item))
    if not candidates:
        return None

    _computed, _identifier, result = max(candidates, key=lambda item: (item[0], item[1]))
    result["analysis_ready"] = bool(result["analysis_ready"])
    result["summary"] = json_value(
        result.pop("summary_json"), field=f"dossier {result['id']} summary_json"
    )
    domain_cursor = conn.execute(
        "SELECT domain,state,reason_json,fact_count,fresh_fact_count "
        "FROM dossier_domain_assessments WHERE assessment_id=? ORDER BY domain",
        (result["id"],),
    )
    domains: list[dict[str, Any]] = []
    for row in domain_cursor.fetchall():
        item = row_dict(domain_cursor, row)
        item["reason"] = json_value(
            item.pop("reason_json"),
            field=f"dossier {result['id']} domain {item['domain']} reason_json",
        )
        domains.append(item)

    expected = {seed.name for seed in DOSSIER_DOMAIN_SEED_V1}
    actual = {str(item["domain"]) for item in domains}
    if actual != expected or len(domains) != len(expected):
        raise DossierQueryError(
            f"sealed dossier assessment {result['id']!r} has incomplete domain coverage: "
            f"missing={sorted(expected - actual)!r}, unexpected={sorted(actual - expected)!r}"
        )
    parse_timestamp(result["facts_as_of"], field=f"dossier {result['id']} facts_as_of")
    parse_timestamp(result["sealed_at"], field=f"dossier {result['id']} sealed_at")
    result["domains"] = domains
    result["snapshot_semantics"] = "immutable_historical_assessment_not_recomputed_by_phase5"
    return result


def preview_domains(
    facts: list[dict[str, Any]],
    unknowns: list[dict[str, Any]],
    integrity_issues: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for seed in DOSSIER_DOMAIN_SEED_V1:
        domain_facts = [fact for fact in facts if fact["domain"] == seed.name]
        domain_unknowns = [item for item in unknowns if item["domain"] == seed.name]
        fresh_value_facts = [
            fact
            for fact in domain_facts
            if fact["status"] in {"confirmed", "single_source"}
            and not fact["freshness"]["is_stale"]
        ]

        if seed.name == "provenance":
            if not facts:
                state, reasons = "not_started", ["no_current_facts_to_trace"]
            elif integrity_issues:
                state, reasons = "insufficient", ["current_fact_provenance_has_integrity_issues"]
            else:
                state, reasons = "partial", [
                    "current_fact_provenance_is_traceable_but_preview_never_claims_sufficiency"
                ]
        elif seed.name == "unknowns":
            state = "partial"
            reasons = [
                "controlled_unresolved_items_are_enumerated"
                if unknowns
                else "no_controlled_unresolved_items_detected_but_preview_never_claims_sufficiency"
            ]
        elif not domain_facts:
            state, reasons = "not_started", ["no_current_facts_in_domain"]
        elif all(fact["status"] == "not_applicable" for fact in domain_facts) and not domain_unknowns:
            state, reasons = "not_applicable", ["all_current_domain_facts_are_not_applicable"]
        elif any(fact["status"] == "conflicted" for fact in domain_facts):
            state, reasons = "conflicted", ["one_or_more_current_facts_are_conflicted"]
        elif not fresh_value_facts and any(
            fact["status"] == "stale" or fact["freshness"]["is_stale"] for fact in domain_facts
        ):
            state, reasons = "stale", ["no_fresh_supported_value_fact_remains_in_domain"]
        elif all(fact["status"] in NULL_STATUSES for fact in domain_facts):
            state, reasons = "insufficient", ["domain_has_only_null_semantic_fact_states"]
        else:
            state, reasons = "partial", [
                "some_current_evidence_exists_but_phase5_preview_does_not_promote_sufficiency"
            ]

        result.append(
            {
                "domain": seed.name,
                "mandatory_for_initial_analysis": bool(seed.mandatory_for_initial_analysis),
                "state": state,
                "fact_count": len(domain_facts),
                "fresh_fact_count": len(fresh_value_facts),
                "unresolved_count": len(domain_unknowns),
                "reasons": reasons,
            }
        )
    return result
