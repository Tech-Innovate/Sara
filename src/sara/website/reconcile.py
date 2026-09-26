from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from ..maps_backfill import _fetch_one
from .model import OFFICIAL_WEB_SOURCE_ID, RECONCILIATION_VERSION, WebsiteAcquisitionError, opaque_id
from .parser import normalize_http_url, same_site


def _parse_json(value: object) -> Any:
    if not isinstance(value, str):
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


def _instant(value: object, *, field: str) -> datetime:
    if not isinstance(value, str):
        raise WebsiteAcquisitionError(f"{field} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise WebsiteAcquisitionError(f"{field} is not a valid ISO-8601 timestamp: {value!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise WebsiteAcquisitionError(f"{field} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


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


def _verified_website_alias_from_new_evidence(
    conn: sqlite3.Connection,
    observations: list[dict[str, Any]],
    left_json: object,
    right_json: object,
) -> bool:
    """Return true only for an alias proven by this acquisition's retained home evidence.

    This is intentionally directional: the prior fact value must equal the acquisition
    start URL, and the newly reconciled value must equal the verified home capture's
    final URL. Merely sharing a host, differing by ``www`` or switching schemes is not
    sufficient.
    """
    left = _parse_json(left_json)
    right = _parse_json(right_json)
    if not isinstance(left, str) or not isinstance(right, str):
        return False
    left_url = normalize_http_url(left)
    right_url = normalize_http_url(right)
    if left_url is None or right_url is None:
        return False

    for item in observations:
        evidence_id = item.get("evidence_id")
        if not isinstance(evidence_id, str) or not evidence_id:
            continue
        row = conn.execute(
            "SELECT source_id,status,metadata_json FROM evidence_items WHERE id=?",
            (evidence_id,),
        ).fetchone()
        if row is None or row[0] != OFFICIAL_WEB_SOURCE_ID or row[1] != "usable":
            continue
        metadata = _parse_json(row[2])
        if not isinstance(metadata, dict):
            continue
        if metadata.get("acquisition_kind") != "bounded_official_website":
            continue
        if metadata.get("home_page") is not True:
            continue
        start_value = metadata.get("start_url")
        final_value = metadata.get("final_url")
        if not isinstance(start_value, str) or not isinstance(final_value, str):
            continue
        start_url = normalize_http_url(start_value)
        final_url = normalize_http_url(final_value)
        if start_url is None or final_url is None or not same_site(start_url, final_url):
            continue
        if start_url == left_url and final_url == right_url:
            return True
    return False


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
    _instant(valid_from, field=f"fact {fact_id} valid_from")
    _instant(reconciled_at, field=f"fact {fact_id} reconciled_at")
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
    _instant(valid_to, field=f"fact {fact_id} valid_to")
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

    Semantic URL equivalence avoids representation-only conflicts, but support
    edges remain exact-value claims. A historical observation is therefore never
    attached as support to a differently serialized current value.
    """
    if not observations:
        return 0, 0, 0
    observed_instant = _instant(observed_at, field="website observation observed_at")
    _instant(reconciled_at, field="website reconciliation reconciled_at")
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

    current_valid_from = _instant(
        current["valid_from"], field=f"current fact {current['id']} valid_from"
    )
    current_last_verified = _instant(
        current["last_verified_at"] or current["valid_from"],
        field=f"current fact {current['id']} last_verified_at",
    )
    previous = _usable_supports(conn, str(current["id"]))
    previous_sources = {item["source_id"] for item in previous}
    exact_same = (
        current["value_json"] == value_json and current["value_hash"] == value_hash
    )
    semantically_same = values_equivalent(
        predicate, current["value_json"], value_json
    ) or (
        predicate == "business.website.official"
        and _verified_website_alias_from_new_evidence(
            conn, observations, current["value_json"], value_json
        )
    )

    if observed_instant < current_valid_from:
        if exact_same and (
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

    if exact_same and observed_instant <= current_last_verified:
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
    exact_same_old = [item for item in previous if item["value_json"] == value_json]
    support_sources = {item["source_id"] for item in exact_same_old}
    support_sources.add(OFFICIAL_WEB_SOURCE_ID)
    status = (
        "confirmed"
        if (exact_same and current["status"] == "confirmed")
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
    for item in exact_same_old:
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
    if not semantically_same:
        for item in previous:
            equivalent = values_equivalent(
                predicate, item["value_json"], value_json
            ) or (
                predicate == "business.website.official"
                and _verified_website_alias_from_new_evidence(
                    conn, observations, item["value_json"], value_json
                )
            )
            if not equivalent:
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
    observed_instant = _instant(
        observed_at, field=f"{predicate} not_observed observed_at"
    )
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
        current_valid_from = _instant(
            current["valid_from"], field=f"current fact {current['id']} valid_from"
        )
        if observed_instant < current_valid_from:
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
