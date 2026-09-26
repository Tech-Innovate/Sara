from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from . import maps_backfill as mb
from .migrations import MigrationError, apply_migrations
from .storage import connect_existing
from .understanding_vocabulary import (
    VocabularySeedError,
    seed_business_understanding_vocabulary,
    verify_business_understanding_vocabulary,
)


class MapsSyncError(RuntimeError):
    """The current Maps canonical state cannot be synchronized safely."""


@dataclass(frozen=True)
class MapsSyncStats:
    business_count: int
    entities_created: int
    locations_created: int
    links_created: int
    links_repointed: int
    locations_merged: int
    external_identifiers_created: int
    external_identifiers_refreshed: int
    acquisition_sessions_created: int
    evidence_items_created: int
    observations_created: int
    facts_created: int
    fact_support_links_created: int
    fact_updates_deferred: int
    already_synchronized: bool


SYNC_COLLECTOR_NAME = "sara.maps_sync"
SYNC_VERSION = "2"
SYNC_RECONCILIATION_VERSION = "maps-sync-v2"
_IDENTIFIER_NAMESPACES = ("place_id", "cid", "data_id")
_SYNC_METADATA_KEYS = {
    "import_kind",
    "legacy_business_id",
    "legacy_canonical_key",
    "legacy_run_id",
    "sync_external_identifiers",
    "sync_entity_id",
    "sync_location_id",
    "reconciliation_manifest",
    "raw_json",
}


def _identifier_snapshot(business: dict[str, Any]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for namespace in _IDENTIFIER_NAMESPACES:
        value = mb._text(business.get(namespace))
        if value:
            result.append({"namespace": namespace, "value": value})
    return result


def _parse_identifier_snapshot(evidence_id: object, value: object) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise MapsSyncError(f"sync evidence {evidence_id} has no valid identifier snapshot")
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict) or set(item) != {"namespace", "value"}:
            raise MapsSyncError(f"sync evidence {evidence_id} has malformed identifier snapshot")
        namespace = item.get("namespace")
        identifier = item.get("value")
        if (
            namespace not in _IDENTIFIER_NAMESPACES
            or namespace in seen
            or not isinstance(identifier, str)
            or not identifier
            or identifier != identifier.strip()
        ):
            raise MapsSyncError(f"sync evidence {evidence_id} has malformed identifier snapshot")
        seen.add(namespace)
        result.append({"namespace": namespace, "value": identifier})
    expected_order = [namespace for namespace in _IDENTIFIER_NAMESPACES if namespace in seen]
    if [item["namespace"] for item in result] != expected_order:
        raise MapsSyncError(f"sync evidence {evidence_id} has non-canonical identifier snapshot")
    return result


def _ensure_source(conn: sqlite3.Connection, created_at: str) -> None:
    row = mb._fetch_one(
        conn,
        "SELECT source_type FROM sources WHERE id=?",
        (mb.GOOGLE_MAPS_SOURCE_ID,),
    )
    if row is None:
        conn.execute(
            "INSERT INTO sources(id,source_type,name,base_url,created_at,active) "
            "VALUES (?,'google_maps','Google Maps',NULL,?,1)",
            (mb.GOOGLE_MAPS_SOURCE_ID, created_at),
        )
    elif row["source_type"] != "google_maps":
        raise MapsSyncError(f"Google Maps source identity/type drift: database={row!r}")


def _latest_run(conn: sqlite3.Connection, business: dict[str, Any]) -> dict[str, Any]:
    try:
        return mb._latest_run(conn, business)
    except mb.MapsBackfillError as exc:
        raise MapsSyncError(str(exc)) from exc


def _parse_raw(business: dict[str, Any]) -> dict[str, Any]:
    try:
        return mb._parse_raw(business)
    except mb.MapsBackfillError as exc:
        raise MapsSyncError(str(exc)) from exc


def _subject(conn: sqlite3.Connection, subject_id: str, kind: str) -> dict[str, Any]:
    row = mb._fetch_one(
        conn,
        "SELECT id,kind,record_state,merged_into_subject_id,created_at "
        "FROM knowledge_subjects WHERE id=?",
        (subject_id,),
    )
    if row is None:
        raise MapsSyncError(f"missing Understanding subject {subject_id!r}")
    if row["kind"] != kind:
        raise MapsSyncError(
            f"Understanding subject {subject_id!r} has kind {row['kind']!r}, expected {kind!r}"
        )
    return row


def _resolve_subject(conn: sqlite3.Connection, subject_id: str, kind: str) -> str:
    current = subject_id
    seen: set[str] = set()
    while True:
        if current in seen:
            raise MapsSyncError(f"Understanding subject redirect cycle detected from {subject_id!r}")
        seen.add(current)
        row = _subject(conn, current, kind)
        state = str(row["record_state"])
        target = row["merged_into_subject_id"]
        if state == "active":
            if target is not None:
                raise MapsSyncError(f"active Understanding subject {current!r} unexpectedly redirects")
            return current
        if state == "merged":
            if not isinstance(target, str) or not target:
                raise MapsSyncError(f"merged Understanding subject {current!r} has no merge target")
            current = target
            continue
        raise MapsSyncError(f"current Maps state resolves through retired Understanding subject {current!r}")


def _location_owner(conn: sqlite3.Connection, location_id: str) -> str:
    row = mb._fetch_one(
        conn,
        "SELECT business_entity_id FROM business_locations WHERE id=?",
        (location_id,),
    )
    if row is None:
        raise MapsSyncError(f"missing business location {location_id!r}")
    return _resolve_subject(conn, str(row["business_entity_id"]), "business_entity")


def _linked_business(conn: sqlite3.Connection, location_id: str) -> int | None:
    row = mb._fetch_one(
        conn,
        "SELECT business_id FROM maps_business_location_links WHERE location_id=?",
        (location_id,),
    )
    return None if row is None else int(row["business_id"])


def _repoint_link(
    conn: sqlite3.Connection,
    *,
    business_id: int,
    from_location_id: str,
    to_location_id: str,
) -> bool:
    if from_location_id == to_location_id:
        return False
    other = _linked_business(conn, to_location_id)
    if other is not None and other != business_id:
        raise MapsSyncError(
            f"cannot repoint Maps business {business_id} to location {to_location_id}; "
            f"it is already linked to current Maps business {other}"
        )
    cursor = conn.execute(
        "UPDATE maps_business_location_links SET location_id=? "
        "WHERE business_id=? AND location_id=?",
        (to_location_id, business_id, from_location_id),
    )
    if cursor.rowcount != 1:
        raise MapsSyncError(f"Maps link for business {business_id} changed during synchronization")
    return True


