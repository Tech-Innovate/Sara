from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

from .maps_source import MapsSourceShapeError, official_website
from .understanding_vocabulary import (
    PREDICATE_SEEDS,
    VocabularySeedError,
    verify_business_understanding_vocabulary,
)


class MapsBackfillError(RuntimeError):
    """The current Maps canonical state cannot be backfilled safely."""


@dataclass(frozen=True)
class MapsBackfillStats:
    business_count: int
    entities_created: int
    locations_created: int
    links_created: int
    external_identifiers_created: int
    acquisition_sessions_created: int
    evidence_items_created: int
    observations_created: int
    facts_created: int
    already_backfilled: bool


GOOGLE_MAPS_SOURCE_ID = "src_google_maps"
BACKFILL_COLLECTOR_NAME = "sara.maps_backfill"
BACKFILL_VERSION = "2"
RECONCILIATION_VERSION = "maps-backfill-v2"
_ID_NAMESPACE = "sara.business-understanding.maps-backfill.v1"
_IDENTIFIER_NAMESPACES = ("place_id", "cid", "data_id")

_FIELD_SPECS: tuple[tuple[str, str, str], ...] = (
    ("title", "business_entity", "business.name.trading"),
    ("category", "business_entity", "business.category.primary"),
    ("address", "location", "location.address"),
    ("latitude", "location", "location.latitude"),
    ("longitude", "location", "location.longitude"),
    ("phone", "location", "location.phone"),
    ("website", "business_entity", "business.website.official"),
    ("review_rating", "location", "reputation.rating"),
    ("review_count", "location", "reputation.review_count"),
    ("status", "location", "location.operating_status"),
)
_PREDICATE_CARDINALITY = {seed.name: seed.cardinality for seed in PREDICATE_SEEDS}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _opaque_id(prefix: str, *parts: object) -> str:
    payload = "\x1f".join((_ID_NAMESPACE, *(str(part) for part in parts)))
    return f"{prefix}_{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:32]}"


def business_entity_id_for_maps_business(business_id: int) -> str:
    return _opaque_id("be", "business", int(business_id))


def location_id_for_maps_business(business_id: int) -> str:
    return _opaque_id("loc", "business", int(business_id))


def _acquisition_id(run_id: str) -> str:
    return _opaque_id("acq", "legacy-run", run_id)


def _evidence_id(business_id: int, content_sha256: str) -> str:
    return _opaque_id("ev", "business", business_id, content_sha256)


def _external_identifier_id(namespace: str, value: str) -> str:
    return _opaque_id("xid", GOOGLE_MAPS_SOURCE_ID, namespace, value)


def _observation_id(evidence_id: str, predicate: str) -> str:
    return _opaque_id("obs", evidence_id, predicate)


def _fact_id(observation_id: str) -> str:
    return _opaque_id("fact", observation_id, RECONCILIATION_VERSION)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _row_dict(cursor: sqlite3.Cursor, row: sqlite3.Row | tuple[Any, ...]) -> dict[str, Any]:
    return {
        str(column[0]): row[index]
        for index, column in enumerate(cursor.description or ())
    }


def _fetch_one(
    conn: sqlite3.Connection, sql: str, params: Iterable[Any] = ()
) -> dict[str, Any] | None:
    cursor = conn.execute(sql, tuple(params))
    row = cursor.fetchone()
    return None if row is None else _row_dict(cursor, row)


def _fetch_all(conn: sqlite3.Connection, sql: str) -> list[dict[str, Any]]:
    cursor = conn.execute(sql)
    return [_row_dict(cursor, row) for row in cursor.fetchall()]


def _text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    value = str(value).strip()
    return value or None


