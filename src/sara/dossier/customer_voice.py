from __future__ import annotations

import json
import math
import sqlite3
from typing import Any

from ..reviews.model import (
    COLLECTOR_NAME,
    REVIEW_ARRAY_FIELDS,
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
_GOOGLE_MAPS_SOURCE_ID = "src_google_maps"
_MAPS_COLLECTOR_KINDS = {
    "sara.maps_backfill": "legacy_maps_business_snapshot",
    "sara.maps_sync": "maps_sync_snapshot",
}
_LEGACY_V1_CONFIG_KEYS = frozenset({
    "input_kind",
    "source_maps_business_id",
    "source_business_entity_id",
    "source_location_id",
    "source_evidence_id",
    "source_content_sha256",
    "review_array_fields",
    "source_review_records",
    "review_evidence_records",
})


def _is_strict_legacy_v1_zero_config(config: dict[str, Any]) -> bool:
    """R9-03: the EXACT canonical pre-outcome v1 config shape with
    zero declared review records on both count keys. Anything else
    fails the inference and surfaces as an integrity issue."""
    if set(config) != _LEGACY_V1_CONFIG_KEYS:
        return False
    if config["input_kind"] != "retained_maps_review_snapshot":
        return False
    if config["review_array_fields"] != list(REVIEW_ARRAY_FIELDS):
        return False
    if config["source_review_records"] != 0:
        return False
    if config["review_evidence_records"] != 0:
        return False
    business_id = config["source_maps_business_id"]
    if isinstance(business_id, bool) or not isinstance(business_id, int):
        return False
    for key in (
        "source_business_entity_id",
        "source_location_id",
        "source_evidence_id",
        "source_content_sha256",
    ):
        value = config[key]
        if not isinstance(value, str) or not value:
            return False
    return True


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

    R9-03: the v1 derivation applies ONLY to rigorously verified v1
    rows — collector version 1, the exact canonical legacy config
    shape with zero declared counts, and zero actual evidence and
    observation rows. Any other no-outcome zero-evidence row (a
    malformed v2/future row) is an integrity issue, never an outcome;
    a no-outcome row WITH evidence is a historical with-reviews
    session and is silently skipped. R9-04: the referenced retained
    Maps evidence is validated against the frozen content hash, Maps
    source/role/status, the Maps collector session (name, version,
    complete) and its snapshot metadata before any outcome is
    projected — the same fail-closed provenance discipline review
    observations get. A mismatch is an integrity issue and produces
    no outcome.
    """
    source_ids = _location_lineage(conn, current_location_ids)
    if not source_ids:
        return [], []
    placeholders = ",".join("?" for _ in source_ids)
    cursor = conn.execute(
        "SELECT id,target_subject_id,collector_name,collector_version,"
        "config_json,status,finished_at,evidence_count "
        "FROM acquisition_sessions "
        f"WHERE collector_name=? AND target_subject_id IN ({placeholders}) "
        "AND status='complete' ORDER BY finished_at,id",
        (COLLECTOR_NAME, *source_ids),
    )
    outcomes: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    for row in cursor.fetchall():
        item = row_dict(cursor, row)
        session_id = str(item["id"])
        try:
            config = json.loads(str(item["config_json"]))
            if not isinstance(config, dict):
                raise ValueError("config is not a JSON object")
        except ValueError:
            issues.append(
                {
                    "code": "review_outcome_session_config_invalid",
                    "session_id": session_id,
                }
            )
            continue
        outcome = config.get("extraction_outcome")
        if outcome is None:
            if int(item["evidence_count"] or 0) != 0:
                # A historical no-outcome session that DID produce
                # review evidence: nothing to project here.
                continue
            # R9-03: infer unavailable ONLY for a rigorously verified
            # v1 zero-review row: version 1, canonical legacy config
            # shape, zero declared counts, zero ACTUAL evidence and
            # observation rows. Anything else is an integrity issue.
            actual_evidence = int(conn.execute(
                "SELECT COUNT(*) FROM evidence_items "
                "WHERE acquisition_session_id=?", (session_id,)
            ).fetchone()[0])
            actual_observations = int(conn.execute(
                "SELECT COUNT(*) FROM observations o "
                "JOIN evidence_items e ON e.id=o.evidence_id "
                "WHERE e.acquisition_session_id=?", (session_id,)
            ).fetchone()[0])
            if (
                str(item["collector_version"]) != "1"
                or not _is_strict_legacy_v1_zero_config(config)
                or actual_evidence != 0
                or actual_observations != 0
            ):
                issues.append(
                    {
                        "code": "review_outcome_session_config_invalid",
                        "session_id": session_id,
                    }
                )
                continue
            outcome = "unavailable"
        if outcome != "unavailable":
            continue
        finished_at = item["finished_at"]
        if not isinstance(finished_at, str) or not finished_at:
            issues.append(
                {
                    "code": "review_outcome_session_config_invalid",
                    "session_id": session_id,
                }
            )
            continue
        parse_timestamp(
            finished_at,
            field=f"review outcome session {session_id} finished_at",
        )
        # R8-05 + R9-04: outcome currency is SOURCE currency, and the
        # referenced retained Maps evidence must pass the same
        # fail-closed provenance discipline review observations get:
        # frozen content hash, Maps source/role/status, a complete
        # Maps collector session of the expected version, and snapshot
        # metadata that matches the frozen business/location identity.
        source_evidence_id = config.get("source_evidence_id")
        evidence_row = None
        if isinstance(source_evidence_id, str) and source_evidence_id:
            evidence_row = conn.execute(
                "SELECT e.content_sha256,e.source_id,e.source_role,e.status,"
                "e.retrieved_at,a.collector_name,a.collector_version,"
                "a.status AS session_status,e.metadata_json "
                "FROM evidence_items e "
                "JOIN acquisition_sessions a ON a.id=e.acquisition_session_id "
                "WHERE e.id=?",
                (source_evidence_id,),
            ).fetchone()
        if evidence_row is None:
            issues.append(
                {
                    "code": "review_outcome_source_evidence_missing",
                    "session_id": session_id,
                }
            )
            continue
        (
            ev_content_sha,
            ev_source_id,
            ev_source_role,
            ev_status,
            ev_retrieved_at,
            ev_collector,
            ev_collector_version,
            ev_session_status,
            ev_metadata_json,
        ) = evidence_row
        expected_kind = _MAPS_COLLECTOR_KINDS.get(str(ev_collector))
        try:
            ev_metadata = json.loads(str(ev_metadata_json))
            if not isinstance(ev_metadata, dict):
                raise ValueError("metadata is not a JSON object")
        except ValueError:
            ev_metadata = None
        provenance_valid = (
            str(ev_source_id) == _GOOGLE_MAPS_SOURCE_ID
            and str(ev_source_role) == "platform"
            and str(ev_status) == "usable"
            and str(ev_session_status) == "complete"
            and str(ev_content_sha) == str(config.get("source_content_sha256"))
            and expected_kind is not None
            and ev_metadata is not None
            and ev_metadata.get("import_kind") == expected_kind
        )
        if provenance_valid:
            from ..maps_backfill import BACKFILL_VERSION
            from ..maps_sync import SYNC_VERSION

            expected_version = (
                BACKFILL_VERSION
                if str(ev_collector) == "sara.maps_backfill"
                else SYNC_VERSION
            )
            identity_matches = (
                ev_metadata.get("legacy_business_id")
                == config.get("source_maps_business_id")
                if expected_kind == "legacy_maps_business_snapshot"
                else ev_metadata.get("sync_location_id")
                == config.get("source_location_id")
            )
            provenance_valid = (
                str(ev_collector_version) == str(expected_version)
                and identity_matches
            )
        if not provenance_valid or not isinstance(ev_retrieved_at, str) \
                or not ev_retrieved_at:
            issues.append(
                {
                    "code": "review_outcome_source_evidence_mismatch",
                    "session_id": session_id,
                }
            )
            continue
        source_retrieved_at = str(ev_retrieved_at)
        parse_timestamp(
            source_retrieved_at,
            field=(
                f"review outcome session {session_id} "
                f"source retrieved_at"
            ),
        )
        outcomes.append(
            {
                "session_id": session_id,
                "target_subject_id": str(item["target_subject_id"]),
                "collector_version": str(item["collector_version"]),
                "finished_at": finished_at,
                "source_evidence_id": str(source_evidence_id),
                "source_content_sha256": str(config.get("source_content_sha256")),
                "source_location_id": str(config.get("source_location_id")),
                "source_retrieved_at": source_retrieved_at,
            }
        )
    issues.sort(
        key=lambda item: (
            str(item.get("code", "")),
            str(item.get("session_id", "")),
        )
    )
    return outcomes, issues