def _merge_location(
    conn: sqlite3.Connection,
    *,
    source_location_id: str,
    target_location_id: str,
    current_business_id: int,
    merged_at: str,
) -> bool:
    source = _resolve_subject(conn, source_location_id, "location")
    target = _resolve_subject(conn, target_location_id, "location")
    if source == target:
        return False

    source_business = _linked_business(conn, source)
    if source_business is not None:
        if source_business != current_business_id:
            raise MapsSyncError(
                f"provider-identity convergence would merge location {source!r} "
                f"still linked to distinct current Maps business {source_business}"
            )
        _repoint_link(
            conn,
            business_id=current_business_id,
            from_location_id=source,
            to_location_id=target,
        )
    target_business = _linked_business(conn, target)
    if target_business is not None and target_business != current_business_id:
        raise MapsSyncError(
            f"provider-identity convergence target {target!r} is linked to "
            f"distinct current Maps business {target_business}"
        )

    cursor = conn.execute(
        "UPDATE knowledge_subjects "
        "SET record_state='merged',merged_into_subject_id=?,merged_at=?,updated_at=? "
        "WHERE id=? AND kind='location' AND record_state='active'",
        (target, merged_at, merged_at, source),
    )
    if cursor.rowcount != 1:
        raise MapsSyncError(f"failed to record location convergence {source!r} -> {target!r}")
    return True


def _select_location_target(conn: sqlite3.Connection, locations: set[str]) -> str:
    if not locations:
        raise MapsSyncError("cannot select a Location target from an empty set")
    candidates: list[tuple[str, str]] = []
    for location_id in locations:
        canonical = _resolve_subject(conn, location_id, "location")
        row = _subject(conn, canonical, "location")
        candidates.append((str(row["created_at"]), canonical))
    return min(candidates)[1]


def _create_anchor(
    conn: sqlite3.Connection, business: dict[str, Any], created_at: str
) -> tuple[str, str]:
    business_id = int(business["id"])
    entity_id = mb.business_entity_id_for_maps_business(business_id)
    location_id = mb.location_id_for_maps_business(business_id)
    for subject_id in (entity_id, location_id):
        if conn.execute("SELECT 1 FROM knowledge_subjects WHERE id=?", (subject_id,)).fetchone():
            raise MapsSyncError(
                f"deterministic subject id collision for Maps business {business_id}: {subject_id}"
            )
    conn.execute(
        "INSERT INTO knowledge_subjects(id,kind,created_at,updated_at) "
        "VALUES (?,'business_entity',?,?)",
        (entity_id, created_at, created_at),
    )
    conn.execute(
        "INSERT INTO business_entities("
        "id,display_name,entity_type,lifecycle_status,identity_confidence,created_at,updated_at"
        ") VALUES (?,?,'unknown','unknown',NULL,?,?)",
        (entity_id, business.get("title"), created_at, created_at),
    )
    conn.execute(
        "INSERT INTO knowledge_subjects(id,kind,created_at,updated_at) "
        "VALUES (?,'location',?,?)",
        (location_id, created_at, created_at),
    )
    conn.execute(
        "INSERT INTO business_locations("
        "id,business_entity_id,label,location_type,created_at,updated_at"
        ") VALUES (?,?,NULL,'unknown',?,?)",
        (location_id, entity_id, created_at, created_at),
    )
    return entity_id, location_id


def _link_business(
    conn: sqlite3.Connection,
    *,
    business_id: int,
    location_id: str,
    linked_at: str,
) -> None:
    other = _linked_business(conn, location_id)
    if other is not None and other != business_id:
        raise MapsSyncError(
            f"location {location_id!r} is already linked to current Maps business {other}; "
            "acquisition-layer convergence is required first"
        )
    conn.execute(
        "INSERT INTO maps_business_location_links(business_id,location_id,linked_at) VALUES (?,?,?)",
        (business_id, location_id, linked_at),
    )


def _identifier_row(conn: sqlite3.Connection, namespace: str, value: str) -> dict[str, Any] | None:
    return mb._fetch_one(
        conn,
        "SELECT id,subject_id,source_id,namespace,value,status,first_observed_at,last_observed_at,created_at "
        "FROM external_identifiers WHERE source_id=? AND namespace=? AND value=?",
        (mb.GOOGLE_MAPS_SOURCE_ID, namespace, value),
    )


def _ensure_identifier(
    conn: sqlite3.Connection,
    *,
    business: dict[str, Any],
    raw: dict[str, Any],
    target_location_id: str,
    namespace: str,
    value: str,
    sync_at: str,
) -> tuple[int, int, int]:
    expected_id = mb._external_identifier_id(namespace, value)
    row = _identifier_row(conn, namespace, value)
    if row is None:
        observed_at = (
            str(business["last_seen_at"])
            if mb._text(raw.get(namespace)) == value
            else sync_at
        )
        conn.execute(
            "INSERT INTO external_identifiers("
            "id,subject_id,source_id,namespace,value,status,first_observed_at,last_observed_at,created_at"
            ") VALUES (?,?,?,?,?,'active',?,?,?)",
            (
                expected_id,
                target_location_id,
                mb.GOOGLE_MAPS_SOURCE_ID,
                namespace,
                value,
                observed_at,
                observed_at,
                sync_at,
            ),
        )
        return 1, 0, 0
    if row["id"] != expected_id:
        raise MapsSyncError(
            f"current Maps identifier {namespace}={value!r} has non-deterministic Understanding identity"
        )
    if row["status"] != "active":
        raise MapsSyncError(
            f"current Maps identifier {namespace}={value!r} is {row['status']!r} in the Understanding layer"
        )
    canonical = _resolve_subject(conn, str(row["subject_id"]), "location")
    merged = 0
    if canonical != target_location_id:
        merged = int(
            _merge_location(
                conn,
                source_location_id=canonical,
                target_location_id=target_location_id,
                current_business_id=int(business["id"]),
                merged_at=sync_at,
            )
        )
    refreshed = 0
    if mb._text(raw.get(namespace)) == value:
        observed_at = str(business["last_seen_at"])
        if str(row["last_observed_at"]) < observed_at:
            conn.execute(
                "UPDATE external_identifiers SET last_observed_at=? WHERE id=?",
                (observed_at, row["id"]),
            )
            refreshed = 1
    return 0, refreshed, merged


