from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from sara.maps_backfill import backfill_maps_business_understanding, business_entity_id_for_maps_business
from sara.migrations import apply_migrations
from sara.reviews import (
    ReviewIntelligenceError,
    extract_retained_reviews,
    extract_retained_reviews_for_entity,
)
from sara.storage import connect, ingest_records
from sara.understanding_vocabulary import seed_business_understanding_vocabulary


def prepared(path: Path, *, reviews: list[dict] | None = None):
    conn = connect(path)
    assert apply_migrations(conn) == (1, 2, 3, 4)
    seed_business_understanding_vocabulary(conn)
    conn.execute(
        "INSERT INTO runs(id,area_name,bbox_json,cell_km,depth,queries_json,scraper_image,"
        "config_json,raw_path,status,started_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("r1", "test", '{"max_lat":22,"max_lon":40,"min_lat":21,"min_lon":39}',
         2.0, 1, '["restaurant"]', "gosom/google-maps-scraper:v1.18.1",
         '{"strict_bounds":true}', "/evidence/r1.jsonl", "running",
         "2026-09-25T10:00:00+00:00"),
    )
    conn.commit()
    record = {
        "place_id": "place-reviews", "cid": "cid-reviews", "data_id": "data-reviews",
        "title": "Review Business", "category": "Restaurant", "address": "Review Street",
        "latitude": 21.55, "longitude": 39.18, "phone": "+966500000000",
        "website": "https://reviews.example", "review_rating": 4.4, "review_count": 120,
        "status": "Open", "link": "https://maps.example/reviews",
    }
    if reviews is not None:
        record["user_reviews"] = reviews
    with patch("sara.storage.utc_now", return_value="2026-09-26T09:59:00+00:00"), patch(
        "sara.maps_backfill._utc_now", return_value="2026-09-26T10:00:00+00:00"
    ):
        ingest_records(conn, "r1", [record], finalize_run=("complete", 0, None))
        backfill_maps_business_understanding(conn)
    return conn


REVIEW = {
    "review_id": "rev-1", "source": "Google", "Rating": 5,
    "Description": "Excellent service",
    "language": "en", "posted_at_unix_micros": 1_758_758_400_000_000,
    "Name": "Reviewer", "ProfilePicture": "https://profiles.example/p.jpg",
}


def test_zero_reviews_record_unavailable_not_complete(tmp_path: Path) -> None:
    """No retained reviews => session status 'unavailable', no absence fact."""
    conn = prepared(tmp_path / "zero.sqlite", reviews=[])
    stats = extract_retained_reviews(conn, business_id=1)
    assert stats.status == "unavailable"
    # The session lifecycle ran to completion; the zero-review OUTCOME is
    # explicit in the frozen session configuration, not a new status value.
    cfg = json.loads(conn.execute(
        "SELECT config_json FROM acquisition_sessions WHERE id=?", (stats.session_id,)
    ).fetchone()[0])
    assert cfg["extraction_outcome"] == "unavailable"
    # No absence fact was created for the empty review array.
    absent = conn.execute(
        "SELECT COUNT(*) FROM facts WHERE predicate LIKE 'reputation%' "
        "AND status='not_observed' AND valid_to IS NULL"
    ).fetchone()[0]
    assert absent == 0
    conn.close()


def test_zero_reviews_outcome_is_idempotent(tmp_path: Path) -> None:
    """Re-extracting the same empty snapshot replays the same session."""
    conn = prepared(tmp_path / "zero-replay.sqlite", reviews=[])
    first = extract_retained_reviews(conn, business_id=1)
    second = extract_retained_reviews(conn, business_id=1)
    assert second.session_id == first.session_id
    assert second.already_extracted is True
    assert second.status == "unavailable"
    n = conn.execute(
        "SELECT COUNT(*) FROM acquisition_sessions WHERE id=?", (first.session_id,)
    ).fetchone()[0]
    assert n == 1
    conn.close()


def test_reviews_present_outcome_complete(tmp_path: Path) -> None:
    """Retained reviews extract to 'complete' with evidence + observations."""
    conn = prepared(tmp_path / "one.sqlite", reviews=[REVIEW])
    stats = extract_retained_reviews(conn, business_id=1)
    assert stats.status == "complete"
    assert stats.evidence_items_created == 1
    assert stats.observations_created == 1
    row = conn.execute(
        "SELECT status, evidence_count, observation_count FROM acquisition_sessions "
        "WHERE id=?", (stats.session_id,)).fetchone()
    assert row[0] == "complete"
    # Reviews stay observations, never facts.
    facts = conn.execute(
        "SELECT COUNT(*) FROM facts WHERE predicate='reputation.customer_review'"
    ).fetchone()[0]
    assert facts == 0
    conn.close()