def _float(value: Any) -> float | None:
    try:
        return None if value in (None, "") else float(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int | None:
    try:
        return None if value in (None, "") else int(value)
    except (TypeError, ValueError):
        return None


def _source_values(raw: dict[str, Any]) -> dict[str, Any]:
    longitude = raw.get("longitude")
    if longitude is None:
        longitude = raw.get("longtitude")
    try:
        website = official_website(raw)
    except MapsSourceShapeError as exc:
        raise MapsBackfillError(str(exc)) from exc
    return {
        "title": _text(raw.get("title")),
        "category": _text(raw.get("category")),
        "address": _text(raw.get("address")),
        "latitude": _float(raw.get("latitude")),
        "longitude": _float(longitude),
        "phone": _text(raw.get("phone")),
        "website": website,
        "review_rating": _float(raw.get("review_rating")),
        "review_count": _int(raw.get("review_count")),
        "status": _text(raw.get("status")),
    }


def _source_locator(raw: dict[str, Any]) -> str | None:
    link = _text(raw.get("link"))
    if link:
        return link
    for namespace in _IDENTIFIER_NAMESPACES:
        value = _text(raw.get(namespace))
        if value:
            return f"{namespace}:{value}"
    return None


def _phase3_identifier_snapshot(business: dict[str, Any]) -> list[dict[str, str]]:
    snapshot: list[dict[str, str]] = []
    for namespace in _IDENTIFIER_NAMESPACES:
        value = _text(business.get(namespace))
        if value:
            snapshot.append({"namespace": namespace, "value": value})
    return snapshot


def _parse_phase3_identifier_snapshot(
    evidence_id: object, metadata: dict[str, Any]
) -> list[dict[str, str]]:
    raw_snapshot = metadata.get("phase3_external_identifiers")
    if not isinstance(raw_snapshot, list):
        raise MapsBackfillError(
            f"backfill evidence {evidence_id} has no valid Phase-3 identifier snapshot"
        )

    snapshot: list[dict[str, str]] = []
    seen_namespaces: set[str] = set()
    for item in raw_snapshot:
        if not isinstance(item, dict) or set(item) != {"namespace", "value"}:
            raise MapsBackfillError(
                f"backfill evidence {evidence_id} has malformed Phase-3 identifier snapshot"
            )
        namespace = item.get("namespace")
        value = item.get("value")
        if (
            namespace not in _IDENTIFIER_NAMESPACES
            or namespace in seen_namespaces
            or not isinstance(value, str)
            or not value
            or value != value.strip()
        ):
            raise MapsBackfillError(
                f"backfill evidence {evidence_id} has malformed Phase-3 identifier snapshot"
            )
        seen_namespaces.add(namespace)
        snapshot.append({"namespace": namespace, "value": value})

    expected_order = [
        namespace for namespace in _IDENTIFIER_NAMESPACES if namespace in seen_namespaces
    ]
    if [item["namespace"] for item in snapshot] != expected_order:
        raise MapsBackfillError(
            f"backfill evidence {evidence_id} has non-canonical Phase-3 identifier snapshot"
        )
    return snapshot


def _parse_raw(business: dict[str, Any]) -> dict[str, Any]:
    raw_json = business.get("raw_json")
    if not isinstance(raw_json, str):
        raise MapsBackfillError(
            f"business {business['id']} has non-text raw_json; provenance import is unsafe"
        )
    try:
        raw = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise MapsBackfillError(
            f"business {business['id']} raw_json is malformed: {exc}"
        ) from exc
    if not isinstance(raw, dict):
        raise MapsBackfillError(f"business {business['id']} raw_json is not a JSON object")
    return raw


def _latest_run(conn: sqlite3.Connection, business: dict[str, Any]) -> dict[str, Any]:
    run_id = business.get("last_run_id")
    if not isinstance(run_id, str) or not run_id:
        raise MapsBackfillError(
            f"business {business['id']} has no last_run_id; latest Maps provenance is unresolved"
        )
    run = _fetch_one(
        conn,
        "SELECT id,status,started_at,finished_at,error,raw_path FROM runs WHERE id=?",
        (run_id,),
    )
    if run is None:
        raise MapsBackfillError(
            f"business {business['id']} references missing last run {run_id!r}"
        )
    if (
        run["status"] != "complete"
        or run["finished_at"] in (None, "")
        or run["error"] not in (None, "")
    ):
        raise MapsBackfillError(
            f"business {business['id']} latest run {run_id!r} is not a clean completed run"
        )
    if str(business.get("last_seen_at")) != str(run["started_at"]):
        raise MapsBackfillError(
            f"business {business['id']} last_seen_at does not match latest run start"
        )
    if conn.execute(
        "SELECT 1 FROM run_businesses WHERE run_id=? AND business_id=?",
        (run_id, business["id"]),
    ).fetchone() is None:
        raise MapsBackfillError(
            f"business {business['id']} is not a member of its last run {run_id!r}"
        )
    return run


def _observations(
    business: dict[str, Any], raw: dict[str, Any]
) -> list[dict[str, Any]]:
    business_id = int(business["id"])
    entity_id = business_entity_id_for_maps_business(business_id)
    location_id = location_id_for_maps_business(business_id)
    evidence_id = _evidence_id(business_id, _sha256_text(str(business["raw_json"])))
    values = _source_values(raw)
    result: list[dict[str, Any]] = []
    for field, subject_kind, predicate in _FIELD_SPECS:
        value = values[field]
        if value is None:
            continue
        value_json = _canonical_json(value)
        value_hash = _sha256_text(value_json)
        cardinality = _PREDICATE_CARDINALITY.get(predicate)
        if cardinality not in {"single", "multi"}:
            raise MapsBackfillError(f"unsupported predicate cardinality for {predicate!r}")
        result.append(
            {
                "subject_id": entity_id if subject_kind == "business_entity" else location_id,
                "predicate": predicate,
                "evidence_id": evidence_id,
                "value_json": value_json,
                "value_hash": value_hash,
                "fact_slot": "__single__" if cardinality == "single" else value_hash,
            }
        )
    return result


def _prepare(conn: sqlite3.Connection, businesses: list[dict[str, Any]]):
    prepared = []
    for business in businesses:
        run = _latest_run(conn, business)
        raw = _parse_raw(business)
        prepared.append((business, run, raw, _observations(business, raw)))
    return prepared


def _session_counts(prepared):
    counts: dict[str, tuple[dict[str, Any], int, int]] = {}
    for _business, run, _raw, observations in prepared:
        run_id = str(run["id"])
        previous = counts.get(run_id)
        counts[run_id] = (
            run,
            1 + (0 if previous is None else previous[1]),
            len(observations) + (0 if previous is None else previous[2]),
        )
    return counts


def _source_registry_row(conn: sqlite3.Connection) -> dict[str, Any] | None:
    return _fetch_one(
        conn,
        "SELECT source_type,name,base_url,active FROM sources WHERE id=?",
        (GOOGLE_MAPS_SOURCE_ID,),
    )


def _expected_source_registry_row() -> dict[str, Any]:
    return {
        "source_type": "google_maps",
        "name": "Google Maps",
        "base_url": None,
        "active": 1,
    }


def _ensure_source(conn: sqlite3.Connection, created_at: str) -> None:
    row = _source_registry_row(conn)
    expected = _expected_source_registry_row()
    if row is None:
        conn.execute(
            "INSERT INTO sources(id,source_type,name,base_url,created_at,active) "
            "VALUES (?,'google_maps','Google Maps',NULL,?,1)",
            (GOOGLE_MAPS_SOURCE_ID, created_at),
        )
    elif row != expected:
        raise MapsBackfillError(
            f"Google Maps source registry drift: database={row!r}, expected={expected!r}"
        )


def _insert_sessions(conn: sqlite3.Connection, prepared) -> int:
    created = 0
    for run_id, (run, evidence_count, observation_count) in sorted(_session_counts(prepared).items()):
        session_id = _acquisition_id(run_id)
        if conn.execute(
            "SELECT 1 FROM acquisition_sessions WHERE id=? OR legacy_run_id=?",
            (session_id, run_id),
        ).fetchone() is not None:
            raise MapsBackfillError(
                f"legacy run {run_id!r} already has an acquisition session before initial backfill"
            )
        config_json = _canonical_json(
            {"import_mode": "latest_canonical_maps_snapshot", "legacy_run_id": run_id}
        )
        conn.execute(
            "INSERT INTO acquisition_sessions("
            "id,target_subject_id,source_id,collector_name,collector_version,config_json,"
            "config_hash,status,started_at,finished_at,error,legacy_run_id,"
            "evidence_count,observation_count"
            ") VALUES (?,NULL,?,?,?,?,?,'complete',?,?,NULL,?,?,?)",
            (
                session_id,
                GOOGLE_MAPS_SOURCE_ID,
                BACKFILL_COLLECTOR_NAME,
                BACKFILL_VERSION,
                config_json,
                _sha256_text(config_json),
                run["started_at"],
                run["finished_at"],
                run_id,
                evidence_count,
                observation_count,
            ),
        )
        created += 1
    return created


def _insert_anchor(
    conn: sqlite3.Connection, business: dict[str, Any], created_at: str
) -> tuple[str, str]:
    business_id = int(business["id"])
    entity_id = business_entity_id_for_maps_business(business_id)
    location_id = location_id_for_maps_business(business_id)
    for subject_id in (entity_id, location_id):
        if conn.execute(
            "SELECT 1 FROM knowledge_subjects WHERE id=?", (subject_id,)
        ).fetchone() is not None:
            raise MapsBackfillError(
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
    conn.execute(
        "INSERT INTO maps_business_location_links(business_id,location_id,linked_at) "
        "VALUES (?,?,?)",
        (business_id, location_id, created_at),
    )
    return entity_id, location_id


def _insert_identifiers(
    conn: sqlite3.Connection,
    business: dict[str, Any],
    location_id: str,
    created_at: str,
) -> int:
    count = 0
    for identifier in _phase3_identifier_snapshot(business):
        namespace = identifier["namespace"]
        value = identifier["value"]
        identifier_id = _external_identifier_id(namespace, value)
        if conn.execute(
            "SELECT 1 FROM external_identifiers "
            "WHERE id=? OR (source_id=? AND namespace=? AND value=?)",
            (identifier_id, GOOGLE_MAPS_SOURCE_ID, namespace, value),
        ).fetchone() is not None:
            raise MapsBackfillError(
                f"Maps identifier {namespace}={value!r} already exists before initial backfill"
            )
        conn.execute(
            "INSERT INTO external_identifiers("
            "id,subject_id,source_id,namespace,value,status,first_observed_at,last_observed_at,created_at"
            ") VALUES (?,?,?,?,?,'active',?,?,?)",
            (
                identifier_id,
                location_id,
                GOOGLE_MAPS_SOURCE_ID,
                namespace,
                value,
                created_at,
                created_at,
                created_at,
            ),
        )
        count += 1
    return count


def _insert_provenance(
    conn: sqlite3.Connection,
    business: dict[str, Any],
    run: dict[str, Any],
    raw: dict[str, Any],
    observations: list[dict[str, Any]],
    created_at: str,
) -> tuple[int, int, int]:
    business_id = int(business["id"])
    raw_json = str(business["raw_json"])
    content_hash = _sha256_text(raw_json)
    evidence_id = _evidence_id(business_id, content_hash)
    metadata_json = _canonical_json(
        {
            "import_kind": "legacy_maps_business_snapshot",
            "legacy_business_id": business_id,
            "legacy_canonical_key": business["canonical_key"],
            "legacy_run_id": run["id"],
            "phase3_external_identifiers": _phase3_identifier_snapshot(business),
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
            _acquisition_id(str(run["id"])),
            GOOGLE_MAPS_SOURCE_ID,
            _source_locator(raw),
            business["last_seen_at"],
            content_hash,
            run.get("raw_path"),
            metadata_json,
            created_at,
        ),
    )
    for observation in observations:
        observation_id = _observation_id(evidence_id, observation["predicate"])
        conn.execute(
            "INSERT INTO observations("
            "id,subject_id,predicate,evidence_id,value_json,normalized_value_json,value_hash,"
            "observation_kind,observed_at,extracted_at,extraction_method,extractor_name,"
            "extractor_version,confidence,created_at"
            ") VALUES (?,?,?,?,?,?,?,'structured_value',?,?,'legacy_import',?,?,NULL,?)",
            (
                observation_id,
                observation["subject_id"],
                observation["predicate"],
                evidence_id,
                observation["value_json"],
                observation["value_json"],
                observation["value_hash"],
                business["last_seen_at"],
                created_at,
                BACKFILL_COLLECTOR_NAME,
                BACKFILL_VERSION,
                created_at,
            ),
        )
        fact_id = _fact_id(observation_id)
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
                business["last_seen_at"],
                business["last_seen_at"],
                created_at,
                RECONCILIATION_VERSION,
                created_at,
            ),
        )
        conn.execute(
            "INSERT INTO fact_observation_support(fact_id,observation_id,support_role) "
            "VALUES (?,?,'supports')",
            (fact_id, observation_id),
        )
    return 1, len(observations), len(observations)