def _session_counts(conn: sqlite3.Connection, session_id: str) -> tuple[int, int]:
    evidence_count = int(
        conn.execute(
            "SELECT COUNT(*) FROM evidence_items WHERE acquisition_session_id=?",
            (session_id,),
        ).fetchone()[0]
    )
    observation_count = int(
        conn.execute(
            "SELECT COUNT(*) FROM observations o JOIN evidence_items e ON e.id=o.evidence_id "
            "WHERE e.acquisition_session_id=?",
            (session_id,),
        ).fetchone()[0]
    )
    return evidence_count, observation_count


def _ensure_session(
    conn: sqlite3.Connection, run: dict[str, Any]
) -> tuple[str, str, bool]:
    run_id = str(run["id"])
    session_id = mb._acquisition_id(run_id)
    row = mb._fetch_one(
        conn,
        "SELECT id,target_subject_id,source_id,collector_name,collector_version,config_json,"
        "config_hash,status,started_at,finished_at,error,legacy_run_id,evidence_count,observation_count "
        "FROM acquisition_sessions WHERE legacy_run_id=?",
        (run_id,),
    )
    backfill_config = mb._canonical_json(
        {"import_mode": "latest_canonical_maps_snapshot", "legacy_run_id": run_id}
    )
    sync_config = mb._canonical_json(
        {"import_mode": "maps_sync_current_snapshot", "legacy_run_id": run_id}
    )
    if row is not None:
        collector = str(row["collector_name"])
        version = str(row["collector_version"])
        config_json = str(row["config_json"])
        if (collector, version, config_json) == (
            mb.BACKFILL_COLLECTOR_NAME,
            mb.BACKFILL_VERSION,
            backfill_config,
        ):
            session_kind = "phase3"
        elif (collector, version, config_json) == (
            SYNC_COLLECTOR_NAME,
            SYNC_VERSION,
            sync_config,
        ):
            session_kind = "sync"
        else:
            raise MapsSyncError(
                f"existing acquisition session for legacy run {run_id!r} is incompatible with Maps synchronization"
            )
        if (
            row["id"] != session_id
            or row["target_subject_id"] is not None
            or row["source_id"] != mb.GOOGLE_MAPS_SOURCE_ID
            or row["config_hash"] != mb._sha256_text(config_json)
            or row["status"] != "complete"
            or str(row["started_at"]) != str(run["started_at"])
            or str(row["finished_at"]) != str(run["finished_at"])
            or row["error"] not in (None, "")
            or str(row["legacy_run_id"]) != run_id
        ):
            raise MapsSyncError(
                f"existing acquisition session for legacy run {run_id!r} is incompatible with Maps synchronization"
            )
        actual_counts = _session_counts(conn, session_id)
        stored_counts = (int(row["evidence_count"]), int(row["observation_count"]))
        if stored_counts != actual_counts:
            raise MapsSyncError(
                f"acquisition session {session_id!r} count drift: database={stored_counts!r}, actual={actual_counts!r}"
            )
        return session_id, session_kind, False

    collision = mb._fetch_one(
        conn, "SELECT legacy_run_id FROM acquisition_sessions WHERE id=?", (session_id,)
    )
    if collision is not None:
        raise MapsSyncError(f"deterministic acquisition session id collision for run {run_id!r}")
    conn.execute(
        "INSERT INTO acquisition_sessions("
        "id,target_subject_id,source_id,collector_name,collector_version,config_json,config_hash,"
        "status,started_at,finished_at,error,legacy_run_id,evidence_count,observation_count"
        ") VALUES (?,NULL,?,?,?,?,?,'complete',?,?,NULL,?,0,0)",
        (
            session_id,
            mb.GOOGLE_MAPS_SOURCE_ID,
            SYNC_COLLECTOR_NAME,
            SYNC_VERSION,
            sync_config,
            mb._sha256_text(sync_config),
            run["started_at"],
            run["finished_at"],
            run_id,
        ),
    )
    return session_id, "sync", True


def _refresh_session_counts(conn: sqlite3.Connection, session_id: str) -> None:
    evidence_count, observation_count = _session_counts(conn, session_id)
    conn.execute(
        "UPDATE acquisition_sessions SET evidence_count=?,observation_count=? WHERE id=?",
        (evidence_count, observation_count, session_id),
    )


def _snapshot_observations(
    raw: dict[str, Any], *, entity_id: str, location_id: str
) -> list[dict[str, Any]]:
    values = mb._source_values(raw)
    result: list[dict[str, Any]] = []
    for field, subject_kind, predicate in mb._FIELD_SPECS:
        value = values[field]
        if value is None:
            continue
        value_json = mb._canonical_json(value)
        value_hash = mb._sha256_text(value_json)
        cardinality = mb._PREDICATE_CARDINALITY.get(predicate)
        if cardinality not in {"single", "multi"}:
            raise MapsSyncError(f"unsupported predicate cardinality for {predicate!r}")
        result.append(
            {
                "subject_id": entity_id if subject_kind == "business_entity" else location_id,
                "predicate": predicate,
                "value_json": value_json,
                "value_hash": value_hash,
                "fact_slot": "__single__" if cardinality == "single" else value_hash,
            }
        )
    return result


def _current_fact(
    conn: sqlite3.Connection, *, subject_id: str, predicate: str, fact_slot: str
) -> dict[str, Any] | None:
    return mb._fetch_one(
        conn,
        "SELECT id,value_json,normalized_value_json,value_hash,status,valid_from,"
        "last_verified_at,reconciliation_version FROM facts "
        "WHERE subject_id=? AND predicate=? AND fact_slot=? AND valid_to IS NULL",
        (subject_id, predicate, fact_slot),
    )


def _fact_is_maps_only(conn: sqlite3.Connection, fact_id: str) -> bool:
    support_count = int(
        conn.execute(
            "SELECT COUNT(*) FROM fact_observation_support WHERE fact_id=?",
            (fact_id,),
        ).fetchone()[0]
    )
    bad_count = int(
        conn.execute(
            "SELECT COUNT(*) FROM fact_observation_support fos "
            "JOIN observations o ON o.id=fos.observation_id "
            "JOIN evidence_items e ON e.id=o.evidence_id "
            "WHERE fos.fact_id=? AND (fos.support_role<>'supports' OR e.source_id<>?)",
            (fact_id, mb.GOOGLE_MAPS_SOURCE_ID),
        ).fetchone()[0]
    )
    return support_count > 0 and bad_count == 0


