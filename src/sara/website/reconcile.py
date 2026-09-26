from __future__ import annotations

import json
import sqlite3
from typing import Any

from ..maps_backfill import _fetch_one
from .model import OFFICIAL_WEB_SOURCE_ID, RECONCILIATION_VERSION, WebsiteAcquisitionError, opaque_id
from .parser import normalize_http_url


def _parse_json(value: object) -> Any:
    if not isinstance(value, str):
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


def values_equivalent(predicate: str, left_json: object, right_json: object) -> bool:
    if left_json == right_json:
        return True
    if predicate != "business.website.official":
        return False
    left = _parse_json(left_json)
    right = _parse_json(right_json)
    if not isinstance(left, str) or not isinstance(right, str):
        return False
    return normalize_http_url(left) == normalize_http_url(right)


def _current_fact(
    conn: sqlite3.Connection,
    *,
    entity_id: str,
    predicate: str,
    fact_slot: str,
) -> dict[str, Any] | None:
    return _fetch_one(
        conn,
        "SELECT id,value_json,normalized_value_json,value_hash,status,valid_from,"
        "last_verified_at,reconciliation_version FROM facts "
        "WHERE subject_id=? AND predicate=? AND fact_slot=? AND valid_to IS NULL",
        (entity_id, predicate, fact_slot),
    )


def _usable_supports(conn: sqlite3.Connection, fact_id: str) -> list[dict[str, str]]:
    cursor = conn.execute(
        "SELECT o.id,e.source_id,o.value_json FROM fact_observation_support fos "
        "JOIN observations o ON o.id=fos.observation_id "
        "JOIN evidence_items e ON e.id=o.evidence_id "
        "WHERE fos.fact_id=? AND fos.support_role='supports' AND e.status='usable' "
        "ORDER BY o.id",
        (fact_id,),
    )
    return [
        {
            "observation_id": str(row[0]),
            "source_id": str(row[1]),
            "value_json": str(row[2]),
        }
        for row in cursor.fetchall()
    ]


def _insert_fact(
    conn: sqlite3.Connection,
    *,
    fact_id: str,
    entity_id: str,
    predicate: str,
    fact_slot: str,
    value_json: str | None,
    value_hash: str | None,
    status: str,
    valid_from: str,
    reconciled_at: str,
) -> None:
    conn.execute(
        "INSERT INTO facts("
        "id,subject_id,predicate,fact_slot,value_json,normalized_value_json,value_hash,status,"
        "valid_from,valid_to,last_verified_at,reconciled_at,reconciliation_version,created_at"
        ") VALUES (?,?,?,?,?,?,?,?,?,NULL,?,?,?,?)",
        (
            fact_id,
            entity_id,
            predicate,
            fact_slot,
            value_json,
            value_json,
            value_hash,
            status,
            valid_from,
            valid_from,
            reconciled_at,
            RECONCILIATION_VERSION,
            reconciled_at,
        ),
    )


def _close_fact(conn: sqlite3.Connection, fact_id: str, valid_to: str) -> None:
    cursor = conn.execute(
        "UPDATE facts SET valid_to=? WHERE id=? AND valid_to IS NULL",
        (valid_to, fact_id),
    )
    if cursor.rowcount != 1:
        raise WebsiteAcquisitionError(
            f"current fact changed during website reconciliation: {fact_id}"
        )


def _link_observation(
    conn: sqlite3.Connection,
    *,
    fact_id: str,
    observation_id: str,
    role: str,
) -> int:
    conn.execute(
        "INSERT OR IGNORE INTO fact_observation_support(fact_id,observation_id,support_role) "
        "VALUES (?,?,?)",
        (fact_id, observation_id, role),
    )
    return int(conn.execute("SELECT changes()").fetchone()[0] == 1)


