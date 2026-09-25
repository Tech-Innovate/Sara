from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .storage import connect_readonly
from .understanding_vocabulary import (
    DOSSIER_DOMAIN_SEED_V1,
    DOSSIER_POLICY_VERSION,
    VocabularySeedError,
    verify_business_understanding_vocabulary,
)


class DossierQueryError(RuntimeError):
    """The requested Business Understanding dossier cannot be read safely."""


_VALUE_STATUSES = {"confirmed", "single_source", "conflicted", "stale"}
_NULL_STATUSES = {"unknown", "not_observed", "not_applicable"}
_REQUIRED_OBJECTS = {
    "knowledge_subjects": "table",
    "business_entities": "table",
    "business_locations": "table",
    "maps_business_location_links": "table",
    "external_identifiers": "table",
    "sources": "table",
    "acquisition_sessions": "table",
    "evidence_items": "table",
    "predicate_definitions": "table",
    "observations": "table",
    "facts": "table",
    "fact_observation_support": "table",
    "fact_acquisition_support": "table",
    "dossier_assessments": "table",
    "dossier_domain_assessments": "table",
    "dossier_assessment_seals": "table",
    "finalized_dossier_assessments": "view",
}


def _dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return None if row is None else dict(row)


def _parse_timestamp(value: object, *, field: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise DossierQueryError(f"{field} is not a non-empty timestamp")
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise DossierQueryError(f"{field} has invalid ISO-8601 timestamp {value!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise DossierQueryError(f"{field} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _evaluation_time(value: str | None) -> datetime:
    return datetime.now(timezone.utc) if value is None else _parse_timestamp(value, field="evaluated_at")


def _json_value(value: object, *, field: str) -> Any:
    if value is None:
        return None
    if not isinstance(value, str):
        raise DossierQueryError(f"{field} is not stored as JSON text")
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise DossierQueryError(f"{field} contains malformed JSON") from exc


def _verify_schema(conn: sqlite3.Connection) -> None:
    try:
        verify_business_understanding_vocabulary(conn)
    except VocabularySeedError as exc:
        raise DossierQueryError(str(exc)) from exc
    placeholders = ",".join("?" for _ in _REQUIRED_OBJECTS)
    found = {
        (str(row["name"]), str(row["type"]))
        for row in conn.execute(
            f"SELECT name,type FROM sqlite_master WHERE name IN ({placeholders})",
            tuple(_REQUIRED_OBJECTS),
        )
    }
    missing = [
        name for name, object_type in _REQUIRED_OBJECTS.items()
        if (name, object_type) not in found
    ]
    if missing:
        raise DossierQueryError(
            "Business Understanding schema is incomplete; missing: " + ", ".join(sorted(missing))
        )


def _subject(conn: sqlite3.Connection, subject_id: str, kind: str) -> dict[str, Any]:
    row = _dict(conn.execute(
        "SELECT id,kind,record_state,merged_into_subject_id,created_at,updated_at,merged_at "
        "FROM knowledge_subjects WHERE id=?",
        (subject_id,),
    ).fetchone())
    if row is None:
        raise DossierQueryError(f"missing Understanding subject {subject_id!r}")
    if row["kind"] != kind:
        raise DossierQueryError(
            f"Understanding subject {subject_id!r} has kind {row['kind']!r}, expected {kind!r}"
        )
    return row


def _resolve_subject(conn: sqlite3.Connection, subject_id: str, kind: str) -> tuple[dict[str, Any], list[str]]:
    current = subject_id
    seen: set[str] = set()
    chain: list[str] = []
    while True:
        if current in seen:
            raise DossierQueryError(f"Understanding subject redirect cycle detected from {subject_id!r}")
        seen.add(current)
        chain.append(current)
        row = _subject(conn, current, kind)
        if row["record_state"] == "merged":
            target = row["merged_into_subject_id"]
            if not isinstance(target, str) or not target:
                raise DossierQueryError(f"merged Understanding subject {current!r} has no merge target")
            current = target
            continue
        if row["record_state"] not in {"active", "retired"} or row["merged_into_subject_id"] is not None:
            raise DossierQueryError(f"Understanding subject {current!r} has inconsistent lifecycle state")
        return row, chain


def _location_owner(conn: sqlite3.Connection, location_id: str) -> str:
    row = conn.execute(
        "SELECT business_entity_id FROM business_locations WHERE id=?", (location_id,)
    ).fetchone()
    if row is None:
        raise DossierQueryError(f"missing business location {location_id!r}")
    canonical, _chain = _resolve_subject(conn, str(row["business_entity_id"]), "business_entity")
    return str(canonical["id"])


def _resolve_selection(
    conn: sqlite3.Connection,
    *,
    business_id: int | None,
    canonical_key: str | None,
    entity_id: str | None,
) -> tuple[str, dict[str, Any]]:
    if entity_id is not None:
        canonical, chain = _resolve_subject(conn, entity_id, "business_entity")
        return str(canonical["id"]), {
            "kind": "entity_id", "value": entity_id, "entity_resolution_chain": chain
        }

    if business_id is not None:
        row = _dict(conn.execute(
            "SELECT id,canonical_key,title,last_seen_at FROM businesses WHERE id=?", (business_id,)
        ).fetchone())
        kind = "business_id"
    else:
        assert canonical_key is not None
        row = _dict(conn.execute(
            "SELECT id,canonical_key,title,last_seen_at FROM businesses WHERE canonical_key=?",
            (canonical_key,),
        ).fetchone())
        kind = "canonical_key"
    if row is None:
        raise DossierQueryError("no canonical Maps business matches the requested selector")
    link = _dict(conn.execute(
        "SELECT location_id,linked_at FROM maps_business_location_links WHERE business_id=?",
        (row["id"],),
    ).fetchone())
    if link is None:
        raise DossierQueryError(
            f"Maps business {row['id']} is not synchronized into Business Understanding; "
            "run sara-maps-sync explicitly before reading its dossier"
        )
    location, location_chain = _resolve_subject(conn, str(link["location_id"]), "location")
    if location["record_state"] != "active":
        raise DossierQueryError(f"current Maps business {row['id']} resolves to a retired location")
    entity = _location_owner(conn, str(location["id"]))
    canonical_entity, entity_chain = _resolve_subject(conn, entity, "business_entity")
    if canonical_entity["record_state"] != "active":
        raise DossierQueryError(f"current Maps business {row['id']} resolves to a retired entity")
    return str(canonical_entity["id"]), {
        "kind": kind,
        "value": row["id"] if kind == "business_id" else row["canonical_key"],
        "maps_business": row,
        "linked_location_id": link["location_id"],
        "canonical_location_id": location["id"],
        "location_resolution_chain": location_chain,
        "entity_resolution_chain": entity_chain,
    }


def _entity(conn: sqlite3.Connection, entity_id: str) -> dict[str, Any]:
    row = _dict(conn.execute(
        "SELECT be.id,be.display_name,be.entity_type,be.lifecycle_status,be.identity_confidence,"
        "be.created_at,be.updated_at,ks.record_state,ks.merged_into_subject_id,ks.merged_at "
        "FROM business_entities be JOIN knowledge_subjects ks ON ks.id=be.id WHERE be.id=?",
        (entity_id,),
    ).fetchone())
    if row is None:
        raise DossierQueryError(f"missing business entity row for {entity_id!r}")
    return row


def _identifiers(conn: sqlite3.Connection, location_id: str) -> list[dict[str, Any]]:
    return [
        dict(row) for row in conn.execute(
            "SELECT ei.id,ei.source_id,ei.namespace,ei.value,ei.status,ei.first_observed_at,"
            "ei.last_observed_at,ei.created_at,s.source_type,s.name AS source_name "
            "FROM external_identifiers ei JOIN sources s ON s.id=ei.source_id "
            "WHERE ei.subject_id=? ORDER BY ei.source_id,ei.namespace,ei.value,ei.id",
            (location_id,),
        )
    ]


def _locations(conn: sqlite3.Connection, entity_id: str) -> tuple[list[dict[str, Any]], list[str]]:
    result: list[dict[str, Any]] = []
    current_ids: set[str] = set()
    rows = list(conn.execute(
        "SELECT bl.id,bl.business_entity_id,bl.label,bl.location_type,bl.created_at,bl.updated_at,"
        "ks.record_state,ks.merged_into_subject_id,ks.merged_at "
        "FROM business_locations bl JOIN knowledge_subjects ks ON ks.id=bl.id ORDER BY bl.id"
    ))
    for raw in rows:
        item = dict(raw)
        owner, _owner_chain = _resolve_subject(conn, str(item["business_entity_id"]), "business_entity")
        if str(owner["id"]) != entity_id:
            continue
        canonical, chain = _resolve_subject(conn, str(item["id"]), "location")
        canonical_owner = _location_owner(conn, str(canonical["id"]))
        is_current = (
            canonical["record_state"] == "active"
            and canonical_owner == entity_id
            and str(canonical["id"]) == str(item["id"])
        )
        if is_current:
            current_ids.add(str(item["id"]))
        item["canonical_location_id"] = str(canonical["id"])
        item["resolution_chain"] = chain
        item["current_for_entity"] = is_current
        item["external_identifiers"] = _identifiers(conn, str(item["id"]))
        result.append(item)
    return result, sorted(current_ids)


def _maps_businesses(conn: sqlite3.Connection, current_location_ids: list[str]) -> list[dict[str, Any]]:
    current = set(current_location_ids)
    result: list[dict[str, Any]] = []
    if not current:
        return result
    rows = list(conn.execute(
        "SELECT b.id,b.canonical_key,b.title,b.last_seen_at,m.location_id,m.linked_at "
        "FROM maps_business_location_links m JOIN businesses b ON b.id=m.business_id ORDER BY b.id"
    ))
    for raw in rows:
        item = dict(raw)
        canonical, _chain = _resolve_subject(conn, str(item["location_id"]), "location")
        canonical_id = str(canonical["id"])
        if canonical_id in current:
            item["canonical_location_id"] = canonical_id
            result.append(item)
    return result


def _freshness(fact: dict[str, Any], evaluated_at: datetime) -> dict[str, Any]:
    days = fact["freshness_days"]
    reference_text = fact["last_verified_at"] or fact["valid_from"]
    if fact["status"] in _NULL_STATUSES or fact["status"] == "conflicted" or days is None:
        return {
            "evaluated": False,
            "freshness_days": days,
            "reference_at": reference_text,
            "stale_after": None,
            "is_stale": fact["status"] == "stale",
        }
    reference = _parse_timestamp(reference_text, field=f"fact {fact['id']} freshness reference")
    stale_after = reference + timedelta(days=int(days))
    return {
        "evaluated": True,
        "freshness_days": int(days),
        "reference_at": reference.isoformat(),
        "stale_after": stale_after.isoformat(),
        "is_stale": fact["status"] == "stale" or evaluated_at > stale_after,
    }


def _facts(
    conn: sqlite3.Connection,
    entity_id: str,
    current_location_ids: list[str],
    evaluated_at: datetime,
) -> list[dict[str, Any]]:
    subjects = [entity_id, *current_location_ids]
    placeholders = ",".join("?" for _ in subjects)
    rows = list(conn.execute(
        "SELECT f.id,f.subject_id,ks.kind AS subject_kind,f.predicate,pd.domain,pd.cardinality,"
        "pd.value_type,pd.freshness_days,f.fact_slot,f.value_json,f.normalized_value_json,"
        "f.value_hash,f.status,f.valid_from,f.valid_to,f.last_verified_at,f.reconciled_at,"
        "f.reconciliation_version,f.created_at "
        "FROM facts f JOIN predicate_definitions pd ON pd.name=f.predicate "
        "JOIN knowledge_subjects ks ON ks.id=f.subject_id "
        f"WHERE f.valid_to IS NULL AND f.subject_id IN ({placeholders}) "
        "ORDER BY pd.domain,f.subject_id,f.predicate,f.fact_slot,f.id",
        tuple(subjects),
    ))
    result: list[dict[str, Any]] = []
    for raw in rows:
        fact = dict(raw)
        fact["value"] = _json_value(fact.pop("value_json"), field=f"fact {fact['id']} value_json")
        fact["normalized_value"] = _json_value(
            fact.pop("normalized_value_json"), field=f"fact {fact['id']} normalized_value_json"
        )
        fact["freshness"] = _freshness(fact, evaluated_at)
        fact["observation_support"] = []
        fact["acquisition_support"] = []
        result.append(fact)
    return result


def _attach_provenance(
    conn: sqlite3.Connection, facts: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not facts:
        return [], []
    fact_by_id = {str(fact["id"]): fact for fact in facts}
    ids = sorted(fact_by_id)
    placeholders = ",".join("?" for _ in ids)
    evidence: dict[str, dict[str, Any]] = {}
    issues: list[dict[str, Any]] = []

    raw_obs = {
        str(row["fact_id"]): int(row["n"])
        for row in conn.execute(
            f"SELECT fact_id,COUNT(*) AS n FROM fact_observation_support WHERE fact_id IN ({placeholders}) GROUP BY fact_id",
            tuple(ids),
        )
    }
    joined_obs: dict[str, int] = {}
    rows = list(conn.execute(
        "SELECT fos.fact_id,fos.support_role,o.id AS observation_id,o.subject_id AS observation_subject_id,"
        "o.predicate AS observation_predicate,o.observed_at,o.extracted_at,o.extraction_method,"
        "o.extractor_name,o.extractor_version,o.confidence,e.id AS evidence_id,e.source_id,"
        "e.source_locator,e.source_role,e.status AS evidence_status,e.retrieved_at,e.published_at,"
        "e.language,e.media_type,e.content_sha256,e.artifact_ref,a.id AS acquisition_session_id,"
        "a.collector_name,a.collector_version,a.status AS acquisition_status,s.source_type,"
        "s.name AS source_name,s.base_url,s.active AS source_active "
        "FROM fact_observation_support fos JOIN observations o ON o.id=fos.observation_id "
        "JOIN evidence_items e ON e.id=o.evidence_id "
        "JOIN acquisition_sessions a ON a.id=e.acquisition_session_id "
        "JOIN sources s ON s.id=e.source_id "
        f"WHERE fos.fact_id IN ({placeholders}) ORDER BY fos.fact_id,fos.support_role,o.id",
        tuple(ids),
    ))
    for raw in rows:
        item = dict(raw)
        fact_id = str(item["fact_id"])
        joined_obs[fact_id] = joined_obs.get(fact_id, 0) + 1
        evidence_id = str(item["evidence_id"])
        fact_by_id[fact_id]["observation_support"].append({
            "support_role": item["support_role"],
            "observation_id": item["observation_id"],
            "evidence_id": evidence_id,
            "source_id": item["source_id"],
        })
        record = evidence.setdefault(evidence_id, {
            "id": evidence_id,
            "source_id": item["source_id"],
            "source_type": item["source_type"],
            "source_name": item["source_name"],
            "source_base_url": item["base_url"],
            "source_active": bool(item["source_active"]),
            "source_locator": item["source_locator"],
            "source_role": item["source_role"],
            "status": item["evidence_status"],
            "retrieved_at": item["retrieved_at"],
            "published_at": item["published_at"],
            "language": item["language"],
            "media_type": item["media_type"],
            "content_sha256": item["content_sha256"],
            "artifact_ref": item["artifact_ref"],
            "acquisition_session_id": item["acquisition_session_id"],
            "collector_name": item["collector_name"],
            "collector_version": item["collector_version"],
            "acquisition_status": item["acquisition_status"],
            "observations": [],
        })
        observation = {
            "id": item["observation_id"],
            "subject_id": item["observation_subject_id"],
            "predicate": item["observation_predicate"],
            "observed_at": item["observed_at"],
            "extracted_at": item["extracted_at"],
            "extraction_method": item["extraction_method"],
            "extractor_name": item["extractor_name"],
            "extractor_version": item["extractor_version"],
            "confidence": item["confidence"],
        }
        if observation not in record["observations"]:
            record["observations"].append(observation)

    raw_acq = {
        str(row["fact_id"]): int(row["n"])
        for row in conn.execute(
            f"SELECT fact_id,COUNT(*) AS n FROM fact_acquisition_support WHERE fact_id IN ({placeholders}) GROUP BY fact_id",
            tuple(ids),
        )
    }
    joined_acq: dict[str, int] = {}
    rows = list(conn.execute(
        "SELECT fas.fact_id,fas.support_role,a.id AS acquisition_session_id,a.source_id,"
        "a.collector_name,a.collector_version,a.status,a.started_at,a.finished_at,a.legacy_run_id,"
        "s.source_type,s.name AS source_name FROM fact_acquisition_support fas "
        "JOIN acquisition_sessions a ON a.id=fas.acquisition_session_id "
        "JOIN sources s ON s.id=a.source_id "
        f"WHERE fas.fact_id IN ({placeholders}) ORDER BY fas.fact_id,fas.support_role,a.id",
        tuple(ids),
    ))
    for raw in rows:
        item = dict(raw)
        fact_id = str(item.pop("fact_id"))
        joined_acq[fact_id] = joined_acq.get(fact_id, 0) + 1
        fact_by_id[fact_id]["acquisition_support"].append(item)

    for fact_id, fact in fact_by_id.items():
        if raw_obs.get(fact_id, 0) != joined_obs.get(fact_id, 0):
            issues.append({"code": "broken_observation_provenance", "fact_id": fact_id})
        if raw_acq.get(fact_id, 0) != joined_acq.get(fact_id, 0):
            issues.append({"code": "broken_acquisition_provenance", "fact_id": fact_id})
        if fact["status"] in _VALUE_STATUSES and not any(
            support["support_role"] == "supports" for support in fact["observation_support"]
        ):
            issues.append({"code": "value_fact_without_supporting_observation", "fact_id": fact_id})
        if fact["status"] == "not_observed" and not fact["acquisition_support"]:
            issues.append({"code": "not_observed_without_acquisition_support", "fact_id": fact_id})

    for record in evidence.values():
        record["observations"].sort(key=lambda value: str(value["id"]))
    return [evidence[key] for key in sorted(evidence)], issues


def _unknowns(
    conn: sqlite3.Connection,
    entity_id: str,
    current_location_ids: list[str],
    facts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    indexed: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for fact in facts:
        indexed.setdefault((str(fact["subject_id"]), str(fact["predicate"])), []).append(fact)
    result: list[dict[str, Any]] = []
    rows = list(conn.execute(
        "SELECT name,domain,subject_kind FROM predicate_definitions WHERE active=1 ORDER BY domain,name"
    ))
    for raw in rows:
        predicate = dict(raw)
        subjects = [entity_id] if predicate["subject_kind"] == "business_entity" else current_location_ids
        if not subjects:
            result.append({
                "domain": predicate["domain"], "predicate": predicate["name"],
                "subject_id": None, "subject_kind": predicate["subject_kind"],
                "state": "unresolved", "reason": "no_current_subject_for_predicate_scope",
            })
            continue
        for subject_id in subjects:
            matching = indexed.get((subject_id, predicate["name"]), [])
            if not matching:
                result.append({
                    "domain": predicate["domain"], "predicate": predicate["name"],
                    "subject_id": subject_id, "subject_kind": predicate["subject_kind"],
                    "state": "unresolved", "reason": "no_current_fact",
                })
            else:
                for fact in matching:
                    if fact["status"] in {"unknown", "not_observed"}:
                        result.append({
                            "domain": predicate["domain"], "predicate": predicate["name"],
                            "subject_id": subject_id, "subject_kind": predicate["subject_kind"],
                            "state": fact["status"], "fact_id": fact["id"],
                            "reason": "explicit_fact_state",
                        })
    result.sort(key=lambda value: (
        str(value["domain"]), str(value["predicate"]), str(value.get("subject_id") or ""), str(value["state"])
    ))
    return result


def _persisted_assessment(conn: sqlite3.Connection, entity_id: str) -> dict[str, Any] | None:
    row = _dict(conn.execute(
        "SELECT id,business_entity_id,policy_version,facts_as_of,analysis_ready,computed_at,summary_json,sealed_at "
        "FROM finalized_dossier_assessments WHERE business_entity_id=? AND policy_version=? "
        "ORDER BY computed_at DESC,id DESC LIMIT 1",
        (entity_id, DOSSIER_POLICY_VERSION),
    ).fetchone())
    if row is None:
        return None
    row["analysis_ready"] = bool(row["analysis_ready"])
    row["summary"] = _json_value(row.pop("summary_json"), field=f"dossier {row['id']} summary_json")
    domains: list[dict[str, Any]] = []
    for raw in conn.execute(
        "SELECT domain,state,reason_json,fact_count,fresh_fact_count "
        "FROM dossier_domain_assessments WHERE assessment_id=? ORDER BY domain",
        (row["id"],),
    ):
        item = dict(raw)
        item["reason"] = _json_value(
            item.pop("reason_json"), field=f"dossier {row['id']} domain {item['domain']} reason_json"
        )
        domains.append(item)
    expected = {seed.name for seed in DOSSIER_DOMAIN_SEED_V1}
    actual = {str(item["domain"]) for item in domains}
    if actual != expected or len(domains) != len(expected):
        raise DossierQueryError(
            f"sealed dossier assessment {row['id']!r} has incomplete domain coverage: "
            f"missing={sorted(expected - actual)!r}, unexpected={sorted(actual - expected)!r}"
        )
    _parse_timestamp(row["facts_as_of"], field=f"dossier {row['id']} facts_as_of")
    _parse_timestamp(row["computed_at"], field=f"dossier {row['id']} computed_at")
    _parse_timestamp(row["sealed_at"], field=f"dossier {row['id']} sealed_at")
    row["domains"] = domains
    return row


def _preview_domains(
    facts: list[dict[str, Any]],
    unknowns: list[dict[str, Any]],
    integrity_issues: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for seed in DOSSIER_DOMAIN_SEED_V1:
        domain_facts = [fact for fact in facts if fact["domain"] == seed.name]
        gaps = [item for item in unknowns if item["domain"] == seed.name]
        fresh = [
            fact for fact in domain_facts
            if fact["status"] in {"confirmed", "single_source"}
            and not fact["freshness"]["is_stale"]
        ]
        if seed.name == "provenance":
            state = "not_started" if not facts else ("insufficient" if integrity_issues else "partial")
            reasons = [
                "no_current_facts_to_trace" if not facts else
                "current_fact_provenance_has_integrity_issues" if integrity_issues else
                "current_fact_provenance_is_traceable_but_preview_never_claims_sufficiency"
            ]
        elif seed.name == "unknowns":
            state = "partial"
            reasons = [
                "controlled_unresolved_items_are_enumerated" if unknowns else
                "no_controlled_unresolved_items_detected_but_preview_never_claims_sufficiency"
            ]
        elif not domain_facts:
            state, reasons = "not_started", ["no_current_facts_in_domain"]
        elif all(fact["status"] == "not_applicable" for fact in domain_facts) and not gaps:
            state, reasons = "not_applicable", ["all_current_domain_facts_are_not_applicable"]
        elif any(fact["status"] == "conflicted" for fact in domain_facts):
            state, reasons = "conflicted", ["one_or_more_current_facts_are_conflicted"]
        elif not fresh and any(
            fact["status"] == "stale" or fact["freshness"]["is_stale"] for fact in domain_facts
        ):
            state, reasons = "stale", ["no_fresh_supported_value_fact_remains_in_domain"]
        elif all(fact["status"] in _NULL_STATUSES for fact in domain_facts):
            state, reasons = "insufficient", ["domain_has_only_null_semantic_fact_states"]
        else:
            state, reasons = "partial", [
                "some_current_evidence_exists_but_phase5_preview_does_not_promote_sufficiency"
            ]
        result.append({
            "domain": seed.name,
            "mandatory_for_initial_analysis": bool(seed.mandatory_for_initial_analysis),
            "state": state,
            "fact_count": len(domain_facts),
            "fresh_fact_count": len(fresh),
            "unresolved_count": len(gaps),
            "reasons": reasons,
        })
    return result


def build_business_dossier(
    conn: sqlite3.Connection,
    *,
    business_id: int | None = None,
    canonical_key: str | None = None,
    entity_id: str | None = None,
    evaluated_at: str | None = None,
) -> dict[str, Any]:
    if sum(value is not None for value in (business_id, canonical_key, entity_id)) != 1:
        raise DossierQueryError("select exactly one of business_id, canonical_key, or entity_id")
    if business_id is not None and business_id <= 0:
        raise DossierQueryError("business_id must be greater than zero")
    if canonical_key is not None and not canonical_key.strip():
        raise DossierQueryError("canonical_key must not be blank")
    if entity_id is not None and not entity_id.strip():
        raise DossierQueryError("entity_id must not be blank")

    _verify_schema(conn)
    evaluation = _evaluation_time(evaluated_at)
    canonical_entity_id, selection = _resolve_selection(
        conn,
        business_id=business_id,
        canonical_key=canonical_key.strip() if canonical_key is not None else None,
        entity_id=entity_id.strip() if entity_id is not None else None,
    )
    entity = _entity(conn, canonical_entity_id)
    locations, current_location_ids = _locations(conn, canonical_entity_id)
    facts = _facts(conn, canonical_entity_id, current_location_ids, evaluation)
    evidence, integrity_issues = _attach_provenance(conn, facts)
    unknowns = _unknowns(conn, canonical_entity_id, current_location_ids, facts)
    persisted = _persisted_assessment(conn, canonical_entity_id)
    return {
        "schema": "sara-business-dossier-v1",
        "fact_scope": "current_only",
        "evaluated_at": evaluation.isoformat(),
        "selection": selection,
        "business_entity": entity,
        "maps_businesses": _maps_businesses(conn, current_location_ids),
        "locations": locations,
        "facts": facts,
        "evidence": evidence,
        "unknowns": unknowns,
        "integrity_issues": integrity_issues,
        "dossier_status": {
            "active_policy_version": DOSSIER_POLICY_VERSION,
            "persisted_current_policy": persisted,
            "effective_analysis_ready": bool(persisted["analysis_ready"]) if persisted else False,
            "read_only_preview": {
                "derivation_version": "phase5-readonly-v1",
                "analysis_ready": False,
                "promotion_policy": "never_promote_from_preview",
                "domains": _preview_domains(facts, unknowns, integrity_issues),
            },
        },
    }


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sara-dossier",
        description="Read a Business Understanding dossier without mutating Sara state.",
    )
    parser.add_argument("--db", default="data/sara.db", help="SQLite database path")
    selector = parser.add_mutually_exclusive_group(required=True)
    selector.add_argument("--business-id", type=_positive_int)
    selector.add_argument("--canonical-key")
    selector.add_argument("--entity-id")
    parser.add_argument(
        "--evaluated-at", help="timezone-aware ISO-8601 time used only for freshness evaluation"
    )
    parser.add_argument("--pretty", action="store_true", help="pretty-print JSON output")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    conn: sqlite3.Connection | None = None
    try:
        conn = connect_readonly(Path(args.db))
        conn.execute("BEGIN")
        dossier = build_business_dossier(
            conn,
            business_id=args.business_id,
            canonical_key=args.canonical_key,
            entity_id=args.entity_id,
            evaluated_at=args.evaluated_at,
        )
        kwargs = {"ensure_ascii": False, "sort_keys": True}
        if args.pretty:
            print(json.dumps(dossier, indent=2, **kwargs))
        else:
            print(json.dumps(dossier, separators=(",", ":"), **kwargs))
        return 0
    except (FileNotFoundError, DossierQueryError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("dossier query interrupted", file=sys.stderr)
        return 130
    except sqlite3.Error as exc:
        print(f"dossier query failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