def _sync_fact_id(observation_id: str) -> str:
    return mb._opaque_id("fact", observation_id, SYNC_RECONCILIATION_VERSION)


def _plan_reconciliation(
    conn: sqlite3.Connection,
    *,
    observation_id: str,
    observation: dict[str, Any],
    observed_at: str,
) -> dict[str, Any]:
    current = _current_fact(
        conn,
        subject_id=str(observation["subject_id"]),
        predicate=str(observation["predicate"]),
        fact_slot=str(observation["fact_slot"]),
    )
    if current is None:
        outcome = "created_fact"
        fact_id: str | None = _sync_fact_id(observation_id)
        replaced: str | None = None
    else:
        same_value = (
            current["value_json"] == observation["value_json"]
            and current["normalized_value_json"] == observation["value_json"]
            and current["value_hash"] == observation["value_hash"]
        )
        maps_replaceable = (
            current["status"] in {"single_source", "stale"}
            and current["reconciliation_version"]
            in {mb.RECONCILIATION_VERSION, SYNC_RECONCILIATION_VERSION}
            and _fact_is_maps_only(conn, str(current["id"]))
        )
        if same_value and not maps_replaceable:
            outcome = "supported_existing_fact"
            fact_id = str(current["id"])
            replaced = None
        elif not maps_replaceable or str(observed_at) < str(current["valid_from"]):
            outcome = "deferred"
            fact_id = None
            replaced = None
        else:
            outcome = "replaced_maps_fact"
            fact_id = _sync_fact_id(observation_id)
            replaced = str(current["id"])
    return {
        "predicate": observation["predicate"],
        "fact_slot": observation["fact_slot"],
        "observation_id": observation_id,
        "outcome": outcome,
        "fact_id": fact_id,
        "replaced_fact_id": replaced,
    }


def _insert_sync_fact(
    conn: sqlite3.Connection,
    *,
    fact_id: str,
    observation: dict[str, Any],
    observed_at: str,
    created_at: str,
) -> None:
    conn.execute(
        "INSERT INTO facts("
        "id,subject_id,predicate,fact_slot,value_json,normalized_value_json,value_hash,status,"
        "valid_from,valid_to,last_verified_at,reconciled_at,reconciliation_version,created_at"
        ") VALUES (?,?,?,?,?,?,?,'single_source',?,NULL,?,?,?,?)",
        (
            fact_id,
            observation["subject_id"],
            observation["predicate"],
            observation["fact_slot"],
            observation["value_json"],
            observation["value_json"],
            observation["value_hash"],
            observed_at,
            observed_at,
            created_at,
            SYNC_RECONCILIATION_VERSION,
            created_at,
        ),
    )


def _execute_reconciliation(
    conn: sqlite3.Connection,
    *,
    plan: dict[str, Any],
    observation: dict[str, Any],
    observed_at: str,
    created_at: str,
) -> tuple[int, int, int]:
    outcome = str(plan["outcome"])
    observation_id = str(plan["observation_id"])
    if outcome == "deferred":
        return 0, 0, 1
    if outcome == "supported_existing_fact":
        conn.execute(
            "INSERT INTO fact_observation_support(fact_id,observation_id,support_role) "
            "VALUES (?,?,'supports')",
            (plan["fact_id"], observation_id),
        )
        return 0, 1, 0
    if outcome == "replaced_maps_fact":
        cursor = conn.execute(
            "UPDATE facts SET valid_to=? WHERE id=? AND valid_to IS NULL",
            (observed_at, plan["replaced_fact_id"]),
        )
        if cursor.rowcount != 1:
            raise MapsSyncError(
                f"current fact {plan['replaced_fact_id']!r} changed during Maps synchronization"
            )
    if outcome not in {"created_fact", "replaced_maps_fact"}:
        raise MapsSyncError(f"unsupported reconciliation outcome {outcome!r}")
    fact_id = str(plan["fact_id"])
    _insert_sync_fact(
        conn,
        fact_id=fact_id,
        observation=observation,
        observed_at=observed_at,
        created_at=created_at,
    )
    conn.execute(
        "INSERT INTO fact_observation_support(fact_id,observation_id,support_role) "
        "VALUES (?,?,'supports')",
        (fact_id, observation_id),
    )
    return 1, 1, 0


def _support_exists(conn: sqlite3.Connection, fact_id: str, observation_id: str) -> bool:
    row = conn.execute(
        "SELECT support_role FROM fact_observation_support WHERE fact_id=? AND observation_id=?",
        (fact_id, observation_id),
    ).fetchone()
    return row is not None and row["support_role"] == "supports"


def _verify_observations(
    conn: sqlite3.Connection,
    *,
    evidence_id: str,
    observations: list[dict[str, Any]],
    observation_ids: list[str],
    observed_at: str,
    extraction_method: str,
    extractor_name: str,
    extractor_version: str,
) -> None:
    expected: list[dict[str, Any]] = []
    for observation, observation_id in zip(observations, observation_ids):
        expected.append(
            {
                "id": observation_id,
                "subject_id": observation["subject_id"],
                "predicate": observation["predicate"],
                "evidence_id": evidence_id,
                "value_json": observation["value_json"],
                "normalized_value_json": observation["value_json"],
                "value_hash": observation["value_hash"],
                "observation_kind": "structured_value",
                "observed_at": observed_at,
                "extraction_method": extraction_method,
                "extractor_name": extractor_name,
                "extractor_version": extractor_version,
            }
        )
    expected.sort(key=lambda item: str(item["id"]))
    cursor = conn.execute(
        "SELECT id,subject_id,predicate,evidence_id,value_json,normalized_value_json,value_hash,"
        "observation_kind,observed_at,extraction_method,extractor_name,extractor_version "
        "FROM observations WHERE evidence_id=? ORDER BY id",
        (evidence_id,),
    )
    actual = [mb._row_dict(cursor, row) for row in cursor.fetchall()]
    if actual != expected:
        raise MapsSyncError(
            f"snapshot evidence {evidence_id} observation provenance is incomplete or inconsistent"
        )


