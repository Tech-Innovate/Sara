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
from ..dossier.core import DossierQueryError, resolve_selection, resolve_subject
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
        raise ReviewIntelligenceError(f"canonical Maps business {business_id} does not exist")
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
    except (VocabularySeedError, DossierQueryError) as exc:
        raise ReviewIntelligenceError(str(exc)) from exc
    maps_business = selection.get("maps_business")
    if not isinstance(maps_business, dict) or not isinstance(maps_business.get("id"), int):
        raise ReviewIntelligenceError("review extraction requires a current Maps business selector")
    location_id = selection.get("canonical_location_id")
    if not isinstance(location_id, str) or not location_id:
        raise ReviewIntelligenceError("selected Maps business has no current Understanding location")
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
        raise ReviewIntelligenceError(
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
            "extractor_version": COLLECTOR_VERSION,
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


def _verify_existing(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    config_json: str,
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
        "collector_version": COLLECTOR_VERSION,
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
    )
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
        raw = _parse_json_object(business["raw_json"], field="canonical Maps raw_json")
        reviews, source_review_records = extract_reviews(raw)
        config_json = _session_config(
            source_evidence=source_evidence,
            source_review_records=source_review_records,
            review_evidence_records=len(reviews),
        )
        # The zero-review OUTCOME is part of the session's frozen
        # configuration: enrich it before the deterministic id and the
        # idempotency verification so replays compare identical bytes.
        outcome_status = "complete" if reviews else "unavailable"
        config_with_outcome = json.loads(config_json)
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
    parser.add_argument("--pretty", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    conn: sqlite3.Connection | None = None
    try:
        conn = connect_existing(Path(args.db))
        apply_migrations(conn)
        seed_business_understanding_vocabulary(conn)
        stats = extract_retained_reviews(
            conn,
            business_id=args.business_id,
            canonical_key=args.canonical_key,
        )
        options = {"ensure_ascii": False, "sort_keys": True}
        payload = asdict(stats)
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