def _backfill_evidence_by_business(
    conn: sqlite3.Connection,
) -> dict[int, dict[str, Any]]:
    cursor = conn.execute(
        "SELECT e.id,e.content_sha256,e.metadata_json,e.retrieved_at,e.created_at,a.legacy_run_id "
        "FROM evidence_items e "
        "JOIN acquisition_sessions a ON a.id=e.acquisition_session_id "
        "WHERE e.source_id=? AND a.source_id=? AND a.collector_name=? "
        "AND a.collector_version=? AND a.status='complete' "
        "AND e.source_role='platform' AND e.status='usable'",
        (
            GOOGLE_MAPS_SOURCE_ID,
            GOOGLE_MAPS_SOURCE_ID,
            BACKFILL_COLLECTOR_NAME,
            BACKFILL_VERSION,
        ),
    )
    result: dict[int, dict[str, Any]] = {}
    for row in cursor.fetchall():
        item = _row_dict(cursor, row)
        try:
            metadata = json.loads(str(item["metadata_json"]))
        except json.JSONDecodeError as exc:
            raise MapsBackfillError(
                f"backfill evidence {item['id']} has malformed metadata_json"
            ) from exc
        if not isinstance(metadata, dict):
            raise MapsBackfillError(
                f"backfill evidence {item['id']} metadata_json is not an object"
            )
        if metadata.get("import_kind") != "legacy_maps_business_snapshot":
            continue
        legacy_business_id = metadata.get("legacy_business_id")
        if not isinstance(legacy_business_id, int):
            raise MapsBackfillError(
                f"backfill evidence {item['id']} has invalid legacy_business_id"
            )
        raw_json = metadata.get("raw_json")
        if not isinstance(raw_json, str) or _sha256_text(raw_json) != item["content_sha256"]:
            raise MapsBackfillError(
                f"backfill evidence {item['id']} content hash does not match retained raw_json"
            )
        expected_evidence_id = _evidence_id(legacy_business_id, str(item["content_sha256"]))
        if str(item["id"]) != expected_evidence_id:
            raise MapsBackfillError(
                f"backfill evidence {item['id']} does not have its deterministic Phase-3 identity"
            )
        if str(metadata.get("legacy_run_id")) != str(item["legacy_run_id"]):
            raise MapsBackfillError(
                f"backfill evidence {item['id']} legacy run provenance is inconsistent"
            )
        if legacy_business_id in result:
            raise MapsBackfillError(
                f"multiple immutable Phase-3 evidence items claim Maps business {legacy_business_id}"
            )
        item["phase3_external_identifiers"] = _parse_phase3_identifier_snapshot(
            item["id"], metadata
        )
        item["raw_json"] = raw_json
        result[legacy_business_id] = item
    return result