def _verify_fact_core(
    conn: sqlite3.Connection,
    *,
    fact_id: str,
    observation: dict[str, Any],
    observed_at: str,
    reconciliation_version: str,
    observation_id: str,
    evidence_id: str,
) -> None:
    fact = mb._fetch_one(
        conn,
        "SELECT id,subject_id,predicate,fact_slot,value_json,normalized_value_json,value_hash,"
        "status,valid_from,last_verified_at,reconciliation_version FROM facts WHERE id=?",
        (fact_id,),
    )
    expected = {
        "id": fact_id,
        "subject_id": observation["subject_id"],
        "predicate": observation["predicate"],
        "fact_slot": observation["fact_slot"],
        "value_json": observation["value_json"],
        "normalized_value_json": observation["value_json"],
        "value_hash": observation["value_hash"],
        "status": "single_source",
        "valid_from": observed_at,
        "last_verified_at": observed_at,
        "reconciliation_version": reconciliation_version,
    }
    if fact != expected or not _support_exists(conn, fact_id, observation_id):
        raise MapsSyncError(
            f"snapshot evidence {evidence_id} fact/support provenance is incomplete "
            f"or inconsistent for predicate {observation['predicate']}"
        )


def _evidence_row(conn: sqlite3.Connection, evidence_id: str) -> dict[str, Any] | None:
    return mb._fetch_one(
        conn,
        "SELECT id,acquisition_session_id,source_id,source_locator,source_role,status,retrieved_at,"
        "published_at,language,media_type,content_sha256,artifact_ref,metadata_json,created_at "
        "FROM evidence_items WHERE id=?",
        (evidence_id,),
    )


def _verify_common_evidence(
    evidence: dict[str, Any],
    *,
    evidence_id: str,
    session_id: str,
    business: dict[str, Any],
    run: dict[str, Any],
    raw: dict[str, Any],
    content_hash: str,
) -> dict[str, Any]:
    expected = {
        "id": evidence_id,
        "acquisition_session_id": session_id,
        "source_id": mb.GOOGLE_MAPS_SOURCE_ID,
        "source_locator": mb._source_locator(raw),
        "source_role": "platform",
        "status": "usable",
        "retrieved_at": business["last_seen_at"],
        "published_at": None,
        "language": None,
        "media_type": "application/json",
        "content_sha256": content_hash,
        "artifact_ref": run.get("raw_path"),
    }
    actual = {key: evidence[key] for key in expected}
    if actual != expected:
        raise MapsSyncError(f"snapshot evidence {evidence_id} provenance is incomplete or inconsistent")
    try:
        metadata = json.loads(str(evidence["metadata_json"]))
    except json.JSONDecodeError as exc:
        raise MapsSyncError(f"snapshot evidence {evidence_id} has malformed metadata_json") from exc
    if not isinstance(metadata, dict):
        raise MapsSyncError(f"snapshot evidence {evidence_id} metadata_json is not an object")
    raw_json = metadata.get("raw_json")
    if (
        not isinstance(raw_json, str)
        or raw_json != str(business["raw_json"])
        or mb._sha256_text(raw_json) != content_hash
    ):
        raise MapsSyncError(f"snapshot evidence {evidence_id} raw content provenance is inconsistent")
    if metadata.get("legacy_business_id") != int(business["id"]):
        raise MapsSyncError(f"snapshot evidence {evidence_id} business provenance is inconsistent")
    if str(metadata.get("legacy_run_id")) != str(run["id"]):
        raise MapsSyncError(f"snapshot evidence {evidence_id} run provenance is inconsistent")
    if metadata.get("legacy_canonical_key") != business["canonical_key"]:
        raise MapsSyncError(f"snapshot evidence {evidence_id} canonical-key provenance is inconsistent")
    return metadata


def _verify_phase3_identifier_rows(
    conn: sqlite3.Connection,
    *,
    evidence: dict[str, Any],
    evidence_id: str,
    location_id: str,
    identifiers: list[dict[str, str]],
) -> None:
    for identifier in identifiers:
        namespace = identifier["namespace"]
        value = identifier["value"]
        row = _identifier_row(conn, namespace, value)
        expected = {
            "id": mb._external_identifier_id(namespace, value),
            "subject_id": location_id,
            "source_id": mb.GOOGLE_MAPS_SOURCE_ID,
            "namespace": namespace,
            "value": value,
            "first_observed_at": evidence["created_at"],
            "created_at": evidence["created_at"],
        }
        if row is None or {key: row[key] for key in expected} != expected:
            raise MapsSyncError(
                f"Phase-3 snapshot evidence {evidence_id} external identifier provenance "
                f"is incomplete or inconsistent for namespace {namespace}"
            )


def _verify_sync_identifier_rows(
    conn: sqlite3.Connection,
    *,
    evidence_id: str,
    location_id: str,
    identifiers: list[dict[str, str]],
) -> None:
    canonical_location = _resolve_subject(conn, location_id, "location")
    for identifier in identifiers:
        namespace = identifier["namespace"]
        value = identifier["value"]
        row = _identifier_row(conn, namespace, value)
        if (
            row is None
            or row["id"] != mb._external_identifier_id(namespace, value)
            or row["source_id"] != mb.GOOGLE_MAPS_SOURCE_ID
            or row["namespace"] != namespace
            or row["value"] != value
            or _resolve_subject(conn, str(row["subject_id"]), "location") != canonical_location
        ):
            raise MapsSyncError(
                f"sync evidence {evidence_id} external identifier provenance is incomplete "
                f"or inconsistent for namespace {namespace}"
            )


