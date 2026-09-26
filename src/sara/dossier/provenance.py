from __future__ import annotations

import json
import sqlite3
from typing import Any

from .core import json_value, row_dict


def _semantic_key(value: Any, normalized_value: Any, value_hash: object) -> str | None:
    semantic = normalized_value if normalized_value is not None else value
    if semantic is not None:
        return json.dumps(semantic, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if isinstance(value_hash, str) and value_hash:
        return f"hash:{value_hash}"
    return None


def _support_matches_fact(fact: dict[str, Any], support: dict[str, Any]) -> bool:
    fact_hash = fact.get("value_hash")
    observation_hash = support.get("observation_value_hash")
    if isinstance(fact_hash, str) and fact_hash and isinstance(observation_hash, str) and observation_hash:
        return fact_hash == observation_hash
    fact_key = _semantic_key(fact.get("value"), fact.get("normalized_value"), fact_hash)
    observation_key = _semantic_key(
        support.get("observation_value"),
        support.get("observation_normalized_value"),
        observation_hash,
    )
    return fact_key is not None and fact_key == observation_key


def attach_provenance(
    conn: sqlite3.Connection, facts: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not facts:
        return [], []
    fact_by_id = {str(fact["id"]): fact for fact in facts}
    fact_ids = sorted(fact_by_id)
    placeholders = ",".join("?" for _ in fact_ids)
    issues: list[dict[str, Any]] = []
    evidence_by_id: dict[str, dict[str, Any]] = {}

    raw_observation_counts = {
        str(row[0]): int(row[1])
        for row in conn.execute(
            "SELECT fact_id,COUNT(*) FROM fact_observation_support "
            f"WHERE fact_id IN ({placeholders}) GROUP BY fact_id",
            tuple(fact_ids),
        )
    }
    cursor = conn.execute(
        "SELECT fos.fact_id,fos.support_role,o.id AS observation_id,"
        "o.subject_id AS observation_subject_id,o.predicate AS observation_predicate,"
        "o.value_json AS observation_value_json,"
        "o.normalized_value_json AS observation_normalized_value_json,"
        "o.value_hash AS observation_value_hash,o.observation_kind,"
        "o.observed_at,o.extracted_at,o.extraction_method,o.extractor_name,o.extractor_version,"
        "o.confidence,e.id AS evidence_id,e.source_id,e.source_locator,e.source_role,"
        "e.status AS evidence_status,e.retrieved_at,e.published_at,e.language,e.media_type,"
        "e.content_sha256,e.artifact_ref,a.id AS acquisition_session_id,a.collector_name,"
        "a.collector_version,a.status AS acquisition_status,s.source_type,s.name AS source_name,"
        "s.base_url,s.active AS source_active "
        "FROM fact_observation_support fos "
        "JOIN observations o ON o.id=fos.observation_id "
        "JOIN evidence_items e ON e.id=o.evidence_id "
        "JOIN acquisition_sessions a ON a.id=e.acquisition_session_id "
        "JOIN sources s ON s.id=e.source_id "
        f"WHERE fos.fact_id IN ({placeholders}) "
        "ORDER BY fos.fact_id,fos.support_role,o.id",
        tuple(fact_ids),
    )
    joined_observation_counts: dict[str, int] = {}
    for row in cursor.fetchall():
        item = row_dict(cursor, row)
        fact_id = str(item["fact_id"])
        joined_observation_counts[fact_id] = joined_observation_counts.get(fact_id, 0) + 1
        observation_value = json_value(
            item["observation_value_json"],
            field=f"observation {item['observation_id']} value_json",
        )
        observation_normalized_value = json_value(
            item["observation_normalized_value_json"],
            field=f"observation {item['observation_id']} normalized_value_json",
        )
        support = {
            "support_role": item["support_role"],
            "observation_id": item["observation_id"],
            "observation_subject_id": item["observation_subject_id"],
            "observation_predicate": item["observation_predicate"],
            "observation_value": observation_value,
            "observation_normalized_value": observation_normalized_value,
            "observation_value_hash": item["observation_value_hash"],
            "evidence_id": item["evidence_id"],
            "source_id": item["source_id"],
            "evidence_status": item["evidence_status"],
        }
        fact_by_id[fact_id]["observation_support"].append(support)
        evidence_id = str(item["evidence_id"])
        record = evidence_by_id.setdefault(
            evidence_id,
            {
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
            },
        )
        observation = {
            "id": item["observation_id"],
            "subject_id": item["observation_subject_id"],
            "predicate": item["observation_predicate"],
            "value": observation_value,
            "normalized_value": observation_normalized_value,
            "value_hash": item["observation_value_hash"],
            "observation_kind": item["observation_kind"],
            "observed_at": item["observed_at"],
            "extracted_at": item["extracted_at"],
            "extraction_method": item["extraction_method"],
            "extractor_name": item["extractor_name"],
            "extractor_version": item["extractor_version"],
            "confidence": item["confidence"],
        }
        if observation not in record["observations"]:
            record["observations"].append(observation)

    raw_acquisition_counts = {
        str(row[0]): int(row[1])
        for row in conn.execute(
            "SELECT fact_id,COUNT(*) FROM fact_acquisition_support "
            f"WHERE fact_id IN ({placeholders}) GROUP BY fact_id",
            tuple(fact_ids),
        )
    }
    cursor = conn.execute(
        "SELECT fas.fact_id,fas.support_role,a.id AS acquisition_session_id,a.source_id,"
        "a.collector_name,a.collector_version,a.status,a.started_at,a.finished_at,a.legacy_run_id,"
        "s.source_type,s.name AS source_name FROM fact_acquisition_support fas "
        "JOIN acquisition_sessions a ON a.id=fas.acquisition_session_id "
        "JOIN sources s ON s.id=a.source_id "
        f"WHERE fas.fact_id IN ({placeholders}) ORDER BY fas.fact_id,fas.support_role,a.id",
        tuple(fact_ids),
    )
    joined_acquisition_counts: dict[str, int] = {}
    for row in cursor.fetchall():
        item = row_dict(cursor, row)
        fact_id = str(item.pop("fact_id"))
        joined_acquisition_counts[fact_id] = joined_acquisition_counts.get(fact_id, 0) + 1
        fact_by_id[fact_id]["acquisition_support"].append(item)

    for fact_id, fact in fact_by_id.items():
        if raw_observation_counts.get(fact_id, 0) != joined_observation_counts.get(fact_id, 0):
            issues.append({"code": "broken_observation_provenance", "fact_id": fact_id})
        if raw_acquisition_counts.get(fact_id, 0) != joined_acquisition_counts.get(fact_id, 0):
            issues.append({"code": "broken_acquisition_provenance", "fact_id": fact_id})

        for support in fact["observation_support"]:
            if (
                support["observation_subject_id"] != fact["subject_id"]
                or support["observation_predicate"] != fact["predicate"]
            ):
                issues.append(
                    {
                        "code": "support_observation_semantic_mismatch",
                        "fact_id": fact_id,
                        "observation_id": support["observation_id"],
                    }
                )
            if support["evidence_status"] != "usable":
                issues.append(
                    {
                        "code": "fact_support_uses_nonusable_evidence",
                        "fact_id": fact_id,
                        "observation_id": support["observation_id"],
                        "evidence_id": support["evidence_id"],
                        "evidence_status": support["evidence_status"],
                    }
                )
            if (
                fact["status"] in {"confirmed", "single_source", "stale"}
                and support["support_role"] == "supports"
                and not _support_matches_fact(fact, support)
            ):
                issues.append(
                    {
                        "code": "support_observation_value_mismatch",
                        "fact_id": fact_id,
                        "observation_id": support["observation_id"],
                    }
                )

        if fact["status"] in {"confirmed", "single_source", "stale"} and not any(
            support["support_role"] == "supports" for support in fact["observation_support"]
        ):
            issues.append({"code": "value_fact_without_supporting_observation", "fact_id": fact_id})

        if fact["status"] == "conflicted":
            if len(fact["observation_support"]) < 2:
                issues.append(
                    {"code": "conflicted_fact_without_multiple_observations", "fact_id": fact_id}
                )
            semantic_values = {
                key
                for support in fact["observation_support"]
                if (
                    key := _semantic_key(
                        support["observation_value"],
                        support["observation_normalized_value"],
                        support["observation_value_hash"],
                    )
                )
                is not None
            }
            if len(semantic_values) < 2:
                issues.append(
                    {
                        "code": "conflicted_fact_without_distinct_observation_values",
                        "fact_id": fact_id,
                    }
                )

        if fact["status"] == "not_observed" and not fact["acquisition_support"]:
            issues.append({"code": "not_observed_without_acquisition_support", "fact_id": fact_id})

    for evidence in evidence_by_id.values():
        evidence["observations"].sort(key=lambda item: str(item["id"]))
    issues.sort(
        key=lambda item: (
            str(item["code"]),
            str(item.get("fact_id", "")),
            str(item.get("observation_id", "")),
        )
    )
    return [evidence_by_id[key] for key in sorted(evidence_by_id)], issues


def controlled_unknowns(
    conn: sqlite3.Connection,
    entity_id: str,
    current_location_ids: list[str],
    facts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    indexed: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for fact in facts:
        indexed.setdefault((str(fact["subject_id"]), str(fact["predicate"])), []).append(fact)

    cursor = conn.execute(
        "SELECT name,domain,subject_kind FROM predicate_definitions "
        "WHERE active=1 ORDER BY domain,name"
    )
    result: list[dict[str, Any]] = []
    for row in cursor.fetchall():
        predicate = row_dict(cursor, row)
        subjects = [entity_id] if predicate["subject_kind"] == "business_entity" else current_location_ids
        if not subjects:
            result.append(
                {
                    "domain": predicate["domain"],
                    "predicate": predicate["name"],
                    "subject_id": None,
                    "subject_kind": predicate["subject_kind"],
                    "state": "unresolved",
                    "reason": "no_current_subject_for_predicate_scope",
                }
            )
            continue
        for subject_id in subjects:
            matching = indexed.get((subject_id, predicate["name"]), [])
            if not matching:
                result.append(
                    {
                        "domain": predicate["domain"],
                        "predicate": predicate["name"],
                        "subject_id": subject_id,
                        "subject_kind": predicate["subject_kind"],
                        "state": "unresolved",
                        "reason": "no_current_fact",
                    }
                )
                continue
            for fact in matching:
                if fact["status"] in {"unknown", "not_observed"}:
                    result.append(
                        {
                            "domain": predicate["domain"],
                            "predicate": predicate["name"],
                            "subject_id": subject_id,
                            "subject_kind": predicate["subject_kind"],
                            "state": fact["status"],
                            "fact_id": fact["id"],
                            "reason": "explicit_fact_state",
                        }
                    )
    result.sort(
        key=lambda item: (
            str(item["domain"]),
            str(item["predicate"]),
            str(item.get("subject_id") or ""),
            str(item["state"]),
        )
    )
    return result
