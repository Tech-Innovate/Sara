from __future__ import annotations

import json
from pathlib import Path

import pytest

from sara.maps_backfill import (
    backfill_maps_business_understanding,
    business_entity_id_for_maps_business,
    location_id_for_maps_business,
)
from sara.migrations import apply_migrations
from sara.reviews import ReviewIntelligenceError, extract_retained_reviews
from sara.reviews.model import (
    COLLECTOR_NAME,
    COLLECTOR_VERSION,
    REVIEW_PREDICATE,
)
from sara.reviews.parser import extract_reviews
from sara.storage import connect as storage_connect, ingest_records
from sara.understanding_vocabulary import seed_business_understanding_vocabulary


def prepared_conn(path: Path):
    conn = storage_connect(path)
    assert apply_migrations(conn) == (1, 2, 3, 4)
    seed_business_understanding_vocabulary(conn)
    return conn


def add_run(conn, run_id: str, *, started_at: str) -> None:
    conn.execute(
        "INSERT INTO runs("
        "id,area_name,bbox_json,cell_km,depth,queries_json,scraper_image,config_json,"
        "raw_path,status,started_at"
        ") VALUES (?,?,?,?,?,?,?,?,?,'running',?)",
        (
            run_id,
            "review-test",
            '{"max_lat":22,"max_lon":40,"min_lat":21,"min_lon":39}',
            2.0,
            1,
            '["restaurant"]',
            "gosom/google-maps-scraper:v1.18.1",
            '{"strict_bounds":true}',
            f"/evidence/{run_id}.jsonl",
            started_at,
        ),
    )
    conn.commit()


def maps_record(*, reviews=None, extended=None) -> dict:
    record = {
        "place_id": "place-review-1",
        "cid": "cid-review-1",
        "data_id": "data-review-1",
        "title": "Review Restaurant",
        "category": "Restaurant",
        "address": "Review Street",
        "latitude": 21.55,
        "longitude": 39.18,
        "phone": "+966500000000",
        "website": "https://reviews.example",
        "review_rating": 4.5,
        "review_count": 25,
        "status": "Open",
        "link": "https://maps.example/review-1",
    }
    if reviews is not None:
        record["user_reviews"] = reviews
    if extended is not None:
        record["user_reviews_extended"] = extended
    return record


def ingest_and_backfill(conn, record: dict) -> int:
    add_run(conn, "r1", started_at="2026-09-27T07:00:00+00:00")
    ingest_records(conn, "r1", [record], finalize_run=("complete", 0, None))
    business_id = int(conn.execute("SELECT id FROM businesses").fetchone()[0])
    backfill_maps_business_understanding(conn)
    return business_id


def review_one() -> dict:
    return {
        "Name": "Public Reviewer",
        "ProfilePicture": "https://images.example/reviewer.jpg",
        "Rating": 5,
        "Description": "Excellent service",
        "When": "a month ago",
        "review_id": "review-001",
        "source": "Google",
        "rating_scale": 5,
        "rating_float": 5.0,
        "posted_at_unix_micros": 1_756_684_800_000_000,
        "updated_at_unix_micros": 1_756_684_800_000_000,
        "language": "en",
        "text_original": "Excellent service",
        "reply_text_original": "Thank you",
        "reply_language": "en",
        "reply_posted_at_unix_micros": 1_756_771_200_000_000,
    }


def review_without_id() -> dict:
    return {
        "Name": "Another Public Reviewer",
        "Rating": 4,
        "Description": "Good meal",
        "When": "2 weeks ago",
        "language": "en",
        "text_original": "Good meal",
        "posted_at_unix_micros": 1_757_203_200_000_000,
    }


def test_parser_collapses_exact_duplicates_but_preserves_distinct_review_variants() -> None:
    first = review_one()
    variant = dict(first)
    variant["text_original"] = "Excellent service and quick delivery"
    variant["Description"] = "Excellent service and quick delivery"
    raw = {
        "user_reviews": [first, review_without_id()],
        "user_reviews_extended": [dict(first), variant],
    }

    reviews, source_count = extract_reviews(raw)

    assert source_count == 4
    assert len(reviews) == 3
    assert sum(review.review_id == "review-001" for review in reviews) == 2
    duplicate = next(
        review
        for review in reviews
        if review.review_id == "review-001" and len(review.source_paths) == 2
    )
    assert duplicate.source_paths == ("user_reviews[0]", "user_reviews_extended[0]")