def _verify_phase3_snapshot(
    conn: sqlite3.Connection,
    *,
    evidence: dict[str, Any],
    session_id: str,
    business: dict[str, Any],
    run: dict[str, Any],
    raw: dict[str, Any],
    content_hash: str,
) -> None:
    business_id = int(business["id"])
    evidence_id = mb._evidence_id(business_id, content_hash)
    if evidence["id"] != evidence_id:
        raise MapsSyncError(f"Phase-3 evidence for business {business_id} has non-deterministic identity")
    metadata = _verify_common_evidence(
        evidence,
        evidence_id=evidence_id,
        session_id=session_id,
        business=business,
        run=run,
        raw=raw,
        content_hash=content_hash,
    )
    if metadata.get("import_kind") != "legacy_maps_business_snapshot":
        raise MapsSyncError(f"Phase-3 evidence {evidence_id} has incompatible import kind")
    try:
        phase3_identifiers = mb._parse_phase3_identifier_snapshot(evidence_id, metadata)
    except mb.MapsBackfillError as exc:
        raise MapsSyncError(str(exc)) from exc

    entity_id = mb.business_entity_id_for_maps_business(business_id)
    location_id = mb.location_id_for_maps_business(business_id)
    _subject(conn, entity_id, "business_entity")
    _subject(conn, location_id, "location")
    _verify_phase3_identifier_rows(
        conn,
        evidence=evidence,
        evidence_id=evidence_id,
        location_id=location_id,
        identifiers=phase3_identifiers,
    )
    observations = _snapshot_observations(raw, entity_id=entity_id, location_id=location_id)
    observation_ids = [
        mb._observation_id(evidence_id, observation["predicate"])
        for observation in observations
    ]
    observed_at = str(business["last_seen_at"])
    _verify_observations(
        conn,
        evidence_id=evidence_id,
        observations=observations,
        observation_ids=observation_ids,
        observed_at=observed_at,
        extraction_method="legacy_import",
        extractor_name=mb.BACKFILL_COLLECTOR_NAME,
        extractor_version=mb.BACKFILL_VERSION,
    )
    for observation, observation_id in zip(observations, observation_ids):
        _verify_fact_core(
            conn,
            fact_id=mb._fact_id(observation_id),
            observation=observation,
            observed_at=observed_at,
            reconciliation_version=mb.RECONCILIATION_VERSION,
            observation_id=observation_id,
            evidence_id=evidence_id,
        )


def _sync_evidence_id(business_id: int, run_id: str, content_hash: str) -> str:
    return mb._opaque_id("ev", "maps-sync", business_id, run_id, content_hash)


def _verify_sync_snapshot(
    conn: sqlite3.Connection,
    *,
    evidence: dict[str, Any],
    session_id: str,
    business: dict[str, Any],
    run: dict[str, Any],
    raw: dict[str, Any],
    content_hash: str,
) -> None:
    business_id = int(business["id"])
    evidence_id = _sync_evidence_id(business_id, str(run["id"]), content_hash)
    if evidence["id"] != evidence_id:
        raise MapsSyncError(f"sync evidence for business {business_id} has non-deterministic identity")
    metadata = _verify_common_evidence(
        evidence,
        evidence_id=evidence_id,
        session_id=session_id,
        business=business,
        run=run,
        raw=raw,
        content_hash=content_hash,
    )
    if set(metadata) != _SYNC_METADATA_KEYS or metadata.get("import_kind") != "maps_sync_snapshot":
        raise MapsSyncError(f"sync evidence {evidence_id} metadata contract is inconsistent")
    sync_identifiers = _parse_identifier_snapshot(
        evidence_id, metadata.get("sync_external_identifiers")
    )
    entity_id = metadata.get("sync_entity_id")
    location_id = metadata.get("sync_location_id")
    if not isinstance(entity_id, str) or not isinstance(location_id, str):
        raise MapsSyncError(f"sync evidence {evidence_id} has invalid frozen subject anchors")
    _subject(conn, entity_id, "business_entity")
    _subject(conn, location_id, "location")
    owner = mb._fetch_one(
        conn,
        "SELECT business_entity_id FROM business_locations WHERE id=?",
        (location_id,),
    )
    if owner is None or owner["business_entity_id"] != entity_id:
        raise MapsSyncError(f"sync evidence {evidence_id} frozen subject ownership is inconsistent")
    _verify_sync_identifier_rows(
        conn,
        evidence_id=evidence_id,
        location_id=location_id,
        identifiers=sync_identifiers,
    )

    observations = _snapshot_observations(raw, entity_id=entity_id, location_id=location_id)
    observation_ids = [
        mb._opaque_id("obs", "maps-sync", evidence_id, observation["predicate"])
        for observation in observations
    ]
    observed_at = str(business["last_seen_at"])
    _verify_observations(
        conn,
        evidence_id=evidence_id,
        observations=observations,
        observation_ids=observation_ids,
        observed_at=observed_at,
        extraction_method="direct_structured",
        extractor_name=SYNC_COLLECTOR_NAME,
        extractor_version=SYNC_VERSION,
    )
    manifest = metadata.get("reconciliation_manifest")
    if not isinstance(manifest, list) or len(manifest) != len(observations):
        raise MapsSyncError(f"sync evidence {evidence_id} has invalid reconciliation manifest")
    for observation, observation_id, entry in zip(observations, observation_ids, manifest):
        if not isinstance(entry, dict) or set(entry) != {
            "predicate",
            "fact_slot",
            "observation_id",
            "outcome",
            "fact_id",
            "replaced_fact_id",
        }:
            raise MapsSyncError(f"sync evidence {evidence_id} has malformed reconciliation manifest")
        if (
            entry["predicate"] != observation["predicate"]
            or entry["fact_slot"] != observation["fact_slot"]
            or entry["observation_id"] != observation_id
        ):
            raise MapsSyncError(
                f"sync evidence {evidence_id} reconciliation manifest does not match its observations"
            )
        outcome = entry["outcome"]
        if outcome in {"created_fact", "replaced_maps_fact"}:
            fact_id = _sync_fact_id(observation_id)
            if entry["fact_id"] != fact_id:
                raise MapsSyncError(f"sync evidence {evidence_id} has invalid deterministic fact identity")
            if outcome == "created_fact" and entry["replaced_fact_id"] is not None:
                raise MapsSyncError(f"sync evidence {evidence_id} has invalid created-fact manifest")
            if outcome == "replaced_maps_fact" and not isinstance(entry["replaced_fact_id"], str):
                raise MapsSyncError(f"sync evidence {evidence_id} has invalid replacement manifest")
            _verify_fact_core(
                conn,
                fact_id=fact_id,
                observation=observation,
                observed_at=observed_at,
                reconciliation_version=SYNC_RECONCILIATION_VERSION,
                observation_id=observation_id,
                evidence_id=evidence_id,
            )
        elif outcome == "supported_existing_fact":
            fact_id = entry["fact_id"]
            if (
                not isinstance(fact_id, str)
                or entry["replaced_fact_id"] is not None
                or conn.execute("SELECT 1 FROM facts WHERE id=?", (fact_id,)).fetchone() is None
                or not _support_exists(conn, fact_id, observation_id)
            ):
                raise MapsSyncError(
                    f"sync evidence {evidence_id} support provenance is incomplete or inconsistent "
                    f"for predicate {observation['predicate']}"
                )
        elif outcome == "deferred":
            if entry["fact_id"] is not None or entry["replaced_fact_id"] is not None:
                raise MapsSyncError(f"sync evidence {evidence_id} has invalid deferred manifest")
        else:
            raise MapsSyncError(
                f"sync evidence {evidence_id} has unsupported reconciliation outcome {outcome!r}"
            )


