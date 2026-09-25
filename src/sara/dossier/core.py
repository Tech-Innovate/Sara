from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from ..understanding_vocabulary import (
    VocabularySeedError,
    verify_business_understanding_vocabulary,
)


class DossierQueryError(RuntimeError):
    """The requested Business Understanding dossier cannot be read safely."""


VALUE_STATUSES = frozenset({"confirmed", "single_source", "conflicted", "stale"})
NULL_STATUSES = frozenset({"unknown", "not_observed", "not_applicable"})
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


def row_dict(cursor: sqlite3.Cursor, row: sqlite3.Row | tuple[Any, ...]) -> dict[str, Any]:
    if cursor.description is None:
        raise DossierQueryError("query returned no column metadata")
    return {description[0]: row[index] for index, description in enumerate(cursor.description)}


def parse_timestamp(value: object, *, field: str) -> datetime:
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


def evaluation_time(value: str | None) -> datetime:
    return datetime.now(timezone.utc) if value is None else parse_timestamp(value, field="evaluated_at")


def json_value(value: object, *, field: str) -> Any:
    if value is None:
        return None
    if not isinstance(value, str):
        raise DossierQueryError(f"{field} is not stored as JSON text")
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise DossierQueryError(f"{field} contains malformed JSON") from exc


def verify_schema(conn: sqlite3.Connection) -> None:
    try:
        verify_business_understanding_vocabulary(conn)
    except VocabularySeedError as exc:
        raise DossierQueryError(str(exc)) from exc
    placeholders = ",".join("?" for _ in _REQUIRED_OBJECTS)
    found = {
        (str(row[0]), str(row[1]))
        for row in conn.execute(
            f"SELECT name,type FROM sqlite_master WHERE name IN ({placeholders})",
            tuple(_REQUIRED_OBJECTS),
        )
    }
    missing = [
        name
        for name, object_type in _REQUIRED_OBJECTS.items()
        if (name, object_type) not in found
    ]
    if missing:
        raise DossierQueryError(
            "Business Understanding schema is incomplete; missing: " + ", ".join(sorted(missing))
        )


def subject(conn: sqlite3.Connection, subject_id: str, expected_kind: str) -> dict[str, Any]:
    cursor = conn.execute(
        "SELECT id,kind,record_state,merged_into_subject_id,created_at,updated_at,merged_at "
        "FROM knowledge_subjects WHERE id=?",
        (subject_id,),
    )
    row = cursor.fetchone()
    if row is None:
        raise DossierQueryError(f"missing Understanding subject {subject_id!r}")
    result = row_dict(cursor, row)
    if result["kind"] != expected_kind:
        raise DossierQueryError(
            f"Understanding subject {subject_id!r} has kind {result['kind']!r}, "
            f"expected {expected_kind!r}"
        )
    return result


def resolve_subject(conn: sqlite3.Connection, subject_id: str, expected_kind: str) -> dict[str, Any]:
    current = subject_id
    seen: set[str] = set()
    chain: list[str] = []
    while True:
        if current in seen:
            raise DossierQueryError(f"Understanding subject redirect cycle detected from {subject_id!r}")
        seen.add(current)
        chain.append(current)
        row = subject(conn, current, expected_kind)
        state = str(row["record_state"])
        target = row["merged_into_subject_id"]
        if state == "merged":
            if not isinstance(target, str) or not target:
                raise DossierQueryError(f"merged Understanding subject {current!r} has no merge target")
            current = target
            continue
        if state not in {"active", "retired"} or target is not None:
            raise DossierQueryError(f"Understanding subject {current!r} has inconsistent lifecycle state")
        return {"canonical": row, "chain": chain}


def location_owner(conn: sqlite3.Connection, location_id: str) -> str:
    row = conn.execute(
        "SELECT business_entity_id FROM business_locations WHERE id=?", (location_id,)
    ).fetchone()
    if row is None:
        raise DossierQueryError(f"missing business location {location_id!r}")
    resolved = resolve_subject(conn, str(row[0]), "business_entity")
    return str(resolved["canonical"]["id"])