def _expected_backfill_observations(
    business_id: int, evidence: dict[str, Any]
) -> list[dict[str, Any]]:
    raw_json = evidence.get("raw_json")
    if not isinstance(raw_json, str):
        raise MapsBackfillError(
            f"backfill evidence {evidence['id']} has no retained raw_json"
        )
    try:
        raw = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise MapsBackfillError(
            f"backfill evidence {evidence['id']} retained raw_json is malformed"
        ) from exc
    if not isinstance(raw, dict):
        raise MapsBackfillError(
            f"backfill evidence {evidence['id']} retained raw_json is not an object"
        )
    return _observations({"id": business_id, "raw_json": raw_json}, raw)


def _verify_complete_coverage(
    conn: sqlite3.Connection, businesses: list[dict[str, Any]]
) -> None:
    """Verify original Phase-3 anchors and immutable provenance, not mutable Maps state."""
    source = _source_registry_row(conn)
    if source is None or source["source_type"] != "google_maps":
        raise MapsBackfillError(
            f"existing Maps backfill source identity/type drift: database={source!r}"
        )

    evidence_by_business = _backfill_evidence_by_business(conn)
    for business in businesses:
        business_id = int(business["id"])
        expected_entity = business_entity_id_for_maps_business(business_id)
        expected_location = location_id_for_maps_business(business_id)
        row = _fetch_one(
            conn,
            "SELECT l.location_id,bl.business_entity_id "
            "FROM maps_business_location_links l "
            "JOIN business_locations bl ON bl.id=l.location_id WHERE l.business_id=?",
            (business_id,),
        )
        if row != {
            "location_id": expected_location,
            "business_entity_id": expected_entity,
        }:
            raise MapsBackfillError(
                f"existing Maps backfill anchor drift for business {business_id}"
            )

        evidence = evidence_by_business.get(business_id)
        if evidence is None:
            raise MapsBackfillError(
                f"existing Maps backfill provenance is incomplete for business {business_id}"
            )

        for identifier in evidence["phase3_external_identifiers"]:
            namespace = identifier["namespace"]
            value = identifier["value"]
            identifier_id = _external_identifier_id(namespace, value)
            identifier_row = _fetch_one(
                conn,
                "SELECT id,subject_id,source_id,namespace,value,first_observed_at,created_at "
                "FROM external_identifiers WHERE id=?",
                (identifier_id,),
            )
            expected_identifier_row = {
                "id": identifier_id,
                "subject_id": expected_location,
                "source_id": GOOGLE_MAPS_SOURCE_ID,
                "namespace": namespace,
                "value": value,
                "first_observed_at": evidence["created_at"],
                "created_at": evidence["created_at"],
            }
            if identifier_row != expected_identifier_row:
                raise MapsBackfillError(
                    f"existing Maps backfill external identifier provenance is incomplete or "
                    f"inconsistent for business {business_id}, namespace {namespace}"
                )

        expected_observations = _expected_backfill_observations(business_id, evidence)
        expected_observation_rows: list[dict[str, Any]] = []
        expected_fact_ids: list[str] = []
        for observation in expected_observations:
            observation_id = _observation_id(str(evidence["id"]), observation["predicate"])
            expected_observation_rows.append(
                {
                    "id": observation_id,
                    "subject_id": observation["subject_id"],
                    "predicate": observation["predicate"],
                    "evidence_id": str(evidence["id"]),
                    "value_json": observation["value_json"],
                    "normalized_value_json": observation["value_json"],
                    "value_hash": observation["value_hash"],
                    "observation_kind": "structured_value",
                    "observed_at": evidence["retrieved_at"],
                    "extraction_method": "legacy_import",
                    "extractor_name": BACKFILL_COLLECTOR_NAME,
                    "extractor_version": BACKFILL_VERSION,
                }
            )
            expected_fact_ids.append(_fact_id(observation_id))
        expected_observation_rows.sort(key=lambda item: str(item["id"]))
        expected_fact_ids.sort()

        cursor = conn.execute(
            "SELECT id,subject_id,predicate,evidence_id,value_json,normalized_value_json,value_hash,"
            "observation_kind,observed_at,extraction_method,extractor_name,extractor_version "
            "FROM observations WHERE evidence_id=? AND extraction_method='legacy_import' "
            "AND extractor_name=? AND extractor_version=? ORDER BY id",
            (evidence["id"], BACKFILL_COLLECTOR_NAME, BACKFILL_VERSION),
        )
        actual_observation_rows = [_row_dict(cursor, row) for row in cursor.fetchall()]
        if actual_observation_rows != expected_observation_rows:
            raise MapsBackfillError(
                f"existing Maps backfill observation provenance is incomplete or inconsistent "
                f"for business {business_id}"
            )

        cursor = conn.execute(
            "SELECT DISTINCT f.id FROM facts f "
            "JOIN fact_observation_support fos ON fos.fact_id=f.id "
            "JOIN observations o ON o.id=fos.observation_id "
            "WHERE o.evidence_id=? AND o.extraction_method='legacy_import' "
            "AND o.extractor_name=? AND o.extractor_version=? "
            "AND f.reconciliation_version=? ORDER BY f.id",
            (
                evidence["id"],
                BACKFILL_COLLECTOR_NAME,
                BACKFILL_VERSION,
                RECONCILIATION_VERSION,
            ),
        )
        actual_fact_ids = [str(row[0]) for row in cursor.fetchall()]
        if actual_fact_ids != expected_fact_ids:
            raise MapsBackfillError(
                f"existing Maps backfill fact provenance is incomplete or inconsistent "
                f"for business {business_id}"
            )

        for observation in expected_observations:
            observation_id = _observation_id(str(evidence["id"]), observation["predicate"])
            fact_id = _fact_id(observation_id)
            fact = _fetch_one(
                conn,
                "SELECT id,subject_id,predicate,fact_slot,value_json,normalized_value_json,"
                "value_hash,status,valid_from,last_verified_at,reconciliation_version "
                "FROM facts WHERE id=?",
                (fact_id,),
            )
            expected_fact = {
                "id": fact_id,
                "subject_id": observation["subject_id"],
                "predicate": observation["predicate"],
                "fact_slot": observation["fact_slot"],
                "value_json": observation["value_json"],
                "normalized_value_json": observation["value_json"],
                "value_hash": observation["value_hash"],
                "status": "single_source",
                "valid_from": evidence["retrieved_at"],
                "last_verified_at": evidence["retrieved_at"],
                "reconciliation_version": RECONCILIATION_VERSION,
            }
            if fact != expected_fact:
                raise MapsBackfillError(
                    f"existing Maps backfill fact provenance is incomplete or inconsistent "
                    f"for business {business_id}, predicate {observation['predicate']}"
                )

            support_cursor = conn.execute(
                "SELECT support_role FROM fact_observation_support "
                "WHERE fact_id=? AND observation_id=?",
                (fact_id, observation_id),
            )
            support_rows = [
                _row_dict(support_cursor, support_row)
                for support_row in support_cursor.fetchall()
            ]
            if support_rows != [{"support_role": "supports"}]:
                raise MapsBackfillError(
                    f"existing Maps backfill support provenance is incomplete or inconsistent "
                    f"for business {business_id}, predicate {observation['predicate']}"
                )