def test_retained_reviews_become_source_location_observations_not_facts(
    tmp_path: Path,
) -> None:
    conn = prepared_conn(tmp_path / "reviews.sqlite")
    first = review_one()
    business_id = ingest_and_backfill(
        conn,
        maps_record(
            reviews=[first, review_without_id()],
            extended=[dict(first)],
        ),
    )
    facts_before = int(conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0])

    stats = extract_retained_reviews(
        conn,
        business_id=business_id,
        now=lambda: "2026-09-27T08:00:00+00:00",
    )

    expected_location = location_id_for_maps_business(business_id)
    expected_entity = business_entity_id_for_maps_business(business_id)
    assert stats.business_id == business_id
    assert stats.business_entity_id == expected_entity
    assert stats.source_business_entity_id == expected_entity
    assert stats.source_location_id == expected_location
    assert stats.canonical_location_id == expected_location
    assert stats.source_review_records == 3
    assert stats.review_evidence_records == 2
    assert stats.duplicate_source_records_collapsed == 1
    assert stats.evidence_items_created == 2
    assert stats.observations_created == 2
    assert stats.already_extracted is False

    session = conn.execute(
        "SELECT target_subject_id,source_id,collector_name,collector_version,status,"
        "evidence_count,observation_count,legacy_run_id FROM acquisition_sessions "
        "WHERE id=?",
        (stats.session_id,),
    ).fetchone()
    assert tuple(session) == (
        stats.source_location_id,
        "src_google_maps",
        COLLECTOR_NAME,
        COLLECTOR_VERSION,
        "complete",
        2,
        2,
        None,
    )

    evidence = list(
        conn.execute(
            "SELECT id,source_role,status,content_sha256,artifact_ref,metadata_json "
            "FROM evidence_items WHERE acquisition_session_id=? ORDER BY id",
            (stats.session_id,),
        )
    )
    assert len(evidence) == 2
    assert {row["source_role"] for row in evidence} == {"customer_generated"}
    assert {row["status"] for row in evidence} == {"usable"}
    assert {row["artifact_ref"] for row in evidence} == {None}
    assert all(len(row["content_sha256"]) == 64 for row in evidence)

    observations = list(
        conn.execute(
            "SELECT subject_id,predicate,value_json,observation_kind,extraction_method,"
            "extractor_name,extractor_version,confidence FROM observations "
            "WHERE predicate=? ORDER BY id",
            (REVIEW_PREDICATE,),
        )
    )
    assert len(observations) == 2
    assert {row["subject_id"] for row in observations} == {stats.source_location_id}
    assert {row["predicate"] for row in observations} == {REVIEW_PREDICATE}
    assert {row["observation_kind"] for row in observations} == {"source_assertion"}
    assert {row["extraction_method"] for row in observations} == {"direct_structured"}
    assert {row["extractor_name"] for row in observations} == {COLLECTOR_NAME}
    assert {row["extractor_version"] for row in observations} == {COLLECTOR_VERSION}
    assert {row["confidence"] for row in observations} == {1.0}

    normalized = [json.loads(row["value_json"]) for row in observations]
    identified = next(item for item in normalized if item["review_id"] == "review-001")
    assert identified["rating"] == 5.0
    assert identified["text_original"] == "Excellent service"
    assert identified["owner_response"]["text"] == "Thank you"
    assert "Name" not in identified
    assert "author_url" not in identified

    raw_metadata = [json.loads(row["metadata_json"]) for row in evidence]
    identified_metadata = next(
        item for item in raw_metadata if item["source_review_id"] == "review-001"
    )
    retained_raw = json.loads(identified_metadata["raw_review_json"])
    assert retained_raw["Name"] == "Public Reviewer"
    assert identified_metadata["parent_evidence_id"] == stats.source_evidence_id
    assert identified_metadata["source_business_entity_id"] == stats.source_business_entity_id
    assert identified_metadata["source_location_id"] == stats.source_location_id
    assert identified_metadata["source_paths"] == [
        "user_reviews[0]",
        "user_reviews_extended[0]",
    ]

    assert int(conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]) == facts_before
    assert int(
        conn.execute(
            "SELECT COUNT(*) FROM fact_observation_support fos "
            "JOIN observations o ON o.id=fos.observation_id WHERE o.predicate=?",
            (REVIEW_PREDICATE,),
        ).fetchone()[0]
    ) == 0
    assert list(conn.execute("PRAGMA foreign_key_check")) == []
    conn.close()