def _seed_legacy_v1_session(conn, *, drift: bool = False) -> tuple[str, int]:
    """Freeze a genuine pre-PR21 v1 extraction session with production builders.

    ``drift=True`` freezes a config that disagrees with the snapshot (as if
    written by a tampered producer); sessions are append-only, so the drift
    must be baked in at insert time, never UPDATEd afterwards.
    """
    from sara.reviews import core as reviews_core
    from sara.reviews.model import canonical_json as rj
    from sara.reviews.model import opaque_id as review_opaque_id, sha256_text

    _bid, _eid, canonical_location_id, business = reviews_core._resolve_target(
        conn, business_id=1, canonical_key=None)
    source_evidence = reviews_core._maps_source_evidence(
        conn, business=business, location_id=canonical_location_id)
    raw = json.loads(business["raw_json"])
    reviews, source_review_records = reviews_core.extract_reviews(raw)
    legacy_config = reviews_core._session_config(
        source_evidence=source_evidence,
        source_review_records=source_review_records,
        review_evidence_records=len(reviews),
    )
    if drift:
        drifted = json.loads(legacy_config)
        drifted["source_review_records"] = drifted["source_review_records"] + 1
        legacy_config = rj(drifted)
    session_id = review_opaque_id(
        "acq", "retained-maps-reviews", source_evidence["id"],
        str(source_evidence["frozen_location_id"]), "1")
    extracted_at = "2026-09-26T11:00:00+00:00"
    rows = reviews_core._expected_review_rows(
        session_id=session_id, source_evidence=source_evidence,
        reviews=reviews, extracted_at=extracted_at, extractor_version="1")
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "INSERT INTO acquisition_sessions("
        "id,target_subject_id,source_id,collector_name,collector_version,config_json,config_hash,"
        "status,started_at,finished_at,error,legacy_run_id,evidence_count,observation_count"
        ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (session_id, str(source_evidence["frozen_location_id"]),
         "src_google_maps", "sara.reviews.maps_snapshot", "1",
         legacy_config, sha256_text(legacy_config), "complete",
         extracted_at, extracted_at, None, None, len(rows), len(rows)))
    for evidence, observation in rows:
        conn.execute(
            "INSERT INTO evidence_items("
            "id,acquisition_session_id,source_id,source_locator,source_role,status,retrieved_at,"
            "published_at,language,media_type,content_sha256,artifact_ref,metadata_json,created_at"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            tuple(evidence[key] for key in (
                "id", "acquisition_session_id", "source_id", "source_locator",
                "source_role", "status", "retrieved_at", "published_at",
                "language", "media_type", "content_sha256", "artifact_ref",
                "metadata_json", "created_at")))
        conn.execute(
            "INSERT INTO observations("
            "id,subject_id,predicate,evidence_id,value_json,normalized_value_json,value_hash,"
            "observation_kind,observed_at,extracted_at,extraction_method,extractor_name,"
            "extractor_version,confidence,created_at"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            tuple(observation[key] for key in (
                "id", "subject_id", "predicate", "evidence_id", "value_json",
                "normalized_value_json", "value_hash", "observation_kind",
                "observed_at", "extracted_at", "extraction_method",
                "extractor_name", "extractor_version", "confidence",
                "created_at")))
    conn.commit()
    return session_id, len(rows)


def test_legacy_v1_session_replays_without_duplicate_evidence(tmp_path: Path) -> None:
    """F-05: a genuine pre-outcome v1 session replays; no v2 duplicate."""
    conn = prepared(tmp_path / "legacy-v1.sqlite", reviews=[REVIEW])
    session_id, review_rows = _seed_legacy_v1_session(conn)
    stats = extract_retained_reviews(
        conn, business_id=1, now=lambda: "2026-09-26T12:00:00+00:00")
    assert stats.already_extracted is True
    assert stats.session_id == session_id
    assert stats.status == "complete"
    n_sessions = conn.execute(
        "SELECT COUNT(*) FROM acquisition_sessions "
        "WHERE collector_name='sara.reviews.maps_snapshot'").fetchone()[0]
    assert n_sessions == 1
    n_evidence = conn.execute(
        "SELECT COUNT(*) FROM evidence_items WHERE acquisition_session_id=?",
        (session_id,)).fetchone()[0]
    assert n_evidence == review_rows
    conn.close()


def test_legacy_v1_session_with_drifted_config_fails_closed(tmp_path: Path) -> None:
    """F-05: a v1 row whose config disagrees with the snapshot is refused."""
    conn = prepared(tmp_path / "legacy-drift.sqlite", reviews=[REVIEW])
    session_id, _rows = _seed_legacy_v1_session(conn, drift=True)
    with pytest.raises(ReviewIntelligenceError, match="incompatible provenance"):
        extract_retained_reviews(
            conn, business_id=1, now=lambda: "2026-09-26T12:00:00+00:00")
    conn.close()