def backfill_maps_business_understanding(conn: sqlite3.Connection) -> MapsBackfillStats:
    """Perform the one-time, conservative Phase-3 Maps backfill atomically.

    On first success every current canonical Maps business receives exactly one
    provisional Business Entity and one Location. A later call is a no-op when
    every current row is already anchored and its immutable Phase-3 provenance
    is still complete. A mixed linked/unlinked state fails closed so this
    operation cannot silently become the Phase-4 synchronizer.
    """
    if conn.in_transaction:
        raise MapsBackfillError(
            "Maps understanding backfill requires a connection with no active transaction"
        )
    conn.execute("PRAGMA foreign_keys=ON")
    if conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise MapsBackfillError(
            "SQLite foreign-key enforcement must be enabled for Maps backfill"
        )

    created_at = _utc_now()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            verify_business_understanding_vocabulary(conn)
        except VocabularySeedError as exc:
            raise MapsBackfillError(str(exc)) from exc

        businesses = _fetch_all(conn, "SELECT * FROM businesses ORDER BY id")
        business_count = len(businesses)
        link_count = int(
            conn.execute("SELECT COUNT(*) FROM maps_business_location_links").fetchone()[0]
        )
        if link_count not in {0, business_count}:
            raise MapsBackfillError(
                "partial Maps understanding backfill state detected; Phase 3 will not "
                "guess whether this is corruption or post-backfill new Maps data"
            )
        if business_count == link_count and business_count:
            _verify_complete_coverage(conn, businesses)
            conn.commit()
            return MapsBackfillStats(business_count, 0, 0, 0, 0, 0, 0, 0, 0, True)
        if not business_count:
            conn.commit()
            return MapsBackfillStats(0, 0, 0, 0, 0, 0, 0, 0, 0, False)

        prepared = _prepare(conn, businesses)
        _ensure_source(conn, created_at)
        session_count = _insert_sessions(conn, prepared)
        identifier_count = evidence_count = observation_count = fact_count = 0
        for business, run, raw, observations in prepared:
            _entity_id, location_id = _insert_anchor(conn, business, created_at)
            identifier_count += _insert_identifiers(
                conn, business, location_id, created_at
            )
            evidence_added, observation_added, fact_added = _insert_provenance(
                conn, business, run, raw, observations, created_at
            )
            evidence_count += evidence_added
            observation_count += observation_added
            fact_count += fact_added

        if int(conn.execute(
            "SELECT COUNT(*) FROM maps_business_location_links"
        ).fetchone()[0]) != business_count:
            raise MapsBackfillError("Maps backfill did not create exactly one link per business")
        provenance_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM facts f "
                "JOIN fact_observation_support fos ON fos.fact_id=f.id AND fos.support_role='supports' "
                "JOIN observations o ON o.id=fos.observation_id "
                "JOIN evidence_items e ON e.id=o.evidence_id "
                "JOIN acquisition_sessions a ON a.id=e.acquisition_session_id "
                "JOIN sources s ON s.id=a.source_id "
                "WHERE f.reconciliation_version=? AND s.id=?",
                (RECONCILIATION_VERSION, GOOGLE_MAPS_SOURCE_ID),
            ).fetchone()[0]
        )
        if provenance_count != fact_count:
            raise MapsBackfillError(
                "not every imported fact has complete observation/evidence/acquisition/source provenance"
            )
        violations = list(conn.execute("PRAGMA foreign_key_check"))
        if violations:
            raise MapsBackfillError(
                f"foreign-key violations detected after Maps backfill: {violations!r}"
            )
        conn.commit()
        return MapsBackfillStats(
            business_count,
            business_count,
            business_count,
            business_count,
            identifier_count,
            session_count,
            evidence_count,
            observation_count,
            fact_count,
            False,
        )
    except BaseException:
        conn.rollback()
        raise