def resolve_selection(
    conn: sqlite3.Connection,
    *,
    business_id: int | None,
    canonical_key: str | None,
    entity_id: str | None,
) -> tuple[str, dict[str, Any]]:
    if entity_id is not None:
        resolved = resolve_subject(conn, entity_id, "business_entity")
        return str(resolved["canonical"]["id"]), {
            "kind": "entity_id",
            "value": entity_id,
            "entity_resolution_chain": list(resolved["chain"]),
        }

    if business_id is not None:
        cursor = conn.execute(
            "SELECT id,canonical_key,title,last_seen_at FROM businesses WHERE id=?", (business_id,)
        )
        selector_kind = "business_id"
    else:
        assert canonical_key is not None
        cursor = conn.execute(
            "SELECT id,canonical_key,title,last_seen_at FROM businesses WHERE canonical_key=?",
            (canonical_key,),
        )
        selector_kind = "canonical_key"
    row = cursor.fetchone()
    if row is None:
        raise DossierQueryError("no canonical Maps business matches the requested selector")
    business = row_dict(cursor, row)
    link = conn.execute(
        "SELECT location_id,linked_at FROM maps_business_location_links WHERE business_id=?",
        (business["id"],),
    ).fetchone()
    if link is None:
        raise DossierQueryError(
            f"Maps business {business['id']} is not synchronized into Business Understanding; "
            "run sara-maps-sync explicitly before reading its dossier"
        )
    resolved_location = resolve_subject(conn, str(link[0]), "location")
    canonical_location = resolved_location["canonical"]
    if canonical_location["record_state"] != "active":
        raise DossierQueryError(
            f"current Maps business {business['id']} resolves to a retired location"
        )
    entity = location_owner(conn, str(canonical_location["id"]))
    resolved_entity = resolve_subject(conn, entity, "business_entity")
    canonical_entity = resolved_entity["canonical"]
    if canonical_entity["record_state"] != "active":
        raise DossierQueryError(f"current Maps business {business['id']} resolves to a retired entity")
    return str(canonical_entity["id"]), {
        "kind": selector_kind,
        "value": business["id"] if selector_kind == "business_id" else business["canonical_key"],
        "maps_business": business,
        "linked_location_id": str(link[0]),
        "canonical_location_id": str(canonical_location["id"]),
        "location_resolution_chain": list(resolved_location["chain"]),
        "entity_resolution_chain": list(resolved_entity["chain"]),
    }


def entity_record(conn: sqlite3.Connection, entity_id: str) -> dict[str, Any]:
    cursor = conn.execute(
        "SELECT be.id,be.display_name,be.entity_type,be.lifecycle_status,be.identity_confidence,"
        "be.created_at,be.updated_at,ks.record_state,ks.merged_into_subject_id,ks.merged_at "
        "FROM business_entities be JOIN knowledge_subjects ks ON ks.id=be.id WHERE be.id=?",
        (entity_id,),
    )
    row = cursor.fetchone()
    if row is None:
        raise DossierQueryError(f"missing business entity row for {entity_id!r}")
    return row_dict(cursor, row)


def identifier_rows(conn: sqlite3.Connection, location_id: str) -> list[dict[str, Any]]:
    cursor = conn.execute(
        "SELECT ei.id,ei.source_id,ei.namespace,ei.value,ei.status,ei.first_observed_at,"
        "ei.last_observed_at,ei.created_at,s.source_type,s.name AS source_name "
        "FROM external_identifiers ei JOIN sources s ON s.id=ei.source_id "
        "WHERE ei.subject_id=? ORDER BY ei.source_id,ei.namespace,ei.value,ei.id",
        (location_id,),
    )
    return [row_dict(cursor, row) for row in cursor.fetchall()]