def _existing_snapshot(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    session_kind: str,
    business: dict[str, Any],
    run: dict[str, Any],
    raw: dict[str, Any],
    content_hash: str,
) -> bool:
    business_id = int(business["id"])
    if session_kind == "phase3":
        evidence_id = mb._evidence_id(business_id, content_hash)
        evidence = _evidence_row(conn, evidence_id)
        if evidence is None:
            raise MapsSyncError(
                f"Phase-3 acquisition session {session_id!r} is missing expected evidence "
                f"for Maps business {business_id}"
            )
        _verify_phase3_snapshot(
            conn,
            evidence=evidence,
            session_id=session_id,
            business=business,
            run=run,
            raw=raw,
            content_hash=content_hash,
        )
        return True
    if session_kind != "sync":
        raise MapsSyncError(f"unsupported Maps acquisition-session kind {session_kind!r}")
    evidence_id = _sync_evidence_id(business_id, str(run["id"]), content_hash)
    evidence = _evidence_row(conn, evidence_id)
    if evidence is None:
        return False
    _verify_sync_snapshot(
        conn,
        evidence=evidence,
        session_id=session_id,
        business=business,
        run=run,
        raw=raw,
        content_hash=content_hash,
    )
    return True


def _register_snapshot(
    conn: sqlite3.Connection,
    *,
    business: dict[str, Any],
    run: dict[str, Any],
    raw: dict[str, Any],
    location_id: str,
    session_id: str,
    session_kind: str,
    session_created: bool,
    sync_at: str,
) -> tuple[int, int, int, int, int, int]:
    business_id = int(business["id"])
    raw_json = str(business["raw_json"])
    content_hash = mb._sha256_text(raw_json)
    if _existing_snapshot(
        conn,
        session_id=session_id,
        session_kind=session_kind,
        business=business,
        run=run,
        raw=raw,
        content_hash=content_hash,
    ):
        return 0, 0, 0, 0, 0, 0

    entity_id = _location_owner(conn, location_id)
    observations = _snapshot_observations(raw, entity_id=entity_id, location_id=location_id)
    evidence_id = _sync_evidence_id(business_id, str(run["id"]), content_hash)
    observation_ids = [
        mb._opaque_id("obs", "maps-sync", evidence_id, observation["predicate"])
        for observation in observations
    ]
    observed_at = str(business["last_seen_at"])
    plans = [
        _plan_reconciliation(
            conn,
            observation_id=observation_id,
            observation=observation,
            observed_at=observed_at,
        )
        for observation, observation_id in zip(observations, observation_ids)
    ]
    metadata_json = mb._canonical_json(
        {
            "import_kind": "maps_sync_snapshot",
            "legacy_business_id": business_id,
            "legacy_canonical_key": business["canonical_key"],
            "legacy_run_id": str(run["id"]),
            "sync_external_identifiers": _identifier_snapshot(business),
            "sync_entity_id": entity_id,
            "sync_location_id": location_id,
            "reconciliation_manifest": plans,
            "raw_json": raw_json,
        }
    )
    conn.execute(
        "INSERT INTO evidence_items("
        "id,acquisition_session_id,source_id,source_locator,source_role,status,retrieved_at,"
        "published_at,language,media_type,content_sha256,artifact_ref,metadata_json,created_at"
        ") VALUES (?,?,?,?,'platform','usable',?,NULL,NULL,'application/json',?,?,?,?)",
        (
            evidence_id,
            session_id,
            mb.GOOGLE_MAPS_SOURCE_ID,
            mb._source_locator(raw),
            business["last_seen_at"],
            content_hash,
            run.get("raw_path"),
            metadata_json,
            sync_at,
        ),
    )
    facts_created = 0
    supports_created = 0
    deferred = 0
    for observation, observation_id, plan in zip(observations, observation_ids, plans):
        conn.execute(
            "INSERT INTO observations("
            "id,subject_id,predicate,evidence_id,value_json,normalized_value_json,value_hash,"
            "observation_kind,observed_at,extracted_at,extraction_method,extractor_name,"
            "extractor_version,confidence,created_at"
            ") VALUES (?,?,?,?,?,?,?,'structured_value',?,?,'direct_structured',?,?,NULL,?)",
            (
                observation_id,
                observation["subject_id"],
                observation["predicate"],
                evidence_id,
                observation["value_json"],
                observation["value_json"],
                observation["value_hash"],
                observed_at,
                sync_at,
                SYNC_COLLECTOR_NAME,
                SYNC_VERSION,
                sync_at,
            ),
        )
        created, supports, postponed = _execute_reconciliation(
            conn,
            plan=plan,
            observation=observation,
            observed_at=observed_at,
            created_at=sync_at,
        )
        facts_created += created
        supports_created += supports
        deferred += postponed
    _refresh_session_counts(conn, session_id)
    return (
        int(session_created),
        1,
        len(observations),
        facts_created,
        supports_created,
        deferred,
    )


