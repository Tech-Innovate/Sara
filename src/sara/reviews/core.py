from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .. import maps_backfill as mb
from .. import maps_sync as ms
from ..dossier.core import (
    DossierQueryError,
    locations as dossier_locations,
    maps_businesses as dossier_maps_businesses,
    resolve_selection,
    resolve_subject,
)
from ..migrations import MigrationError, apply_migrations
from ..storage import connect_existing
from ..understanding_vocabulary import (
    VocabularySeedError,
    seed_business_understanding_vocabulary,
    verify_business_understanding_vocabulary,
)
from .model import (
    COLLECTOR_NAME,
    COLLECTOR_VERSION,
    ReviewTargetUnavailableError,
    REVIEW_ARRAY_FIELDS,
    REVIEW_PREDICATE,
    ParsedReview,
    ReviewExtractionStats,
    ReviewIntelligenceError,
    canonical_json,
    opaque_id,
    sha256_text,
)
from .parser import extract_reviews


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# Collector v1 froze the pre-outcome config shape; v1 sessions replay
# under this version through the compatibility path (F-05).
_LEGACY_COLLECTOR_VERSION = "1"


def _validated_timestamp(value: str, *, field: str) -> str:
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError as exc:
        raise ReviewIntelligenceError(f"{field} is not valid ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ReviewIntelligenceError(f"{field} must be timezone-aware")
    return parsed.astimezone(timezone.utc).isoformat()


def _parse_json_object(value: object, *, field: str) -> dict[str, Any]:
    if not isinstance(value, str):
        raise ReviewIntelligenceError(f"{field} is not stored as JSON text")
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ReviewIntelligenceError(f"{field} contains malformed JSON") from exc
    if not isinstance(parsed, dict):
        raise ReviewIntelligenceError(f"{field} is not a JSON object")
    return parsed


def _source_business(
    conn: sqlite3.Connection, *, business_id: int
) -> dict[str, Any]:
    cursor = conn.execute(
        "SELECT id,canonical_key,title,last_seen_at,last_run_id,raw_json "
        "FROM businesses WHERE id=?",
        (business_id,),
    )
    row = cursor.fetchone()
    if row is None:
        raise ReviewTargetUnavailableError(
            f"canonical Maps business {business_id} does not exist")
    return {description[0]: row[index] for index, description in enumerate(cursor.description or ())}


def _resolve_target(
    conn: sqlite3.Connection,
    *,
    business_id: int | None,
    canonical_key: str | None,
) -> tuple[int, str, str, dict[str, Any]]:
    if (business_id is None) == (canonical_key is None):
        raise ReviewIntelligenceError("select exactly one of business_id or canonical_key")
    if business_id is not None and business_id <= 0:
        raise ReviewIntelligenceError("business_id must be greater than zero")
    if canonical_key is not None and not canonical_key.strip():
        raise ReviewIntelligenceError("canonical_key must not be blank")
    try:
        verify_business_understanding_vocabulary(conn)
        entity_id, selection = resolve_selection(
            conn,
            business_id=business_id,
            canonical_key=canonical_key.strip() if canonical_key is not None else None,
            entity_id=None,
        )
    except VocabularySeedError as exc:
        raise ReviewIntelligenceError(str(exc)) from exc
    except DossierQueryError as exc:
        raise ReviewTargetUnavailableError(str(exc)) from exc
    maps_business = selection.get("maps_business")
    if not isinstance(maps_business, dict) or not isinstance(maps_business.get("id"), int):
        raise ReviewTargetUnavailableError(
            "review extraction requires a current Maps business selector")
    location_id = selection.get("canonical_location_id")
    if not isinstance(location_id, str) or not location_id:
        raise ReviewTargetUnavailableError(
            "selected Maps business has no current Understanding location")
    source = _source_business(conn, business_id=int(maps_business["id"]))
    return int(maps_business["id"]), entity_id, location_id, source


def _maps_source_evidence(
    conn: sqlite3.Connection,
    *,
    business: dict[str, Any],
    location_id: str,
) -> dict[str, Any]:
    raw_json = business.get("raw_json")
    if not isinstance(raw_json, str):
        raise ReviewIntelligenceError("canonical Maps business raw_json is not text")
    raw_hash = sha256_text(raw_json)
    run_id = business.get("last_run_id")
    if not isinstance(run_id, str) or not run_id:
        raise ReviewIntelligenceError("canonical Maps business has no last_run_id")
    business_id = int(business["id"])
    canonical_key = business.get("canonical_key")
    expected_evidence_ids = (
        mb._evidence_id(business_id, raw_hash),
        ms._sync_evidence_id(business_id, run_id, raw_hash),
    )
    candidates: list[dict[str, Any]] = []
    cursor = conn.execute(
        "SELECT e.id,e.acquisition_session_id,e.source_id,e.source_locator,e.source_role,e.status,"
        "e.retrieved_at,e.content_sha256,e.artifact_ref,e.metadata_json,e.created_at,"
        "a.collector_name,a.collector_version,a.status AS session_status,a.legacy_run_id "
        "FROM evidence_items e JOIN acquisition_sessions a ON a.id=e.acquisition_session_id "
        "WHERE e.id IN (?,?) AND e.source_id=? AND a.source_id=? "
        "AND e.source_role='platform' AND e.status='usable' "
        "AND a.collector_name IN (?,?) AND a.legacy_run_id=?",
        (
            *expected_evidence_ids,
            mb.GOOGLE_MAPS_SOURCE_ID,
            mb.GOOGLE_MAPS_SOURCE_ID,
            mb.BACKFILL_COLLECTOR_NAME,
            ms.SYNC_COLLECTOR_NAME,
            run_id,
        ),
    )
    for row in cursor.fetchall():
        item = {description[0]: row[index] for index, description in enumerate(cursor.description or ())}
        if item["session_status"] != "complete":
            continue
        if item["collector_name"] == mb.BACKFILL_COLLECTOR_NAME:
            if item["collector_version"] != mb.BACKFILL_VERSION:
                continue
            expected_kind = "legacy_maps_business_snapshot"
        else:
            if item["collector_version"] != ms.SYNC_VERSION:
                continue
            expected_kind = "maps_sync_snapshot"
        metadata = _parse_json_object(item["metadata_json"], field=f"Maps evidence {item['id']} metadata")
        if metadata.get("import_kind") != expected_kind:
            continue
        if metadata.get("legacy_business_id") != business_id:
            continue
        if metadata.get("legacy_canonical_key") != canonical_key:
            continue
        if str(metadata.get("legacy_run_id")) != run_id or str(item["legacy_run_id"]) != run_id:
            continue
        if metadata.get("raw_json") != raw_json:
            continue
        if item["content_sha256"] != raw_hash:
            raise ReviewIntelligenceError(
                f"Maps evidence {item['id']} content hash disagrees with its retained raw snapshot"
            )
        if str(item["retrieved_at"]) != str(business["last_seen_at"]):
            raise ReviewIntelligenceError(
                f"Maps evidence {item['id']} retrieval time disagrees with canonical Maps state"
            )
        if expected_kind == "legacy_maps_business_snapshot":
            frozen_entity_id = mb.business_entity_id_for_maps_business(business_id)
            frozen_location_id = mb.location_id_for_maps_business(business_id)
        else:
            frozen_entity_id = metadata.get("sync_entity_id")
            frozen_location_id = metadata.get("sync_location_id")
            if not isinstance(frozen_entity_id, str) or not frozen_entity_id:
                raise ReviewIntelligenceError(
                    f"Maps evidence {item['id']} has no valid frozen entity anchor"
                )
            if not isinstance(frozen_location_id, str) or not frozen_location_id:
                raise ReviewIntelligenceError(
                    f"Maps evidence {item['id']} has no valid frozen location anchor"
                )
        owner = conn.execute(
            "SELECT business_entity_id FROM business_locations WHERE id=?",
            (frozen_location_id,),
        ).fetchone()
        if owner is None or str(owner[0]) != frozen_entity_id:
            raise ReviewIntelligenceError(
                f"Maps evidence {item['id']} frozen subject ownership is inconsistent"
            )
        try:
            resolved_location = resolve_subject(conn, frozen_location_id, "location")
            resolve_subject(conn, frozen_entity_id, "business_entity")
        except DossierQueryError as exc:
            raise ReviewIntelligenceError(
                f"Maps evidence {item['id']} has an invalid frozen subject anchor: {exc}"
            ) from exc
        if str(resolved_location["canonical"]["id"]) != location_id:
            raise ReviewIntelligenceError(
                f"Maps evidence {item['id']} does not resolve to the selected current location"
            )
        # Location convergence can legitimately move the canonical Location
        # under another provisional Business Entity while the source-time
        # Location keeps its immutable historical owner. Preserve that source
        # entity instead of requiring it to equal today's canonical owner.
        item["metadata"] = metadata
        item["frozen_entity_id"] = frozen_entity_id
        item["frozen_location_id"] = frozen_location_id
        candidates.append(item)
    if not candidates:
        raise ReviewTargetUnavailableError(
            "no exact retained Maps evidence matches the current business snapshot; "
            "run sara-maps-sync before review extraction"
        )
    if len(candidates) != 1:
        raise ReviewIntelligenceError(
            "multiple retained Maps evidence items claim the exact current business snapshot"
        )
    return candidates[0]


def _session_config(
    *,
    source_evidence: dict[str, Any],
    source_review_records: int,
    review_evidence_records: int,
) -> str:
    return canonical_json(
        {
            "input_kind": "retained_maps_review_snapshot",
            "source_maps_business_id": source_evidence["metadata"]["legacy_business_id"],
            "source_business_entity_id": source_evidence["frozen_entity_id"],
            "source_location_id": source_evidence["frozen_location_id"],
            "source_evidence_id": source_evidence["id"],
            "source_content_sha256": source_evidence["content_sha256"],
            "review_array_fields": list(REVIEW_ARRAY_FIELDS),
            "source_review_records": source_review_records,
            "review_evidence_records": review_evidence_records,
        }
    )


def _review_evidence_id(session_id: str, review: ParsedReview) -> str:
    return opaque_id("ev", session_id, review.identity_key, review.raw_sha256)


def _review_observation_id(evidence_id: str, review: ParsedReview) -> str:
    return opaque_id("obs", evidence_id, REVIEW_PREDICATE, review.value_sha256)


def _source_locator(source_evidence: dict[str, Any], review: ParsedReview) -> str:
    if review.review_id is not None:
        return f"google_maps_review:{review.review_id}"
    parent = source_evidence.get("source_locator")
    prefix = str(parent) if parent not in (None, "") else str(source_evidence["id"])
    return f"{prefix}#review-fingerprint={review.value_sha256}"


def _review_metadata(
    *,
    source_evidence: dict[str, Any],
    review: ParsedReview,
) -> str:
    return canonical_json(
        {
            "extraction_kind": "retained_maps_customer_review",
            "source_maps_business_id": source_evidence["metadata"]["legacy_business_id"],
            "source_business_entity_id": source_evidence["frozen_entity_id"],
            "source_location_id": source_evidence["frozen_location_id"],
            "parent_evidence_id": source_evidence["id"],
            "parent_content_sha256": source_evidence["content_sha256"],
            "parent_source_locator": source_evidence.get("source_locator"),
            "parent_artifact_ref": source_evidence.get("artifact_ref"),
            "review_identity": review.identity_key,
            "source_paths": list(review.source_paths),
            "source_review_id": review.review_id,
            "source_review_provider": review.source,
            "raw_review_json": review.raw_json,
            "normalized_value_sha256": review.value_sha256,
        }
    )


def _expected_review_rows(
    *,
    session_id: str,
    source_evidence: dict[str, Any],
    reviews: tuple[ParsedReview, ...],
    extracted_at: str,
    extractor_version: str = COLLECTOR_VERSION,
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    rows: list[tuple[dict[str, Any], dict[str, Any]]] = []
    observed_at = str(source_evidence["retrieved_at"])
    source_location_id = str(source_evidence["frozen_location_id"])
    for review in reviews:
        evidence_id = _review_evidence_id(session_id, review)
        evidence = {
            "id": evidence_id,
            "acquisition_session_id": session_id,
            "source_id": mb.GOOGLE_MAPS_SOURCE_ID,
            "source_locator": _source_locator(source_evidence, review),
            "source_role": "customer_generated",
            "status": "usable",
            "retrieved_at": observed_at,
            "published_at": review.published_at,
            "language": review.language,
            "media_type": "application/json",
            "content_sha256": review.raw_sha256,
            "artifact_ref": None,
            "metadata_json": _review_metadata(
                source_evidence=source_evidence,
                review=review,
            ),
            "created_at": extracted_at,
        }
        observation = {
            "id": _review_observation_id(evidence_id, review),
            "subject_id": source_location_id,
            "predicate": REVIEW_PREDICATE,
            "evidence_id": evidence_id,
            "value_json": review.value_json,
            "normalized_value_json": review.value_json,
            "value_hash": review.value_sha256,
            "observation_kind": "source_assertion",
            "observed_at": observed_at,
            "extracted_at": extracted_at,
            "extraction_method": "direct_structured",
            "extractor_name": COLLECTOR_NAME,
            "extractor_version": extractor_version,
            "confidence": 1.0,
            "created_at": extracted_at,
        }
        rows.append((evidence, observation))
    return rows


def _stats(
    *,
    session_id: str,
    business_id: int,
    business_entity_id: str,
    canonical_location_id: str,
    source_evidence: dict[str, Any],
    source_review_records: int,
    reviews: tuple[ParsedReview, ...],
    evidence_items_created: int,
    observations_created: int,
    already_extracted: bool,
    status: str = "complete",
) -> ReviewExtractionStats:
    return ReviewExtractionStats(
        session_id=session_id,
        business_id=business_id,
        business_entity_id=business_entity_id,
        source_business_entity_id=str(source_evidence["frozen_entity_id"]),
        source_location_id=str(source_evidence["frozen_location_id"]),
        canonical_location_id=canonical_location_id,
        source_evidence_id=str(source_evidence["id"]),
        source_review_records=source_review_records,
        review_evidence_records=len(reviews),
        evidence_items_created=evidence_items_created,
        observations_created=observations_created,
        duplicate_source_records_collapsed=source_review_records - len(reviews),
        already_extracted=already_extracted,
        status=status,
    )


def _compare_stored_children(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    expected_rows: list[tuple[dict[str, Any], dict[str, Any]]],
) -> None:
    """Raise ReviewIntelligenceError when stored evidence or observation
    rows drift from the executor's expected bytes.

    Shared by replay verification (_verify_existing) and planner-side
    coverage verification (review_session_mined_evidence, R12-02) so
    both enforce the identical child provenance contract: same counts
    with drifted content is drift, never coverage.
    """
    evidence_rows = conn.execute(
        "SELECT id,acquisition_session_id,source_id,source_locator,source_role,status,retrieved_at,"
        "published_at,language,media_type,content_sha256,artifact_ref,metadata_json,created_at "
        "FROM evidence_items WHERE acquisition_session_id=? ORDER BY id",
        (session_id,),
    ).fetchall()
    actual_evidence = [dict(row) for row in evidence_rows]
    expected_evidence = sorted((item[0] for item in expected_rows), key=lambda item: item["id"])
    if actual_evidence != expected_evidence:
        raise ReviewIntelligenceError(
            f"existing review extraction session {session_id} evidence has drifted"
        )
    observation_rows = conn.execute(
        "SELECT o.id,o.subject_id,o.predicate,o.evidence_id,o.value_json,o.normalized_value_json,"
        "o.value_hash,o.observation_kind,o.observed_at,o.extracted_at,o.extraction_method,"
        "o.extractor_name,o.extractor_version,o.confidence,o.created_at "
        "FROM observations o JOIN evidence_items e ON e.id=o.evidence_id "
        "WHERE e.acquisition_session_id=? ORDER BY o.id",
        (session_id,),
    ).fetchall()
    actual_observations = [dict(row) for row in observation_rows]
    expected_observations = sorted((item[1] for item in expected_rows), key=lambda item: item["id"])
    if actual_observations != expected_observations:
        raise ReviewIntelligenceError(
            f"existing review extraction session {session_id} observations have drifted"
        )


def _verify_existing(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    config_json: str,
    collector_version: str = COLLECTOR_VERSION,
    business_id: int,
    business_entity_id: str,
    canonical_location_id: str,
    source_evidence: dict[str, Any],
    reviews: tuple[ParsedReview, ...],
    source_review_records: int,
) -> ReviewExtractionStats | None:
    cursor = conn.execute(
        "SELECT id,target_subject_id,source_id,collector_name,collector_version,config_json,config_hash,"
        "status,started_at,finished_at,error,evidence_count,observation_count "
        "FROM acquisition_sessions WHERE id=?",
        (session_id,),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    session = {description[0]: row[index] for index, description in enumerate(cursor.description or ())}
    expected_static = {
        "id": session_id,
        "target_subject_id": source_evidence["frozen_location_id"],
        "source_id": mb.GOOGLE_MAPS_SOURCE_ID,
        "collector_name": COLLECTOR_NAME,
        "collector_version": collector_version,
        "config_json": config_json,
        "config_hash": sha256_text(config_json),
        "status": "complete",
        "error": None,
        "evidence_count": len(reviews),
        "observation_count": len(reviews),
    }
    if any(session[key] != value for key, value in expected_static.items()):
        raise ReviewIntelligenceError(
            f"existing review extraction session {session_id} has incompatible provenance"
        )
    if session["started_at"] in (None, "") or session["finished_at"] in (None, ""):
        raise ReviewIntelligenceError(
            f"existing review extraction session {session_id} has incomplete lifecycle timestamps"
        )
    extracted_at = str(session["finished_at"])
    expected_rows = _expected_review_rows(
        session_id=session_id,
        source_evidence=source_evidence,
        reviews=reviews,
        extracted_at=extracted_at,
        extractor_version=collector_version,
    )
    _compare_stored_children(
        conn, session_id=session_id, expected_rows=expected_rows)
    return _stats(
        session_id=session_id,
        business_id=business_id,
        business_entity_id=business_entity_id,
        canonical_location_id=canonical_location_id,
        source_evidence=source_evidence,
        source_review_records=source_review_records,
        reviews=reviews,
        evidence_items_created=0,
        observations_created=0,
        already_extracted=True,
        status="unavailable" if not reviews else "complete",
    )


def _record_failed_extraction(
    conn: sqlite3.Connection,
    *,
    source_evidence: dict[str, Any],
    error: str,
    now: Callable[[], str],
) -> str:
    """Persist one durable failed review-acquisition attempt (F-06).

    A deterministic malformed-review failure must leave durable
    acquisition state, or the planner would keep scheduling an action
    that provably cannot succeed. The failed-session id embeds the
    attempt timestamp (the per-attempt pattern the website collector
    uses), so repeated attempts on an unchanged malformed snapshot
    accumulate distinct rows and drive the review collector's scoped
    retry ceiling.
    """
    failed_at = _validated_timestamp(now(), field="review extraction time")
    config = canonical_json(
        {
            "input_kind": "retained_maps_review_snapshot",
            "source_maps_business_id": source_evidence["metadata"]["legacy_business_id"],
            "source_business_entity_id": source_evidence["frozen_entity_id"],
            "source_location_id": source_evidence["frozen_location_id"],
            "source_evidence_id": source_evidence["id"],
            "source_content_sha256": source_evidence["content_sha256"],
            "extraction_outcome": "failed",
        }
    )
    session_id = opaque_id(
        "acq",
        "retained-maps-reviews-failed",
        source_evidence["id"],
        str(source_evidence["frozen_location_id"]),
        COLLECTOR_VERSION,
        failed_at,
    )
    conn.execute("BEGIN IMMEDIATE")
    try:
        # R8-03: replay is decided by an exact-identity check under the
        # writer lock, NOT by suppressing constraint errors. Only a
        # byte-identical row for the same attempt id counts as a
        # genuine replay; anything else — a tampered row under this id,
        # or any FK/CHECK/NOT-NULL/trigger violation on the insert —
        # propagates untouched.
        # R9-02: the replay comparison covers the ENTIRE expected
        # session row — identity columns, collector name/version,
        # config bytes and hash, lifecycle, error, legacy run id, and
        # zero evidence/observation counts. Any single differing
        # column is a provenance mismatch, never a suppressed replay.
        stored = conn.execute(
            "SELECT id,target_subject_id,source_id,collector_name,collector_version,"
            "config_json,config_hash,status,started_at,finished_at,error,legacy_run_id,"
            "evidence_count,observation_count "
            "FROM acquisition_sessions WHERE id=?",
            (session_id,),
        ).fetchone()
        if stored is not None:
            # R10-02: stored counters are not database-enforced
            # summaries — evidence can reference a session regardless
            # of its evidence_count. A genuine replay therefore needs
            # the identical row AND zero ACTUAL evidence/observation
            # children.
            stray_evidence = int(conn.execute(
                "SELECT COUNT(*) FROM evidence_items "
                "WHERE acquisition_session_id=?", (session_id,)
            ).fetchone()[0])
            stray_observations = int(conn.execute(
                "SELECT COUNT(*) FROM observations o "
                "JOIN evidence_items e ON e.id=o.evidence_id "
                "WHERE e.acquisition_session_id=?", (session_id,)
            ).fetchone()[0])
            if (
                tuple(stored) == (
                    session_id,
                    str(source_evidence["frozen_location_id"]),
                    mb.GOOGLE_MAPS_SOURCE_ID,
                    COLLECTOR_NAME,
                    COLLECTOR_VERSION,
                    config,
                    sha256_text(config),
                    "failed",
                    failed_at,
                    failed_at,
                    error,
                    None,
                    0,
                    0,
                )
                and stray_evidence == 0
                and stray_observations == 0
            ):
                # Genuine same-attempt replay (identical failed_at
                # second): already durably recorded with these bytes
                # and no stray children.
                conn.rollback()
                return session_id
            raise ReviewIntelligenceError(
                f"failed review session replay provenance mismatch: "
                f"{session_id}"
            )
        conn.execute(
            "INSERT INTO acquisition_sessions("
            "id,target_subject_id,source_id,collector_name,collector_version,config_json,config_hash,"
            "status,started_at,finished_at,error,legacy_run_id,evidence_count,observation_count"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                session_id,
                str(source_evidence["frozen_location_id"]),
                mb.GOOGLE_MAPS_SOURCE_ID,
                COLLECTOR_NAME,
                COLLECTOR_VERSION,
                config,
                sha256_text(config),
                "failed",
                failed_at,
                failed_at,
                error,
                None,
                0,
                0,
            ),
        )
        violations = list(conn.execute("PRAGMA foreign_key_check"))
        if violations:
            raise ReviewIntelligenceError(
                f"foreign-key violations after failed-session record: {violations!r}"
            )
        conn.commit()
    except sqlite3.IntegrityError:
        # Every constraint violation propagates: replay detection above
        # is deterministic, so nothing legitimate reaches this path.
        if conn.in_transaction:
            conn.rollback()
        raise
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise
    return session_id


def extract_retained_reviews(
    conn: sqlite3.Connection,
    *,
    business_id: int | None = None,
    canonical_key: str | None = None,
    now: Callable[[], str] = _utc_now,
) -> ReviewExtractionStats:
    """Extract customer reviews already retained in the current Maps snapshot.

    This operation performs no network access and creates no Facts. Customer
    statements remain source-time Location observations linked to immutable
    evidence. Current identity is obtained by following the Location redirect
    chain. Empty review arrays record a completed extraction attempt but do not
    create an absence fact.
    """
    if conn.in_transaction:
        raise ReviewIntelligenceError(
            "review extraction requires a connection with no active transaction"
        )
    conn.execute("PRAGMA foreign_keys=ON")
    if conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise ReviewIntelligenceError(
            "SQLite foreign-key enforcement must be enabled for review extraction"
        )

    try:
        # Freeze current Maps state, Understanding identity, retained source
        # evidence, parsing, idempotency verification, and writes under one
        # writer transaction. There is no network work inside this boundary.
        conn.execute("BEGIN IMMEDIATE")
        resolved_business_id, entity_id, canonical_location_id, business = _resolve_target(
            conn, business_id=business_id, canonical_key=canonical_key
        )
        source_evidence = _maps_source_evidence(
            conn,
            business=business,
            location_id=canonical_location_id,
        )
        try:
            raw = _parse_json_object(
                business["raw_json"], field="canonical Maps raw_json")
            reviews, source_review_records = extract_reviews(raw)
        except ReviewIntelligenceError as exc:
            # F-06: roll back the writer transaction and durably record
            # the failed attempt (its own transaction) before
            # propagating, so bounded retry can see it.
            if conn.in_transaction:
                conn.rollback()
            failed_session_id = _record_failed_extraction(
                conn, source_evidence=source_evidence, error=str(exc), now=now
            )
            raise ReviewIntelligenceError(
                f"{exc} (recorded as failed review acquisition session "
                f"{failed_session_id})"
            ) from exc
        legacy_config_json = _session_config(
            source_evidence=source_evidence,
            source_review_records=source_review_records,
            review_evidence_records=len(reviews),
        )
        # The zero-review OUTCOME is part of the session's frozen
        # configuration: enrich it before the deterministic id and the
        # idempotency verification so replays compare identical bytes.
        outcome_status = "complete" if reviews else "unavailable"
        config_with_outcome = json.loads(legacy_config_json)
        config_with_outcome["extraction_outcome"] = outcome_status
        config_json = canonical_json(config_with_outcome)
        source_location_id = str(source_evidence["frozen_location_id"])
        session_id = opaque_id(
            "acq",
            "retained-maps-reviews",
            source_evidence["id"],
            source_location_id,
            COLLECTOR_VERSION,
        )
        existing = _verify_existing(
            conn,
            session_id=session_id,
            config_json=config_json,
            business_id=resolved_business_id,
            business_entity_id=entity_id,
            canonical_location_id=canonical_location_id,
            source_evidence=source_evidence,
            reviews=reviews,
            source_review_records=source_review_records,
        )
        if existing is not None:
            conn.commit()
            return existing
        # F-05 v1 compatibility: a genuine v1 session for this snapshot
        # shares every identity input except the embedded version and
        # freezes the pre-outcome config. It must replay as-is — never
        # collide as incompatible provenance, never be re-mined into a
        # duplicate v2 session with duplicate review evidence.
        legacy_session_id = opaque_id(
            "acq",
            "retained-maps-reviews",
            source_evidence["id"],
            source_location_id,
            _LEGACY_COLLECTOR_VERSION,
        )
        legacy_existing = _verify_existing(
            conn,
            session_id=legacy_session_id,
            config_json=legacy_config_json,
            collector_version=_LEGACY_COLLECTOR_VERSION,
            business_id=resolved_business_id,
            business_entity_id=entity_id,
            canonical_location_id=canonical_location_id,
            source_evidence=source_evidence,
            reviews=reviews,
            source_review_records=source_review_records,
        )
        if legacy_existing is not None:
            conn.commit()
            return legacy_existing

        extracted_at = _validated_timestamp(now(), field="review extraction time")
        rows = _expected_review_rows(
            session_id=session_id,
            source_evidence=source_evidence,
            reviews=reviews,
            extracted_at=extracted_at,
        )
        collision = conn.execute(
            "SELECT id FROM acquisition_sessions WHERE id=?", (session_id,)
        ).fetchone()
        if collision is not None:
            raise ReviewIntelligenceError(
                f"deterministic review extraction session collision: {session_id}"
            )
        # Zero retained reviews is a distinct bounded OUTCOME, not a
        # lifecycle state: the extraction ran to completion (session
        # 'complete'), found nothing to acquire, and deliberately creates
        # no absence fact. The outcome is surfaced explicitly via the
        # stats status ('unavailable') and the session metadata rather
        # than a new session-status vocabulary value, because extending
        # the session CHECK requires an FK-off table rebuild that the
        # migration framework's FK-ON transaction forbids.
        conn.execute(
            "INSERT INTO acquisition_sessions("
            "id,target_subject_id,source_id,collector_name,collector_version,config_json,config_hash,"
            "status,started_at,finished_at,error,legacy_run_id,evidence_count,observation_count"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                session_id,
                source_location_id,
                mb.GOOGLE_MAPS_SOURCE_ID,
                COLLECTOR_NAME,
                COLLECTOR_VERSION,
                config_json,
                sha256_text(config_json),
                "complete",
                extracted_at,
                extracted_at,
                None,
                None,
                len(rows),
                len(rows),
            ),
        )
        for evidence, observation in rows:
            conn.execute(
                "INSERT INTO evidence_items("
                "id,acquisition_session_id,source_id,source_locator,source_role,status,retrieved_at,"
                "published_at,language,media_type,content_sha256,artifact_ref,metadata_json,created_at"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                tuple(evidence[key] for key in (
                    "id",
                    "acquisition_session_id",
                    "source_id",
                    "source_locator",
                    "source_role",
                    "status",
                    "retrieved_at",
                    "published_at",
                    "language",
                    "media_type",
                    "content_sha256",
                    "artifact_ref",
                    "metadata_json",
                    "created_at",
                )),
            )
            conn.execute(
                "INSERT INTO observations("
                "id,subject_id,predicate,evidence_id,value_json,normalized_value_json,value_hash,"
                "observation_kind,observed_at,extracted_at,extraction_method,extractor_name,"
                "extractor_version,confidence,created_at"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                tuple(observation[key] for key in (
                    "id",
                    "subject_id",
                    "predicate",
                    "evidence_id",
                    "value_json",
                    "normalized_value_json",
                    "value_hash",
                    "observation_kind",
                    "observed_at",
                    "extracted_at",
                    "extraction_method",
                    "extractor_name",
                    "extractor_version",
                    "confidence",
                    "created_at",
                )),
            )
        violations = list(conn.execute("PRAGMA foreign_key_check"))
        if violations:
            raise ReviewIntelligenceError(
                f"foreign-key violations after review extraction: {violations!r}"
            )
        stats = _stats(
            session_id=session_id,
            business_id=resolved_business_id,
            business_entity_id=entity_id,
            canonical_location_id=canonical_location_id,
            source_evidence=source_evidence,
            source_review_records=source_review_records,
            reviews=reviews,
            evidence_items_created=len(reviews),
            observations_created=len(reviews),
            already_extracted=False,
            status=outcome_status,
        )
        conn.commit()
        return stats
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise


def extract_retained_reviews_for_entity(
    conn: sqlite3.Connection,
    *,
    entity_id: str,
    now: Callable[[], str] = _utc_now,
) -> dict[str, Any]:
    """Extract retained reviews for EVERY Maps business of one entity.

    F-04 deterministic executor target: the planner schedules review
    extraction at Entity scope, so the extraction semantics are
    Entity-scoped too — a multi-location entity owns several
    Maps-backed review corpora. Businesses run in ascending business-id
    order, each keeping its own per-snapshot identity and idempotency.
    A target with no currently extractable snapshot is skipped with an
    explicit reason instead of aborting the remaining locations;
    parse-class failures propagate after their durable failed-session
    record (F-06).
    """
    if conn.in_transaction:
        raise ReviewIntelligenceError(
            "review extraction requires a connection with no active transaction"
        )
    conn.execute("PRAGMA foreign_keys=ON")
    if conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise ReviewIntelligenceError(
            "SQLite foreign-key enforcement must be enabled for review extraction"
        )
    try:
        verify_business_understanding_vocabulary(conn)
    except VocabularySeedError as exc:
        raise ReviewIntelligenceError(str(exc)) from exc
    try:
        canonical_entity_id, _selection = resolve_selection(
            conn,
            business_id=None,
            canonical_key=None,
            entity_id=str(entity_id).strip(),
        )
    except DossierQueryError as exc:
        raise ReviewIntelligenceError(str(exc)) from exc
    _location_rows, current_location_ids = dossier_locations(
        conn, canonical_entity_id
    )
    businesses = dossier_maps_businesses(conn, current_location_ids)
    results: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for business in sorted(businesses, key=lambda item: int(item["id"])):
        business_id = int(business["id"])
        try:
            stats = extract_retained_reviews(
                conn, business_id=business_id, now=now
            )
        except ReviewTargetUnavailableError as exc:
            skipped.append({"business_id": business_id, "reason": str(exc)})
            continue
        results.append(asdict(stats))
    return {
        "entity_id": canonical_entity_id,
        "businesses": results,
        "skipped": skipped,
        "business_count": len(results),
        "skipped_count": len(skipped),
    }


_SUPPORTED_REVIEW_VERSIONS = frozenset({"1", "2"})

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


def _is_canonical_review_config(config: dict[str, Any]) -> bool:
    """The canonical review-config shape: exact key set, typed identity
    fields, and non-negative declared counts with deduplicated evidence
    never exceeding source records (R11-01)."""
    if set(config) != _LEGACY_V1_CONFIG_KEYS:
        return False
    if config["input_kind"] != "retained_maps_review_snapshot":
        return False
    if config["review_array_fields"] != list(REVIEW_ARRAY_FIELDS):
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
    for key in ("source_review_records", "review_evidence_records"):
        value = config[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return False
    if config["review_evidence_records"] > config["source_review_records"]:
        return False
    return True


def _is_strict_zero_review_config(config: dict[str, Any]) -> bool:
    """The canonical config shape with zero declared review records.

    Both the rigorous v1 inference and the explicit v2 outcome share
    this shape: a v2 zero-review config is the legacy shape plus the
    frozen extraction_outcome key.
    """
    return (
        _is_canonical_review_config(config)
        and config["source_review_records"] == 0
        and config["review_evidence_records"] == 0
    )


def current_maps_evidence_for_business(
    conn: sqlite3.Connection, *, business_id: int
) -> dict[str, Any] | None:
    """The executor's exact current retained Maps snapshot (R10-01).

    This IS the review executor's provenance contract — selection,
    run binding (current last_run_id), expected Maps collector and
    version, snapshot metadata, raw content hash, retrieval timestamp,
    and frozen subject anchors — factored out so the planner and the
    outcome projection resolve "the current snapshot" exactly the way
    extraction does. Returns None when the business has no currently
    extractable snapshot; integrity-class failures propagate.
    """
    try:
        _bid, _eid, canonical_location_id, business = _resolve_target(
            conn, business_id=business_id, canonical_key=None
        )
        return _maps_source_evidence(
            conn, business=business, location_id=canonical_location_id
        )
    except ReviewTargetUnavailableError:
        return None


def strict_unavailable_outcome_session(
    conn: sqlite3.Connection, session: dict[str, Any]
) -> tuple[dict[str, Any] | None, str | None]:
    """Strict unavailable-session verifier (R10-03/R10-04).

    Given a COMPLETE sara.reviews.maps_snapshot session row, return
    (outcome, issue_code): exactly one is non-None except the silent
    skips — a session that produced review evidence, or whose outcome
    is complete/failed, projects nothing and reports nothing.

    An unavailable outcome is projected ONLY when the session proves
    the FULL contract: supported review collector version; canonical
    config bytes and hash; the deterministic review-session id; the
    session target equals the frozen source Location; zero stored AND
    actual review output; and the frozen business/entity/location/run
    binding resolves — via the executor's own current-snapshot
    resolver — to the business's CURRENT retained Maps snapshot. A
    superseded snapshot therefore drops its outcome (R10-04), and
    unknown/future review versions never contribute mandatory-domain
    evidence.
    """
    session_id = str(session["id"])
    invalid = (None, "review_outcome_session_config_invalid")
    mismatch = (None, "review_outcome_source_evidence_mismatch")
    try:
        config = json.loads(str(session["config_json"]))
        if not isinstance(config, dict):
            raise ValueError("config is not a JSON object")
    except ValueError:
        return invalid
    version = str(session["collector_version"])
    outcome_key = config.get("extraction_outcome")
    if outcome_key is None:
        if int(session["evidence_count"] or 0) != 0:
            return (None, None)  # historical with-reviews session
        # R9-03: rigorous v1 inference only.
        if version != "1" or not _is_strict_zero_review_config(config):
            return invalid
    elif outcome_key == "unavailable":
        # R10-03: the explicit outcome carries the SAME strictness.
        if version != "2":
            return invalid
        stripped = {
            key: value for key, value in config.items()
            if key != "extraction_outcome"
        }
        if not _is_strict_zero_review_config(stripped):
            return invalid
    else:
        return (None, None)  # complete/failed outcomes project nothing
    if str(session["collector_name"]) != COLLECTOR_NAME:
        return invalid
    if version not in _SUPPORTED_REVIEW_VERSIONS:
        return invalid
    # R11-02: the review session's own source binding — the executor
    # replay contract expects src_google_maps; a deterministic-looking
    # session under any other registered source is invalid.
    if str(session.get("source_id")) != mb.GOOGLE_MAPS_SOURCE_ID:
        return invalid
    config_json = str(session["config_json"])
    if canonical_json(config) != config_json:
        return invalid
    if sha256_text(config_json) != str(session["config_hash"]):
        return invalid
    expected_id = opaque_id(
        "acq",
        "retained-maps-reviews",
        config["source_evidence_id"],
        config["source_location_id"],
        version,
    )
    if session_id != expected_id:
        return invalid
    if str(session["target_subject_id"]) != config["source_location_id"]:
        return invalid
    if int(session["evidence_count"] or 0) != 0:
        return invalid
    if int(session["observation_count"] or 0) != 0:
        return invalid
    actual_evidence = int(conn.execute(
        "SELECT COUNT(*) FROM evidence_items "
        "WHERE acquisition_session_id=?", (session_id,)
    ).fetchone()[0])
    actual_observations = int(conn.execute(
        "SELECT COUNT(*) FROM observations o "
        "JOIN evidence_items e ON e.id=o.evidence_id "
        "WHERE e.acquisition_session_id=?", (session_id,)
    ).fetchone()[0])
    if actual_evidence != 0 or actual_observations != 0:
        return invalid
    finished_at = session["finished_at"]
    if not isinstance(finished_at, str) or not finished_at:
        return invalid
    try:
        _validated_timestamp(finished_at, field="review outcome finished_at")
    except ReviewIntelligenceError:
        return invalid
    # R11-03: validate the frozen HISTORICAL parent independently
    # FIRST — only a legitimate old parent may later be classified as
    # ordinary supersession. An existing but malformed historical
    # parent is an integrity issue, never hidden lifecycle.
    referenced = conn.execute(
        "SELECT 1 FROM evidence_items WHERE id=?",
        (config["source_evidence_id"],),
    ).fetchone()
    if referenced is None:
        return invalid  # fabricated parent reference
    frozen_parent = _validate_frozen_maps_parent(conn, config)
    if frozen_parent is None:
        return mismatch  # existing historical parent fails provenance
    # R10-04: the outcome binds to the business's CURRENT snapshot.
    try:
        parent = current_maps_evidence_for_business(
            conn, business_id=int(config["source_maps_business_id"]))
    except ReviewIntelligenceError:
        return mismatch
    if parent is None or str(parent["id"]) != config["source_evidence_id"]:
        # A VALID old parent that is simply no longer current: ordinary
        # supersession — drop silently so the lifecycle can proceed.
        return (None, None)
    retrieved = frozen_parent["retrieved_at"]
    return (
        {
            "session_id": session_id,
            "target_subject_id": str(session["target_subject_id"]),
            "collector_version": version,
            "finished_at": finished_at,
            "source_evidence_id": config["source_evidence_id"],
            "source_content_sha256": config["source_content_sha256"],
            "source_location_id": config["source_location_id"],
            "source_retrieved_at": str(retrieved),
        },
        None,
    )


def review_session_mined_evidence(
    conn: sqlite3.Connection, session: dict[str, Any]
) -> str | None:
    """The exact evidence id a COMPLETE review session legitimately
    mined (R11-01), or None when the session fails the executor's
    provenance contract.

    The planner's coverage check uses ONLY verified sessions: a
    malformed zero-output marker must never suppress the real
    acquisition. Contract: review collector name and a supported
    version; source binding to Google Maps; canonical config bytes
    and hash; the deterministic review-session id; the session target
    equal to the frozen source Location; declared counts matching the
    stored counters AND the actual evidence/observation rows; and
    output semantics consistent with the declared outcome (zero
    output is valid only as unavailable).
    """
    session_id = str(session["id"])
    # R13-01: the verifier no longer assumes a status-filtered caller.
    # It proves the executor's lifecycle contract itself: status
    # 'complete', no error, both lifecycle timestamps present, and a
    # parseable finish (it stamps the expected children). A
    # non-complete or lifecycle-invalid occupant at the deterministic
    # id is INVALID state, never coverage.
    if str(session.get("status")) != "complete":
        return None
    if session.get("error") is not None:
        return None
    started_at = session.get("started_at")
    finished_at = session.get("finished_at")
    if not isinstance(started_at, str) or not started_at:
        return None
    if not isinstance(finished_at, str) or not finished_at:
        return None
    try:
        _validated_timestamp(finished_at, field="review session finished_at")
    except ReviewIntelligenceError:
        return None
    if str(session["collector_name"]) != COLLECTOR_NAME:
        return None
    version = str(session["collector_version"])
    if version not in _SUPPORTED_REVIEW_VERSIONS:
        return None
    if str(session["source_id"]) != mb.GOOGLE_MAPS_SOURCE_ID:
        return None
    try:
        config = json.loads(str(session["config_json"]))
        if not isinstance(config, dict):
            raise ValueError("config is not a JSON object")
    except ValueError:
        return None
    outcome = config.get("extraction_outcome")
    if outcome is None:
        if version != "1":
            return None
        stripped = config
    elif outcome in ("complete", "unavailable"):
        if version != "2":
            return None
        stripped = {
            key: value for key, value in config.items()
            if key != "extraction_outcome"
        }
    else:
        return None
    if not _is_canonical_review_config(stripped):
        return None
    config_json = str(session["config_json"])
    if canonical_json(config) != config_json:
        return None
    if sha256_text(config_json) != str(session["config_hash"]):
        return None
    expected_id = opaque_id(
        "acq",
        "retained-maps-reviews",
        config["source_evidence_id"],
        config["source_location_id"],
        version,
    )
    if session_id != expected_id:
        return None
    if str(session["target_subject_id"]) != config["source_location_id"]:
        return None
    declared = int(config["review_evidence_records"])
    if int(session["evidence_count"] or 0) != declared:
        return None
    if int(session["observation_count"] or 0) != declared:
        return None
    actual_evidence = int(conn.execute(
        "SELECT COUNT(*) FROM evidence_items "
        "WHERE acquisition_session_id=?", (session_id,)
    ).fetchone()[0])
    if actual_evidence != declared:
        return None
    actual_observations = int(conn.execute(
        "SELECT COUNT(*) FROM observations o "
        "JOIN evidence_items e ON e.id=o.evidence_id "
        "WHERE e.acquisition_session_id=?", (session_id,)
    ).fetchone()[0])
    if actual_observations != declared:
        return None
    if outcome == "complete" and declared == 0:
        return None  # zero-output complete is semantically invalid
    if outcome == "unavailable" and declared != 0:
        return None
    # R12-02: the executor's FULL child provenance contract — rebuild
    # the expected evidence/observation rows from the parent snapshot
    # and compare the stored rows byte for byte, exactly as replay
    # verification does. Same counts with drifted content are not
    # coverage.
    parent_row = conn.execute(
        "SELECT content_sha256,retrieved_at,source_locator,artifact_ref,"
        "metadata_json FROM evidence_items WHERE id=?",
        (config["source_evidence_id"],),
    ).fetchone()
    if parent_row is None:
        return None
    (parent_sha, parent_retrieved, parent_locator,
     parent_artifact_ref, parent_metadata_json) = parent_row
    if str(parent_sha) != config["source_content_sha256"]:
        return None
    if not isinstance(parent_retrieved, str) or not parent_retrieved:
        return None
    try:
        parent_metadata = json.loads(str(parent_metadata_json))
        if not isinstance(parent_metadata, dict):
            raise ValueError("metadata is not a JSON object")
    except ValueError:
        return None
    raw_snapshot = parent_metadata.get("raw_json")
    if not isinstance(raw_snapshot, str) or not raw_snapshot:
        return None
    if sha256_text(raw_snapshot) != str(parent_sha):
        return None
    try:
        reviews, source_review_records = extract_reviews(
            json.loads(raw_snapshot))
    except (ReviewIntelligenceError, ValueError):
        return None
    if source_review_records != int(config["source_review_records"]):
        return None
    if len(reviews) != declared:
        return None
    frozen_source_evidence = {
        "id": config["source_evidence_id"],
        "content_sha256": config["source_content_sha256"],
        "frozen_entity_id": config["source_business_entity_id"],
        "frozen_location_id": config["source_location_id"],
        "retrieved_at": parent_retrieved,
        "source_locator": parent_locator,
        "artifact_ref": parent_artifact_ref,
        "metadata": parent_metadata,
    }
    expected_rows = _expected_review_rows(
        session_id=session_id,
        source_evidence=frozen_source_evidence,
        reviews=reviews,
        extracted_at=str(session["finished_at"]),
        extractor_version=version,
    )
    try:
        _compare_stored_children(
            conn, session_id=session_id, expected_rows=expected_rows)
    except ReviewIntelligenceError:
        return None
    return config["source_evidence_id"]


_MAPS_PARENT_KINDS = {
    "sara.maps_backfill": "legacy_maps_business_snapshot",
    "sara.maps_sync": "maps_sync_snapshot",
}


def _validate_frozen_maps_parent(
    conn: sqlite3.Connection, config: dict[str, Any]
) -> dict[str, Any] | None:
    """Validate the config's frozen HISTORICAL Maps parent on its own
    terms (R11-03), independent of any current business state.

    Returns the validated parent facts (retrieved_at) or None. The
    contract: the referenced evidence exists; Google Maps source,
    platform role, usable status; a complete Maps collector session
    of the expected version; snapshot metadata matching the frozen
    business (legacy) or entity/location (sync) identity; the frozen
    content hash agreeing with the metadata's raw snapshot; and a
    parseable retrieval timestamp.

    R12-03: the validator also reproduces the Maps producer's
    identity — the parent session's source binding and run binding,
    the legacy canonical-key binding, and the DETERMINISTIC evidence
    id (backfill id over business+content hash; sync id over
    business+run+content hash) — before supersession may be silent.

    R13-02: the producer contract now also covers the parent
    ACQUISITION SESSION itself — deterministic session id from the
    run, NULL target, canonical import-mode config with hash,
    run-row-equal lifecycle timestamps, no error, stored counts
    matching actual children — and the legacy business/canonical-key
    bindings apply to BOTH kinds, exactly as the Maps sync verifier
    applies them to all snapshot evidence.
    """
    row = conn.execute(
        "SELECT e.content_sha256,e.source_id,e.source_role,e.status,"
        "e.retrieved_at,a.id AS parent_session_id,"
        "a.target_subject_id AS parent_target_subject_id,"
        "a.collector_name,a.collector_version,"
        "a.status AS session_status,a.source_id AS parent_source_id,"
        "a.legacy_run_id AS parent_legacy_run_id,"
        "a.config_json AS parent_config_json,"
        "a.config_hash AS parent_config_hash,"
        "a.started_at AS parent_started_at,"
        "a.finished_at AS parent_finished_at,"
        "a.error AS parent_error,"
        "a.evidence_count AS parent_evidence_count,"
        "a.observation_count AS parent_observation_count,"
        "e.metadata_json "
        "FROM evidence_items e "
        "JOIN acquisition_sessions a ON a.id=e.acquisition_session_id "
        "WHERE e.id=?",
        (config["source_evidence_id"],),
    ).fetchone()
    if row is None:
        return None
    (content_sha, source_id, role, status, retrieved_at,
     parent_session_id, parent_target_subject_id, collector,
     collector_version, session_status, parent_source_id,
     parent_legacy_run_id, parent_config_json, parent_config_hash,
     parent_started_at, parent_finished_at, parent_error,
     parent_evidence_count, parent_observation_count,
     metadata_json) = row
    if str(source_id) != mb.GOOGLE_MAPS_SOURCE_ID:
        return None
    # R12-03: the parent acquisition session's own source binding.
    if str(parent_source_id) != mb.GOOGLE_MAPS_SOURCE_ID:
        return None
    if str(role) != "platform" or str(status) != "usable":
        return None
    if str(session_status) != "complete":
        return None
    if str(content_sha) != config["source_content_sha256"]:
        return None
    kind = _MAPS_PARENT_KINDS.get(str(collector))
    if kind is None:
        return None
    try:
        metadata = json.loads(str(metadata_json))
        if not isinstance(metadata, dict):
            raise ValueError("metadata is not a JSON object")
    except ValueError:
        return None
    if metadata.get("import_kind") != kind:
        return None
    from ..maps_backfill import BACKFILL_VERSION
    from ..maps_sync import SYNC_VERSION

    expected_version = (
        BACKFILL_VERSION
        if str(collector) == "sara.maps_backfill"
        else SYNC_VERSION
    )
    if str(collector_version) != str(expected_version):
        return None
    raw_snapshot = metadata.get("raw_json")
    if not isinstance(raw_snapshot, str) or not raw_snapshot:
        return None
    if sha256_text(raw_snapshot) != str(content_sha):
        return None
    # R12-03: the parent session's run binding must agree with the
    # snapshot metadata's run before any producer identity check.
    if not parent_legacy_run_id:
        return None
    if str(metadata.get("legacy_run_id") or "") != str(parent_legacy_run_id):
        return None
    # R13-02: the legacy business and canonical-key bindings are COMMON
    # to both producer kinds — the Maps sync verifier applies them to
    # every snapshot evidence row, so a sync-kind parent with a wrong
    # legacy_business_id or legacy_canonical_key is not legitimate.
    business_id = int(config["source_maps_business_id"])
    if metadata.get("legacy_business_id") != business_id:
        return None
    business_row = conn.execute(
        "SELECT canonical_key FROM businesses WHERE id=?",
        (business_id,),
    ).fetchone()
    if business_row is None:
        return None
    if metadata.get("legacy_canonical_key") != business_row[0]:
        return None
    # R13-02: reproduce the Maps producer's deterministic
    # ACQUISITION-SESSION identity, config, hash, and lifecycle
    # contract (mirroring maps_sync._ensure_session): the session id
    # derived from the legacy run, NULL target, the canonical
    # import-mode config for the collector kind with its hash,
    # timestamps equal to the legacy run row's, no error, and stored
    # counts matching actual children.
    run_row = conn.execute(
        "SELECT started_at, finished_at FROM runs WHERE id=?",
        (str(parent_legacy_run_id),),
    ).fetchone()
    if run_row is None:
        return None
    if str(parent_session_id) != mb._acquisition_id(str(parent_legacy_run_id)):
        return None
    if parent_target_subject_id is not None:
        return None
    if parent_error not in (None, ""):
        return None
    if str(parent_started_at) != str(run_row[0]):
        return None
    if str(parent_finished_at) != str(run_row[1]):
        return None
    expected_import_mode = (
        "latest_canonical_maps_snapshot"
        if kind == "legacy_maps_business_snapshot"
        else "maps_sync_current_snapshot"
    )
    expected_config = canonical_json({
        "import_mode": expected_import_mode,
        "legacy_run_id": str(parent_legacy_run_id),
    })
    if str(parent_config_json) != expected_config:
        return None
    if str(parent_config_hash) != sha256_text(expected_config):
        return None
    actual_parent_evidence = int(conn.execute(
        "SELECT COUNT(*) FROM evidence_items "
        "WHERE acquisition_session_id=?",
        (str(parent_session_id),),
    ).fetchone()[0])
    if int(parent_evidence_count or 0) != actual_parent_evidence:
        return None
    actual_parent_observations = int(conn.execute(
        "SELECT COUNT(*) FROM observations o "
        "JOIN evidence_items e ON e.id=o.evidence_id "
        "WHERE e.acquisition_session_id=?",
        (str(parent_session_id),),
    ).fetchone()[0])
    if int(parent_observation_count or 0) != actual_parent_observations:
        return None
    if kind == "legacy_maps_business_snapshot":
        if (
                config["source_business_entity_id"]
            != mb.business_entity_id_for_maps_business(business_id)
        ):
            return None
        if (
                config["source_location_id"]
            != mb.location_id_for_maps_business(business_id)
        ):
            return None
        # R12-03: reproduce the producer's deterministic evidence id —
        # a fabricated row under an arbitrary id with otherwise
        # plausible metadata is not a legitimate historical parent.
        if (
            config["source_evidence_id"]
            != mb._evidence_id(business_id, str(content_sha))
        ):
            return None
    else:
        if metadata.get("sync_entity_id") != config["source_business_entity_id"]:
            return None
        if metadata.get("sync_location_id") != config["source_location_id"]:
            return None
        if (
            config["source_evidence_id"]
            != ms._sync_evidence_id(
                int(config["source_maps_business_id"]),
                str(parent_legacy_run_id),
                str(content_sha),
            )
        ):
            return None
    if not isinstance(retrieved_at, str) or not retrieved_at:
        return None
    try:
        _validated_timestamp(retrieved_at, field="Maps parent retrieved_at")
    except ReviewIntelligenceError:
        return None
    return {"retrieved_at": str(retrieved_at)}


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sara-reviews",
        description=(
            "Extract already-retained Google Maps customer review evidence into "
            "Business Understanding without network access."
        ),
    )
    parser.add_argument("--db", default="data/sara.db", help="SQLite database path")
    selector = parser.add_mutually_exclusive_group(required=True)
    selector.add_argument("--business-id", type=_positive_int)
    selector.add_argument("--canonical-key")
    selector.add_argument("--entity-id")
    parser.add_argument("--pretty", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    conn: sqlite3.Connection | None = None
    try:
        conn = connect_existing(Path(args.db))
        apply_migrations(conn)
        seed_business_understanding_vocabulary(conn)
        if args.entity_id is not None:
            payload = extract_retained_reviews_for_entity(
                conn, entity_id=args.entity_id
            )
        else:
            payload = asdict(
                extract_retained_reviews(
                    conn,
                    business_id=args.business_id,
                    canonical_key=args.canonical_key,
                )
            )
        options = {"ensure_ascii": False, "sort_keys": True}
        if args.pretty:
            print(json.dumps(payload, indent=2, **options))
        else:
            print(json.dumps(payload, separators=(",", ":"), **options))
        return 0
    except KeyboardInterrupt:
        print("review extraction interrupted", file=sys.stderr)
        return 130
    except (
        FileNotFoundError,
        MigrationError,
        VocabularySeedError,
        ReviewIntelligenceError,
        sqlite3.Error,
        ValueError,
    ) as exc:
        print(f"review extraction failed: {exc}", file=sys.stderr)
        return 2
    finally:
        if conn is not None:
            conn.close()