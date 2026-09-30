from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from sara.maps_backfill import backfill_maps_business_understanding, business_entity_id_for_maps_business
from sara.migrations import apply_migrations
from sara.reviews import ReviewIntelligenceError, extract_retained_reviews
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
