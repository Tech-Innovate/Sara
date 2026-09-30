from __future__ import annotations

import json
import math
import sqlite3
from typing import Any

from ..reviews.model import (
    COLLECTOR_NAME,
    REVIEW_PREDICATE,
    canonical_json,
    sha256_text,
)
from .core import DossierQueryError, json_value, parse_timestamp, resolve_subject, row_dict


_REVIEW_NORMALIZED_FIELDS = frozenset(
    {
        "review_id",
        "source",
        "rating",
        "rating_scale",
        "text_original",
        "text_translated",
        "language",
        "translated_language",
        "published_at",
        "updated_at",
        "source_when",
        "owner_response",
    }
)
_OWNER_RESPONSE_FIELDS = frozenset(
    {"text", "language", "translated_language", "published_at", "updated_at"}
)
_REVIEW_TEXT_FIELDS = (
    "review_id",
    "source",
    "text_original",
    "text_translated",
    "language",
    "translated_language",
    "published_at",
    "updated_at",
    "source_when",
)


def _bounded_normalized_review(value: dict[str, Any]) -> dict[str, Any] | None:
    """Return the explicitly permitted review projection or reject schema drift.

    ``normalized_value_json`` is shared producer storage, not a privacy boundary.
    The dossier therefore accepts only the bounded Review Intelligence schema
    emitted by ``reviews.parser`` and reconstructs the object from that allowlist
    before exposing it. This prevents a hash-valid producer-specific extension
    (for example reviewer profile/display metadata) from crossing into the
    Business Understanding read model.
    """
    if set(value) != _REVIEW_NORMALIZED_FIELDS:
        return None

    for field in _REVIEW_TEXT_FIELDS:
        field_value = value[field]
        if field_value is not None and not isinstance(field_value, str):
            return None

    rating = value["rating"]
    if rating is not None:
        if isinstance(rating, bool) or not isinstance(rating, (int, float)):
            return None
        if not math.isfinite(float(rating)) or float(rating) < 0:
            return None

    rating_scale = value["rating_scale"]
    if rating_scale is not None:
        if isinstance(rating_scale, bool) or not isinstance(rating_scale, int):
            return None
        if rating_scale <= 0:
            return None
    if rating is not None and rating_scale is not None and float(rating) > rating_scale:
        return None

    owner_response = value["owner_response"]
    bounded_response: dict[str, Any] | None = None
    if owner_response is not None:
        if not isinstance(owner_response, dict) or set(owner_response) != _OWNER_RESPONSE_FIELDS:
            return None
        if any(
            owner_response[field] is not None
            and not isinstance(owner_response[field], str)
            for field in _OWNER_RESPONSE_FIELDS
        ):
            return None
        bounded_response = {
            field: owner_response[field]
            for field in ("text", "language", "translated_language", "published_at", "updated_at")
        }

    return {
        "review_id": value["review_id"],
        "source": value["source"],
        "rating": rating,
        "rating_scale": rating_scale,
        "text_original": value["text_original"],
        "text_translated": value["text_translated"],
        "language": value["language"],
        "translated_language": value["translated_language"],
        "published_at": value["published_at"],
        "updated_at": value["updated_at"],
        "source_when": value["source_when"],
        "owner_response": bounded_response,
    }


def _location_lineage(
    conn: sqlite3.Connection, current_location_ids: list[str]
) -> list[str]:
    """Return every Location that currently redirects into the selected locations.

    Location ownership is intentionally not part of this traversal. A historical
    source-time Location may remain owned by its immutable source Entity even
    after it redirects into a current Location owned by a different canonical
    Entity. Review observations on that historical Location must remain visible
    to the current dossier without rewriting their subject.
    """
    current_ids = sorted(set(current_location_ids))
    if not current_ids:
        return []
    placeholders = ",".join("?" for _ in current_ids)
    rows = conn.execute(
        "WITH RECURSIVE lineage(id) AS ("
        f"SELECT id FROM knowledge_subjects WHERE kind='location' AND id IN ({placeholders}) "
        "UNION "
        "SELECT ks.id FROM knowledge_subjects ks "
        "JOIN lineage l ON ks.merged_into_subject_id=l.id "
        "WHERE ks.kind='location' AND ks.record_state='merged'"
        ") SELECT id FROM lineage ORDER BY id",
        tuple(current_ids),
    ).fetchall()
    return [str(row[0]) for row in rows]