def locations(conn: sqlite3.Connection, entity_id: str) -> tuple[list[dict[str, Any]], list[str]]:
    cursor = conn.execute(
        "SELECT bl.id,bl.business_entity_id,bl.label,bl.location_type,bl.created_at,bl.updated_at,"
        "ks.record_state,ks.merged_into_subject_id,ks.merged_at "
        "FROM business_locations bl JOIN knowledge_subjects ks ON ks.id=bl.id ORDER BY bl.id"
    )
    result: list[dict[str, Any]] = []
    current_ids: set[str] = set()
    for row in cursor.fetchall():
        item = row_dict(cursor, row)
        owner = resolve_subject(conn, str(item["business_entity_id"]), "business_entity")
        if str(owner["canonical"]["id"]) != entity_id:
            continue
        resolved_location = resolve_subject(conn, str(item["id"]), "location")
        canonical_location = resolved_location["canonical"]
        canonical_owner = location_owner(conn, str(canonical_location["id"]))
        is_current = (
            canonical_location["record_state"] == "active"
            and canonical_owner == entity_id
            and str(canonical_location["id"]) == str(item["id"])
        )
        if is_current:
            current_ids.add(str(item["id"]))
        item["canonical_location_id"] = str(canonical_location["id"])
        item["resolution_chain"] = list(resolved_location["chain"])
        item["current_for_entity"] = is_current
        item["external_identifiers"] = identifier_rows(conn, str(item["id"]))
        result.append(item)
    return result, sorted(current_ids)


def maps_businesses(conn: sqlite3.Connection, current_location_ids: list[str]) -> list[dict[str, Any]]:
    if not current_location_ids:
        return []
    current = set(current_location_ids)
    cursor = conn.execute(
        "SELECT b.id,b.canonical_key,b.title,b.last_seen_at,m.location_id,m.linked_at "
        "FROM maps_business_location_links m JOIN businesses b ON b.id=m.business_id ORDER BY b.id"
    )
    result: list[dict[str, Any]] = []
    for row in cursor.fetchall():
        item = row_dict(cursor, row)
        resolved = resolve_subject(conn, str(item["location_id"]), "location")
        canonical_location_id = str(resolved["canonical"]["id"])
        if canonical_location_id in current:
            item["canonical_location_id"] = canonical_location_id
            result.append(item)
    return result


def _freshness(fact: dict[str, Any], evaluated_at: datetime) -> dict[str, Any]:
    freshness_days = fact["freshness_days"]
    reference_text = fact["last_verified_at"] or fact["valid_from"]
    if fact["status"] in NULL_STATUSES or fact["status"] == "conflicted" or freshness_days is None:
        return {
            "evaluated": False,
            "freshness_days": freshness_days,
            "reference_at": reference_text,
            "stale_after": None,
            "is_stale": fact["status"] == "stale",
        }
    reference = parse_timestamp(reference_text, field=f"fact {fact['id']} freshness reference")
    stale_after = reference + timedelta(days=int(freshness_days))
    return {
        "evaluated": True,
        "freshness_days": int(freshness_days),
        "reference_at": reference.isoformat(),
        "stale_after": stale_after.isoformat(),
        "is_stale": fact["status"] == "stale" or evaluated_at > stale_after,
    }


def current_facts(
    conn: sqlite3.Connection,
    entity_id: str,
    current_location_ids: list[str],
    evaluated_at: datetime,
) -> list[dict[str, Any]]:
    subject_ids = [entity_id, *current_location_ids]
    placeholders = ",".join("?" for _ in subject_ids)
    cursor = conn.execute(
        "SELECT f.id,f.subject_id,ks.kind AS subject_kind,f.predicate,pd.domain,pd.cardinality,"
        "pd.value_type,pd.freshness_days,f.fact_slot,f.value_json,f.normalized_value_json,"
        "f.value_hash,f.status,f.valid_from,f.valid_to,f.last_verified_at,f.reconciled_at,"
        "f.reconciliation_version,f.created_at "
        "FROM facts f JOIN predicate_definitions pd ON pd.name=f.predicate "
        "JOIN knowledge_subjects ks ON ks.id=f.subject_id "
        f"WHERE f.valid_to IS NULL AND f.subject_id IN ({placeholders}) "
        "ORDER BY pd.domain,f.subject_id,f.predicate,f.fact_slot,f.id",
        tuple(subject_ids),
    )
    result: list[dict[str, Any]] = []
    for row in cursor.fetchall():
        fact = row_dict(cursor, row)
        fact["value"] = json_value(fact.pop("value_json"), field=f"fact {fact['id']} value_json")
        fact["normalized_value"] = json_value(
            fact.pop("normalized_value_json"), field=f"fact {fact['id']} normalized_value_json"
        )
        fact["freshness"] = _freshness(fact, evaluated_at)
        fact["observation_support"] = []
        fact["acquisition_support"] = []
        result.append(fact)
    return result