def test_malformed_reviews_record_durable_failed_session(tmp_path: Path) -> None:
    """F-06: deterministic parse failure persists failed acquisition state."""
    conn = prepared(tmp_path / "malformed.sqlite", reviews="not-a-list")
    with pytest.raises(ReviewIntelligenceError, match="array"):
        extract_retained_reviews(
            conn, business_id=1, now=lambda: "2026-09-26T12:00:00+00:00")
    row = conn.execute(
        "SELECT status,error FROM acquisition_sessions "
        "WHERE collector_name='sara.reviews.maps_snapshot' AND status='failed'"
    ).fetchone()
    assert row is not None
    assert "array" in row[1]
    # A second attempt records a SECOND durable failure (per-attempt ids
    # drive the collector-scoped retry ceiling).
    with pytest.raises(ReviewIntelligenceError):
        extract_retained_reviews(
            conn, business_id=1, now=lambda: "2026-09-26T12:10:00+00:00")
    n = conn.execute(
        "SELECT COUNT(*) FROM acquisition_sessions "
        "WHERE status='failed' AND collector_name='sara.reviews.maps_snapshot'"
    ).fetchone()[0]
    assert n == 2
    conn.close()


def test_entity_scoped_extraction_processes_and_skips(tmp_path: Path) -> None:
    """F-04: entity scope extracts every Maps business in id order and
    skips targets with no currently extractable snapshot."""
    conn = prepared(tmp_path / "entity-scope.sqlite", reviews=[REVIEW])
    entity = business_entity_id_for_maps_business(1)
    conn.execute(
        "INSERT INTO runs(id,area_name,bbox_json,cell_km,depth,queries_json,scraper_image,"
        "config_json,raw_path,status,started_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("r2", "test", '{"max_lat":22,"max_lon":40,"min_lat":21,"min_lon":39}',
         2.0, 1, '["restaurant"]', "gosom/google-maps-scraper:v1.18.1",
         '{"strict_bounds":true}', "/evidence/r2.jsonl", "complete",
         "2026-09-25T11:00:00+00:00"),
    )
    conn.execute(
        "INSERT INTO businesses(id,canonical_key,title,first_seen_at,last_seen_at,"
        "last_run_id,raw_json) VALUES (?,?,?,?,?,?,?)",
        (2, "second-business", "Second Business",
         "2026-09-25T11:00:00+00:00", "2026-09-26T09:59:00+00:00",
         "r2", "{}"),
    )
    conn.execute(
        "INSERT INTO knowledge_subjects(id,kind,record_state,created_at,updated_at) "
        "VALUES ('loc2','location','active',"
        "'2026-09-25T10:00:00+00:00','2026-09-26T10:00:00+00:00')",
    )
    conn.execute(
        "INSERT INTO business_locations(id,business_entity_id,label,location_type,"
        "created_at,updated_at) VALUES (?,?,?,?,?,?)",
        ("loc2", entity, "second", "branch",
         "2026-09-25T10:00:00+00:00", "2026-09-26T10:00:00+00:00"),
    )
    conn.execute(
        "INSERT INTO maps_business_location_links(business_id,location_id,linked_at) "
        "VALUES (2,'loc2','2026-09-25T10:00:00+00:00')",
    )
    conn.commit()
    payload = extract_retained_reviews_for_entity(
        conn, entity_id=entity, now=lambda: "2026-09-26T12:00:00+00:00")
    assert payload["entity_id"] == entity
    assert payload["business_count"] == 1
    assert payload["skipped_count"] == 1
    assert payload["skipped"][0]["business_id"] == 2
    assert payload["skipped"][0]["reason"]
    assert payload["businesses"][0]["business_id"] == 1
    assert payload["businesses"][0]["status"] == "complete"
    second = extract_retained_reviews_for_entity(
        conn, entity_id=entity, now=lambda: "2026-09-26T12:30:00+00:00")
    assert second["businesses"][0]["already_extracted"] is True
    conn.close()


def test_reviews_cli_entity_id(tmp_path: Path, capsys) -> None:
    """F-04: the sara-reviews CLI accepts the entity-scoped executor target."""
    from sara.reviews import main as reviews_main

    db = tmp_path / "cli-entity.sqlite"
    conn = prepared(db, reviews=[REVIEW])
    conn.close()
    rc = reviews_main([
        "--db", str(db), "--entity-id", business_entity_id_for_maps_business(1)])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["business_count"] == 1
    assert payload["skipped_count"] == 0


def test_owner_response_fields_preserved(tmp_path: Path) -> None:
    """Owner response text + dates survive into the observation value."""
    review = dict(REVIEW)
    review.update({
        "reply_text_original": "Thank you for your feedback",
        "reply_language": "en",
        "reply_posted_at_unix_micros": 1_758_760_000_000_000,
    })
    conn = prepared(tmp_path / "reply.sqlite", reviews=[review])
    extract_retained_reviews(conn, business_id=1)
    row = conn.execute(
        "SELECT value_json FROM observations "
        "WHERE predicate='reputation.customer_review' LIMIT 1").fetchone()
    value = json.loads(row[0])
    resp = value["owner_response"]
    assert resp["text"] == "Thank you for your feedback"
    assert resp["language"] == "en"
    assert resp["published_at"] is not None
    conn.close()