def customer_review_observations(
    conn: sqlite3.Connection,
    current_location_ids: list[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Project provenance-valid evidence-only reviews onto the current dossier.

    Review lookup is restricted to the reverse redirect closure of the selected
    current Locations before any review payload is decoded. Review observations
    remain on their immutable source-time Location and are exposed only when that
    Location still resolves to one of the selected current Locations.

    A structurally reachable review whose evidence/session provenance or
    normalized-value integrity is invalid is reported as an integrity issue but
    is not promoted into ``customer_voice``. The projection exposes only the
    normalized customer statement and bounded provenance: raw/asserted review
    payloads and raw evidence metadata may contain reviewer profile or display
    identifiers and never cross this read-model boundary.
    """
    source_ids = _location_lineage(conn, current_location_ids)
    if not source_ids:
        return [], []

    current = set(current_location_ids)
    placeholders = ",".join("?" for _ in source_ids)
    cursor = conn.execute(
        "SELECT o.id,o.subject_id,o.normalized_value_json,o.value_hash,"
        "o.observation_kind,o.observed_at,o.extracted_at,o.extraction_method,"
        "o.extractor_name,o.extractor_version,o.confidence,"
        "e.id AS evidence_id,e.source_id,e.source_locator,e.source_role,"
        "e.status AS evidence_status,e.retrieved_at,e.published_at,e.language,"
        "e.media_type,e.content_sha256,e.artifact_ref,e.acquisition_session_id,"
        "a.target_subject_id AS acquisition_target_subject_id,"
        "a.source_id AS acquisition_source_id,a.collector_name,a.collector_version,"
        "a.status AS acquisition_status,a.started_at AS acquisition_started_at,"
        "a.finished_at AS acquisition_finished_at,"
        "s.source_type,s.name AS source_name,s.base_url "
        "FROM observations o "
        "LEFT JOIN evidence_items e ON e.id=o.evidence_id "
        "LEFT JOIN acquisition_sessions a ON a.id=e.acquisition_session_id "
        "LEFT JOIN sources s ON s.id=e.source_id "
        f"WHERE o.predicate=? AND o.subject_id IN ({placeholders}) "
        "ORDER BY o.observed_at,o.id",
        (REVIEW_PREDICATE, *source_ids),
    )

    reviews: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    for row in cursor.fetchall():
        item = row_dict(cursor, row)
        observation_id = str(item["id"])
        source_subject_id = str(item["subject_id"])
        try:
            resolved = resolve_subject(conn, source_subject_id, "location")
        except DossierQueryError as exc:
            issues.append(
                {
                    "code": "customer_review_subject_resolution_failed",
                    "observation_id": observation_id,
                    "subject_id": source_subject_id,
                    "error": str(exc),
                }
            )
            continue
        canonical_location_id = str(resolved["canonical"]["id"])
        if canonical_location_id not in current:
            issues.append(
                {
                    "code": "customer_review_source_location_left_selected_lineage",
                    "observation_id": observation_id,
                    "subject_id": source_subject_id,
                    "canonical_location_id": canonical_location_id,
                }
            )
            continue

        evidence_id = item["evidence_id"]
        if evidence_id is None:
            issues.append(
                {
                    "code": "customer_review_missing_evidence",
                    "observation_id": observation_id,
                }
            )
            continue
        if item["acquisition_session_id"] is None:
            issues.append(
                {
                    "code": "customer_review_missing_acquisition_session",
                    "observation_id": observation_id,
                    "evidence_id": evidence_id,
                }
            )
            continue
        if item["source_id"] is None or item["source_name"] is None:
            issues.append(
                {
                    "code": "customer_review_missing_source",
                    "observation_id": observation_id,
                    "evidence_id": evidence_id,
                }
            )
            continue

        provenance_valid = True
        if item["evidence_status"] != "usable":
            provenance_valid = False
            issues.append(
                {
                    "code": "customer_review_uses_nonusable_evidence",
                    "observation_id": observation_id,
                    "evidence_id": evidence_id,
                    "evidence_status": item["evidence_status"],
                }
            )
        if item["source_role"] != "customer_generated":
            provenance_valid = False
            issues.append(
                {
                    "code": "customer_review_source_role_mismatch",
                    "observation_id": observation_id,
                    "evidence_id": evidence_id,
                    "source_role": item["source_role"],
                }
            )
        if item["acquisition_source_id"] != item["source_id"]:
            provenance_valid = False
            issues.append(
                {
                    "code": "customer_review_source_session_mismatch",
                    "observation_id": observation_id,
                    "evidence_id": evidence_id,
                }
            )
        if item["acquisition_target_subject_id"] != source_subject_id:
            provenance_valid = False
            issues.append(
                {
                    "code": "customer_review_acquisition_target_mismatch",
                    "observation_id": observation_id,
                    "evidence_id": evidence_id,
                    "subject_id": source_subject_id,
                    "acquisition_target_subject_id": item["acquisition_target_subject_id"],
                }
            )
        if item["acquisition_status"] not in {"complete", "partial"}:
            provenance_valid = False
            issues.append(
                {
                    "code": "customer_review_from_unusable_acquisition_state",
                    "observation_id": observation_id,
                    "evidence_id": evidence_id,
                    "acquisition_status": item["acquisition_status"],
                }
            )
        if not provenance_valid:
            continue

        parsed_timestamps: dict[str, Any] = {}
        timestamp_specs = (
            (
                "retrieved_at",
                item["retrieved_at"],
                "customer_review_retrieved_at_invalid",
                f"review evidence {evidence_id} retrieved_at",
            ),
            (
                "extracted_at",
                item["extracted_at"],
                "customer_review_extracted_at_invalid",
                f"review observation {observation_id} extracted_at",
            ),
            (
                "acquisition_started_at",
                item["acquisition_started_at"],
                "customer_review_acquisition_started_at_invalid",
                f"review acquisition {item['acquisition_session_id']} started_at",
            ),
            (
                "acquisition_finished_at",
                item["acquisition_finished_at"],
                "customer_review_acquisition_finished_at_invalid",
                f"review acquisition {item['acquisition_session_id']} finished_at",
            ),
        )
        timestamp_valid = True
        for timestamp_key, timestamp_value, issue_code, field_name in timestamp_specs:
            try:
                parsed_timestamps[timestamp_key] = parse_timestamp(
                    timestamp_value,
                    field=field_name,
                )
            except DossierQueryError as exc:
                timestamp_valid = False
                issues.append(
                    {
                        "code": issue_code,
                        "observation_id": observation_id,
                        "evidence_id": evidence_id,
                        "error": str(exc),
                    }
                )
        if item["observed_at"] is not None:
            try:
                parsed_timestamps["observed_at"] = parse_timestamp(
                    item["observed_at"],
                    field=f"review observation {observation_id} observed_at",
                )
            except DossierQueryError as exc:
                timestamp_valid = False
                issues.append(
                    {
                        "code": "customer_review_observed_at_invalid",
                        "observation_id": observation_id,
                        "evidence_id": evidence_id,
                        "error": str(exc),
                    }
                )
        if not timestamp_valid:
            continue
        if (
            parsed_timestamps["acquisition_finished_at"]
            < parsed_timestamps["acquisition_started_at"]
        ):
            issues.append(
                {
                    "code": "customer_review_acquisition_chronology_invalid",
                    "observation_id": observation_id,
                    "evidence_id": evidence_id,
                    "acquisition_session_id": item["acquisition_session_id"],
                    "acquisition_started_at": item["acquisition_started_at"],
                    "acquisition_finished_at": item["acquisition_finished_at"],
                }
            )
            continue
        if (
            "observed_at" in parsed_timestamps
            and parsed_timestamps["observed_at"] > parsed_timestamps["extracted_at"]
        ):
            issues.append(
                {
                    "code": "customer_review_observation_chronology_invalid",
                    "observation_id": observation_id,
                    "evidence_id": evidence_id,
                    "observed_at": item["observed_at"],
                    "extracted_at": item["extracted_at"],
                }
            )
            continue

        try:
            normalized = json_value(
                item["normalized_value_json"],
                field=f"review observation {observation_id} normalized_value_json",
            )
        except DossierQueryError as exc:
            issues.append(
                {
                    "code": "customer_review_normalized_value_malformed",
                    "observation_id": observation_id,
                    "evidence_id": evidence_id,
                    "error": str(exc),
                }
            )
            continue
        if not isinstance(normalized, dict):
            issues.append(
                {
                    "code": "customer_review_value_not_object",
                    "observation_id": observation_id,
                    "evidence_id": evidence_id,
                }
            )
            continue
        expected_value_hash = sha256_text(canonical_json(normalized))
        if item["value_hash"] != expected_value_hash:
            issues.append(
                {
                    "code": "customer_review_value_hash_mismatch",
                    "observation_id": observation_id,
                    "evidence_id": evidence_id,
                }
            )
            continue
        bounded_normalized = _bounded_normalized_review(normalized)
        if bounded_normalized is None:
            issues.append(
                {
                    "code": "customer_review_normalized_schema_invalid",
                    "observation_id": observation_id,
                    "evidence_id": evidence_id,
                }
            )
            continue

        reviews.append(
            {
                "observation_id": observation_id,
                "source_location_id": source_subject_id,
                "canonical_location_id": canonical_location_id,
                "location_resolution_chain": list(resolved["chain"]),
                "normalized_value": bounded_normalized,
                "value_hash": item["value_hash"],
                "observation_kind": item["observation_kind"],
                "observed_at": item["observed_at"],
                "extracted_at": item["extracted_at"],
                "extraction_method": item["extraction_method"],
                "extractor_name": item["extractor_name"],
                "extractor_version": item["extractor_version"],
                "confidence": item["confidence"],
                "evidence": {
                    "id": evidence_id,
                    "source_id": item["source_id"],
                    "source_type": item["source_type"],
                    "source_name": item["source_name"],
                    "source_base_url": item["base_url"],
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
                    "acquisition_target_subject_id": item["acquisition_target_subject_id"],
                    "collector_name": item["collector_name"],
                    "collector_version": item["collector_version"],
                    "acquisition_status": item["acquisition_status"],
                    "acquisition_started_at": item["acquisition_started_at"],
                    "acquisition_finished_at": item["acquisition_finished_at"],
                },
            }
        )

    reviews.sort(key=lambda item: (str(item["observed_at"]), str(item["observation_id"])))
    issues.sort(
        key=lambda item: (
            str(item.get("code", "")),
            str(item.get("observation_id", "")),
            str(item.get("evidence_id", "")),
        )
    )
    return reviews, issues


def review_evidence_unavailable_outcomes(
    conn: sqlite3.Connection,
    current_location_ids: list[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Project complete review extractions that found NO retained reviews.

    The explicit-unavailable outcome is durable session state (F-01): a
    complete sara.reviews.maps_snapshot session whose frozen config
    carries extraction_outcome="unavailable" proves the retained
    snapshot was mined and contained zero review records. Reputation
    assessment consumes this so an explicitly unavailable review source
    can satisfy the domain instead of the planner re-scheduling a
    provably no-op extraction forever. Sessions whose config cannot be
    decoded are integrity issues, never outcomes.

    R8-04: pre-outcome v1 configs gain the semantics by derivation — a
    v1 session with zero review evidence rows IS a zero-review
    extraction by construction. R8-05: each outcome carries its source
    freshness (the retained snapshot's retrieved_at) so consumers
    measure currency from the SOURCE, never the extraction clock; a
    config naming missing evidence is an integrity issue, not an
    outcome.

    R9-03/R9-04/R10-03: every session-level and parent-provenance
    check lives in ONE strict verifier owned by the collector module
    (reviews.core.strict_unavailable_outcome_session) so the
    projection cannot drift from the executor's contract: supported
    review collector version, canonical config bytes and hash,
    deterministic session id, frozen-target binding, zero stored AND
    actual output, and the executor's exact current-snapshot parent
    provenance. A superseded snapshot drops its outcome (R10-04);
    unknown/future review versions never contribute mandatory-domain
    evidence.
    """
    source_ids = _location_lineage(conn, current_location_ids)
    if not source_ids:
        return [], []
    placeholders = ",".join("?" for _ in source_ids)
    cursor = conn.execute(
        "SELECT id,target_subject_id,collector_name,collector_version,"
        "config_json,config_hash,status,finished_at,evidence_count,"
        "observation_count "
        "FROM acquisition_sessions "
        f"WHERE collector_name=? AND target_subject_id IN ({placeholders}) "
        "AND status='complete' ORDER BY finished_at,id",
        (COLLECTOR_NAME, *source_ids),
    )
    from ..reviews.core import strict_unavailable_outcome_session

    outcomes: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    for row in cursor.fetchall():
        item = row_dict(cursor, row)
        outcome, issue_code = strict_unavailable_outcome_session(conn, item)
        if issue_code is not None:
            issues.append(
                {"code": issue_code, "session_id": str(item["id"])}
            )
        elif outcome is not None:
            outcomes.append(outcome)
    issues.sort(
        key=lambda item: (
            str(item.get("code", "")),
            str(item.get("session_id", "")),
        )
    )
    return outcomes, issues