def test_review_extraction_is_idempotent_for_the_same_retained_snapshot(tmp_path: Path) -> None:
    conn = prepared_conn(tmp_path / "idempotent.sqlite")
    business_id = ingest_and_backfill(conn, maps_record(reviews=[review_one()]))

    first = extract_retained_reviews(
        conn,
        business_id=business_id,
        now=lambda: "2026-09-27T08:00:00+00:00",
    )
    counts = tuple(
        int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in ("acquisition_sessions", "evidence_items", "observations", "facts")
    )
    second = extract_retained_reviews(
        conn,
        business_id=business_id,
        now=lambda: "2026-09-27T09:00:00+00:00",
    )

    assert second.session_id == first.session_id
    assert second.already_extracted is True
    assert second.evidence_items_created == 0
    assert second.observations_created == 0
    assert tuple(
        int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in ("acquisition_sessions", "evidence_items", "observations", "facts")
    ) == counts
    conn.close()


def test_empty_review_arrays_record_attempt_without_claiming_absence(tmp_path: Path) -> None:
    conn = prepared_conn(tmp_path / "empty.sqlite")
    business_id = ingest_and_backfill(
        conn,
        maps_record(reviews=[], extended=[]),
    )

    stats = extract_retained_reviews(
        conn,
        business_id=business_id,
        now=lambda: "2026-09-27T08:00:00+00:00",
    )

    assert stats.source_review_records == 0
    assert stats.review_evidence_records == 0
    assert stats.evidence_items_created == 0
    assert stats.observations_created == 0
    session = conn.execute(
        "SELECT status,evidence_count,observation_count FROM acquisition_sessions WHERE id=?",
        (stats.session_id,),
    ).fetchone()
    assert tuple(session) == ("complete", 0, 0)
    assert conn.execute(
        "SELECT COUNT(*) FROM facts WHERE predicate=?", (REVIEW_PREDICATE,)
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM facts WHERE status='not_observed'"
    ).fetchone()[0] == 0
    conn.close()


def test_malformed_review_source_fails_closed_without_partial_review_session(tmp_path: Path) -> None:
    conn = prepared_conn(tmp_path / "malformed.sqlite")
    business_id = ingest_and_backfill(
        conn,
        maps_record(reviews={"not": "an array"}),
    )
    sessions_before = int(conn.execute("SELECT COUNT(*) FROM acquisition_sessions").fetchone()[0])

    with pytest.raises(ReviewIntelligenceError, match="must be an array"):
        extract_retained_reviews(conn, business_id=business_id)

    # F-06 (PR #21): a deterministic malformed-review failure now
    # leaves DURABLE acquisition state — exactly one failed session
    # (never a partial one) with no evidence or observations.
    rows = conn.execute(
        "SELECT status, error, evidence_count, observation_count "
        "FROM acquisition_sessions "
        "WHERE collector_name=?", (COLLECTOR_NAME,)
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "failed"
    assert "must be an array" in rows[0][1]
    assert rows[0][2] == 0 and rows[0][3] == 0
    assert int(conn.execute(
        "SELECT COUNT(*) FROM acquisition_sessions").fetchone()[0]
    ) == sessions_before + 1
    assert conn.execute(
        "SELECT COUNT(*) FROM observations WHERE predicate=?", (REVIEW_PREDICATE,)
    ).fetchone()[0] == 0
    conn.close()


def test_review_extraction_requires_exact_current_maps_evidence(tmp_path: Path) -> None:
    conn = prepared_conn(tmp_path / "drift.sqlite")
    business_id = ingest_and_backfill(conn, maps_record(reviews=[review_one()]))
    raw = json.loads(conn.execute("SELECT raw_json FROM businesses WHERE id=?", (business_id,)).fetchone()[0])
    raw["user_reviews"][0]["text_original"] = "changed without Maps synchronization"
    conn.execute(
        "UPDATE businesses SET raw_json=? WHERE id=?",
        (json.dumps(raw, ensure_ascii=False, sort_keys=True), business_id),
    )
    conn.commit()

    with pytest.raises(ReviewIntelligenceError, match="run sara-maps-sync"):
        extract_retained_reviews(conn, business_id=business_id)
    conn.close()


def test_review_extraction_does_not_accept_entity_only_scope(tmp_path: Path) -> None:
    conn = prepared_conn(tmp_path / "scope.sqlite")
    business_id = ingest_and_backfill(conn, maps_record(reviews=[review_one()]))
    with pytest.raises(ReviewIntelligenceError, match="select exactly one"):
        extract_retained_reviews(conn)
    with pytest.raises(ReviewIntelligenceError, match="select exactly one"):
        extract_retained_reviews(conn, business_id=business_id, canonical_key="also-set")
    conn.close()