def sync_maps_business_understanding(conn: sqlite3.Connection) -> MapsSyncStats:
    """Explicitly synchronize current Maps canonical state into Understanding.

    The entire operation is one transaction. Strong Maps identifiers may redirect
    duplicate Locations after acquisition-layer convergence; weak similarity never
    merges Business Entities. Existing snapshot provenance is verified before a
    rerun is accepted as a no-op.
    """
    if conn.in_transaction:
        raise MapsSyncError(
            "Maps understanding synchronization requires a connection with no active transaction"
        )
    conn.execute("PRAGMA foreign_keys=ON")
    if conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise MapsSyncError(
            "SQLite foreign-key enforcement must be enabled for Maps synchronization"
        )

    sync_at = mb._utc_now()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            verify_business_understanding_vocabulary(conn)
        except VocabularySeedError as exc:
            raise MapsSyncError(str(exc)) from exc
        _ensure_source(conn, sync_at)

        businesses = mb._fetch_all(conn, "SELECT * FROM businesses ORDER BY id")
        entities_created = locations_created = links_created = links_repointed = 0
        locations_merged = identifiers_created = identifiers_refreshed = 0
        sessions_created = evidence_created = observations_created = 0
        facts_created = supports_created = deferred = 0

        for business in businesses:
            business_id = int(business["id"])
            run = _latest_run(conn, business)
            raw = _parse_raw(business)
            identifiers = _identifier_snapshot(business)
            session_id, session_kind, session_created = _ensure_session(conn, run)
            content_hash = mb._sha256_text(str(business["raw_json"]))
            _existing_snapshot(
                conn,
                session_id=session_id,
                session_kind=session_kind,
                business=business,
                run=run,
                raw=raw,
                content_hash=content_hash,
            )

            link = mb._fetch_one(
                conn,
                "SELECT location_id FROM maps_business_location_links WHERE business_id=?",
                (business_id,),
            )
            target_location: str | None = None
            if link is not None:
                linked_location = str(link["location_id"])
                target_location = _resolve_subject(conn, linked_location, "location")
                if target_location != linked_location and _repoint_link(
                    conn,
                    business_id=business_id,
                    from_location_id=linked_location,
                    to_location_id=target_location,
                ):
                    links_repointed += 1

            candidates: set[str] = set()
            for identifier in identifiers:
                row = _identifier_row(conn, identifier["namespace"], identifier["value"])
                if row is None:
                    continue
                if row["id"] != mb._external_identifier_id(
                    identifier["namespace"], identifier["value"]
                ):
                    raise MapsSyncError(
                        f"current Maps identifier {identifier['namespace']}={identifier['value']!r} "
                        "has non-deterministic Understanding identity"
                    )
                if row["status"] != "active":
                    raise MapsSyncError(
                        f"current Maps identifier {identifier['namespace']}={identifier['value']!r} "
                        f"is {row['status']!r} in the Understanding layer"
                    )
                candidates.add(_resolve_subject(conn, str(row["subject_id"]), "location"))

            if target_location is None:
                if candidates:
                    target_location = _select_location_target(conn, candidates)
                    linked_other = _linked_business(conn, target_location)
                    if linked_other is not None and linked_other != business_id:
                        raise MapsSyncError(
                            f"current Maps business {business_id} resolves to location "
                            f"{target_location!r} already linked to business {linked_other}"
                        )
                    for candidate in sorted(candidates):
                        if candidate != target_location:
                            locations_merged += int(
                                _merge_location(
                                    conn,
                                    source_location_id=candidate,
                                    target_location_id=target_location,
                                    current_business_id=business_id,
                                    merged_at=sync_at,
                                )
                            )
                    _link_business(
                        conn,
                        business_id=business_id,
                        location_id=target_location,
                        linked_at=sync_at,
                    )
                    links_created += 1
                else:
                    _entity, target_location = _create_anchor(conn, business, sync_at)
                    entities_created += 1
                    locations_created += 1
                    _link_business(
                        conn,
                        business_id=business_id,
                        location_id=target_location,
                        linked_at=sync_at,
                    )
                    links_created += 1
            else:
                for candidate in sorted(candidates):
                    if candidate != target_location:
                        locations_merged += int(
                            _merge_location(
                                conn,
                                source_location_id=candidate,
                                target_location_id=target_location,
                                current_business_id=business_id,
                                merged_at=sync_at,
                            )
                        )

            assert target_location is not None
            for identifier in identifiers:
                created, refreshed, merged = _ensure_identifier(
                    conn,
                    business=business,
                    raw=raw,
                    target_location_id=target_location,
                    namespace=identifier["namespace"],
                    value=identifier["value"],
                    sync_at=sync_at,
                )
                identifiers_created += created
                identifiers_refreshed += refreshed
                locations_merged += merged

            (
                new_sessions,
                new_evidence,
                new_observations,
                new_facts,
                new_supports,
                new_deferred,
            ) = _register_snapshot(
                conn,
                business=business,
                run=run,
                raw=raw,
                location_id=target_location,
                session_id=session_id,
                session_kind=session_kind,
                session_created=session_created,
                sync_at=sync_at,
            )
            sessions_created += new_sessions
            evidence_created += new_evidence
            observations_created += new_observations
            facts_created += new_facts
            supports_created += new_supports
            deferred += new_deferred

        violations = list(conn.execute("PRAGMA foreign_key_check"))
        if violations:
            raise MapsSyncError(
                f"foreign-key violations detected after Maps synchronization: {violations!r}"
            )
        conn.commit()
        mutations = sum(
            (
                entities_created,
                locations_created,
                links_created,
                links_repointed,
                locations_merged,
                identifiers_created,
                identifiers_refreshed,
                sessions_created,
                evidence_created,
                observations_created,
                facts_created,
                supports_created,
            )
        )
        return MapsSyncStats(
            business_count=len(businesses),
            entities_created=entities_created,
            locations_created=locations_created,
            links_created=links_created,
            links_repointed=links_repointed,
            locations_merged=locations_merged,
            external_identifiers_created=identifiers_created,
            external_identifiers_refreshed=identifiers_refreshed,
            acquisition_sessions_created=sessions_created,
            evidence_items_created=evidence_created,
            observations_created=observations_created,
            facts_created=facts_created,
            fact_support_links_created=supports_created,
            fact_updates_deferred=deferred,
            already_synchronized=mutations == 0 and deferred == 0,
        )
    except BaseException:
        conn.rollback()
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sara-maps-sync",
        description=(
            "Explicitly synchronize canonical Google Maps businesses into "
            "Sara Business Understanding."
        ),
    )
    parser.add_argument("--db", default="data/sara.db", help="SQLite database path")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    conn: sqlite3.Connection | None = None
    try:
        conn = connect_existing(Path(args.db))
        apply_migrations(conn)
        seed_business_understanding_vocabulary(conn)
        stats = sync_maps_business_understanding(conn)
        print(json.dumps(asdict(stats), sort_keys=True))
        return 0
    except (
        FileNotFoundError,
        MigrationError,
        VocabularySeedError,
        MapsSyncError,
        sqlite3.Error,
        ValueError,
    ) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    finally:
        if conn is not None:
            conn.close()