def reconcile_observation_group(
    conn: sqlite3.Connection,
    *,
    entity_id: str,
    predicate: str,
    fact_slot: str,
    observations: list[dict[str, Any]],
    observed_at: str,
    reconciled_at: str,
) -> tuple[int, int, int]:
    """Reconcile one official-site value without rewriting history.

    Returns (facts_created, facts_replaced, support_links_created). Late-arriving
    evidence older than the current fact is retained as an Observation but is not
    allowed to corrupt the current fact's immutable status/provenance semantics.
    """
    if not observations:
        return 0, 0, 0
    value_json = str(observations[0]["value_json"])
    value_hash = str(observations[0]["value_hash"])
    if any(
        not values_equivalent(predicate, item["value_json"], value_json)
        for item in observations
    ):
        raise WebsiteAcquisitionError(
            f"website observation group disagrees internally for {predicate}"
        )

    definition = _fetch_one(
        conn,
        "SELECT cardinality,reconciliation_policy FROM predicate_definitions "
        "WHERE name=? AND active=1",
        (predicate,),
    )
    if definition is None:
        raise WebsiteAcquisitionError(
            f"website extractor emitted uncontrolled predicate {predicate!r}"
        )
    if definition["cardinality"] == "single" and fact_slot != "__single__":
        raise WebsiteAcquisitionError(
            f"single-valued website predicate {predicate!r} has invalid slot"
        )
    if definition["cardinality"] == "multi" and fact_slot != value_hash:
        raise WebsiteAcquisitionError(
            f"multi-valued website predicate {predicate!r} has invalid slot"
        )
    if definition["reconciliation_policy"] not in {
        "prefer_authoritative_recent",
        "latest_observed",
        "supported_set",
    }:
        raise WebsiteAcquisitionError(
            "website collector does not implement reconciliation policy "
            f"{definition['reconciliation_policy']!r}"
        )

    current = _current_fact(
        conn, entity_id=entity_id, predicate=predicate, fact_slot=fact_slot
    )
    new_ids = [str(item["id"]) for item in observations]
    if current is None:
        fact_id = opaque_id(
            "fact", entity_id, predicate, fact_slot, value_hash, observed_at
        )
        _insert_fact(
            conn,
            fact_id=fact_id,
            entity_id=entity_id,
            predicate=predicate,
            fact_slot=fact_slot,
            value_json=value_json,
            value_hash=value_hash,
            status="single_source",
            valid_from=observed_at,
            reconciled_at=reconciled_at,
        )
        links = sum(
            _link_observation(
                conn,
                fact_id=fact_id,
                observation_id=observation_id,
                role="supports",
            )
            for observation_id in new_ids
        )
        return 1, 0, links

    previous = _usable_supports(conn, str(current["id"]))
    previous_sources = {item["source_id"] for item in previous}
    same_value = values_equivalent(predicate, current["value_json"], value_json)
    last_verified = str(current["last_verified_at"] or current["valid_from"])

    if str(observed_at) < str(current["valid_from"]):
        if same_value and (
            current["status"] == "confirmed"
            or previous_sources == {OFFICIAL_WEB_SOURCE_ID}
        ):
            links = sum(
                _link_observation(
                    conn,
                    fact_id=str(current["id"]),
                    observation_id=observation_id,
                    role="supports",
                )
                for observation_id in new_ids
            )
            return 0, 0, links
        return 0, 0, 0

    if same_value and str(observed_at) <= last_verified:
        if current["status"] == "confirmed" or previous_sources == {OFFICIAL_WEB_SOURCE_ID}:
            links = sum(
                _link_observation(
                    conn,
                    fact_id=str(current["id"]),
                    observation_id=observation_id,
                    role="supports",
                )
                for observation_id in new_ids
            )
            return 0, 0, links
        return 0, 0, 0

    _close_fact(conn, str(current["id"]), observed_at)
    fact_id = opaque_id(
        "fact", entity_id, predicate, fact_slot, value_hash, observed_at
    )
    same_value_old = [
        item
        for item in previous
        if values_equivalent(predicate, item["value_json"], value_json)
    ]
    support_sources = {item["source_id"] for item in same_value_old}
    support_sources.add(OFFICIAL_WEB_SOURCE_ID)
    status = (
        "confirmed"
        if (same_value and current["status"] == "confirmed")
        or len(support_sources) >= 2
        else "single_source"
    )
    _insert_fact(
        conn,
        fact_id=fact_id,
        entity_id=entity_id,
        predicate=predicate,
        fact_slot=fact_slot,
        value_json=value_json,
        value_hash=value_hash,
        status=status,
        valid_from=observed_at,
        reconciled_at=reconciled_at,
    )
    links = 0
    for item in same_value_old:
        links += _link_observation(
            conn,
            fact_id=fact_id,
            observation_id=item["observation_id"],
            role="supports",
        )
    for observation_id in new_ids:
        links += _link_observation(
            conn,
            fact_id=fact_id,
            observation_id=observation_id,
            role="supports",
        )
    if not same_value:
        for item in previous:
            if not values_equivalent(predicate, item["value_json"], value_json):
                links += _link_observation(
                    conn,
                    fact_id=fact_id,
                    observation_id=item["observation_id"],
                    role="contradicts",
                )
    return 1, 1, links


def reconcile_not_observed(
    conn: sqlite3.Connection,
    *,
    entity_id: str,
    predicate: str,
    session_id: str,
    observed_at: str,
) -> tuple[int, int]:
    current = _current_fact(
        conn, entity_id=entity_id, predicate=predicate, fact_slot="__single__"
    )
    if current is not None:
        if current["status"] in {"conflicted", "not_applicable"}:
            return 0, 0
        current_value = _parse_json(current["value_json"])
        if current["value_json"] is not None and current_value is None:
            raise WebsiteAcquisitionError(
                f"current {predicate} fact contains malformed JSON"
            )
        if current_value is not None:
            return 0, 0
        if str(observed_at) < str(current["valid_from"]):
            return 0, 0
        _close_fact(conn, str(current["id"]), observed_at)

    fact_id = opaque_id(
        "fact", entity_id, predicate, "__single__", "not_observed", observed_at
    )
    _insert_fact(
        conn,
        fact_id=fact_id,
        entity_id=entity_id,
        predicate=predicate,
        fact_slot="__single__",
        value_json=None,
        value_hash=None,
        status="not_observed",
        valid_from=observed_at,
        reconciled_at=observed_at,
    )
    conn.execute(
        "INSERT INTO fact_acquisition_support(fact_id,acquisition_session_id,support_role) "
        "VALUES (?,?,'supports_absence')",
        (fact_id, session_id),
    )
    return 1, 1
