from __future__ import annotations

import sqlite3
from typing import Any

from ..reviews.model import REVIEW_PREDICATE
from .core import DossierQueryError, json_value, resolve_subject, row_dict


def customer_review_observations(
    conn: sqlite3.Connection,
    current_location_ids: list[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Project evidence-only customer reviews onto the current dossier identity.

    Review observations remain attached to their immutable source-time Location.
    A review is included when that Location currently resolves to one of the
    selected entity's active Locations. The projection deliberately exposes the
    normalized customer statement plus bounded provenance, not raw evidence
    metadata that may contain reviewer profile/display identifiers.
    """
    if not current_location_ids:
        return [], []

    current = set(current_location_ids)
    cursor = conn.execute(
        "SELECT o.id,o.subject_id,o.value_json,o.normalized_value_json,o.value_hash,"
        "o.observation_kind,o.observed_at,o.extracted_at,o.extraction_method,"
        "o.extractor_name,o.extractor_version,o.confidence,"
        "e.id AS evidence_id,e.source_id,e.source_locator,e.source_role,"
        "e.status AS evidence_status,e.retrieved_at,e.published_at,e.language,"
        "e.media_type,e.content_sha256,e.artifact_ref,e.acquisition_session_id,"
        "a.source_id AS acquisition_source_id,a.collector_name,a.collector_version,"
        "a.status AS acquisition_status,s.source_type,s.name AS source_name,s.base_url "
        "FROM observations o "
        "LEFT JOIN evidence_items e ON e.id=o.evidence_id "
        "LEFT JOIN acquisition_sessions a ON a.id=e.acquisition_session_id "
        "LEFT JOIN sources s ON s.id=e.source_id "
        "WHERE o.predicate=? ORDER BY o.observed_at,o.id",
        (REVIEW_PREDICATE,),
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

        if item["evidence_status"] != "usable":
            issues.append(
                {
                    "code": "customer_review_uses_nonusable_evidence",
                    "observation_id": observation_id,
                    "evidence_id": evidence_id,
                    "evidence_status": item["evidence_status"],
                }
            )
        if item["source_role"] != "customer_generated":
            issues.append(
                {
                    "code": "customer_review_source_role_mismatch",
                    "observation_id": observation_id,
                    "evidence_id": evidence_id,
                    "source_role": item["source_role"],
                }
            )
        if item["acquisition_source_id"] != item["source_id"]:
            issues.append(
                {
                    "code": "customer_review_source_session_mismatch",
                    "observation_id": observation_id,
                    "evidence_id": evidence_id,
                }
            )
        if item["acquisition_status"] not in {"complete", "partial"}:
            issues.append(
                {
                    "code": "customer_review_from_unusable_acquisition_state",
                    "observation_id": observation_id,
                    "evidence_id": evidence_id,
                    "acquisition_status": item["acquisition_status"],
                }
            )

        value = json_value(item["value_json"], field=f"review observation {observation_id} value_json")
        normalized = json_value(
            item["normalized_value_json"],
            field=f"review observation {observation_id} normalized_value_json",
        )
        if not isinstance(value, dict) or not isinstance(normalized, dict):
            issues.append(
                {
                    "code": "customer_review_value_not_object",
                    "observation_id": observation_id,
                    "evidence_id": evidence_id,
                }
            )

        reviews.append(
            {
                "observation_id": observation_id,
                "source_location_id": source_subject_id,
                "canonical_location_id": canonical_location_id,
                "location_resolution_chain": list(resolved["chain"]),
                "value": value,
                "normalized_value": normalized,
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
                    "collector_name": item["collector_name"],
                    "collector_version": item["collector_version"],
                    "acquisition_status": item["acquisition_status"],
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
