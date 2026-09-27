from __future__ import annotations

import sqlite3
from typing import Any

from ..reviews.model import REVIEW_PREDICATE, canonical_json, sha256_text
from .core import DossierQueryError, json_value, resolve_subject, row_dict


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
        "a.source_id AS acquisition_source_id,a.collector_name,a.collector_version,"
        "a.status AS acquisition_status,s.source_type,s.name AS source_name,s.base_url "
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

        reviews.append(
            {
                "observation_id": observation_id,
                "source_location_id": source_subject_id,
                "canonical_location_id": canonical_location_id,
                "location_resolution_chain": list(resolved["chain"]),
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
