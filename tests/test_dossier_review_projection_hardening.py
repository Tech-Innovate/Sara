from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from sara.dossier import build_business_dossier, persist_dossier_assessment
from sara.dossier.status import preview_domains
from sara.maps_backfill import backfill_maps_business_understanding
from sara.migrations import apply_migrations
from sara.reviews import extract_retained_reviews
from sara.storage import connect, ingest_records
from sara.understanding_vocabulary import seed_business_understanding_vocabulary


def _prepared_review_business(path: Path):
    conn = connect(path)
    assert apply_migrations(conn) == (1,)
    seed_business_understanding_vocabulary(conn)
    conn.execute(
        "INSERT INTO runs("
        "id,area_name,bbox_json,cell_km,depth,queries_json,scraper_image,config_json,"
        "raw_path,status,started_at"
        ") VALUES (?,?,?,?,?,?,?,?,?,'running',?)",
        (
            "r1",
            "review-projection-hardening",
            '{"max_lat":22,"max_lon":40,"min_lat":21,"min_lon":39}',
            2.0,
            1,
            '["restaurant"]',
            "gosom/google-maps-scraper:v1.18.1",
            '{"strict_bounds":true}',
            "/evidence/r1.jsonl",
            "2026-09-25T10:00:00+00:00",
        ),
    )
    conn.commit()
    ingest_records(
        conn,
        "r1",
        [
            {
                "place_id": "place-review-hardening",
                "cid": "cid-review-hardening",
                "data_id": "data-review-hardening",
                "title": "Review Hardening Restaurant",
                "category": "Restaurant",
                "address": "Review Hardening Street",
                "latitude": 21.55,
                "longitude": 39.18,
                "phone": "+966500000000",
                "website": "https://review-hardening.example",
                "review_rating": 4.6,
                "review_count": 17,
                "status": "Open",
                "link": "https://maps.example/review-hardening",
                "user_reviews": [
                    {
                        "review_id": "review-hardening-1",
                        "source": "Google",
                        "Rating": 5,
                        "Description": "Good service",
                        "language": "en",
                        "posted_at_unix_micros": 1_758_758_400_000_000,
                    }
                ],
            }
        ],
        finalize_run=("complete", 0, None),
    )
    with patch("sara.maps_backfill._utc_now", return_value="2026-09-26T10:00:00+00:00"):
        backfill_maps_business_understanding(conn)
    extracted = extract_retained_reviews(
        conn,
        business_id=1,
        now=lambda: "2026-09-27T12:00:00+00:00",
    )
    assert extracted.observations_created == 1
    return conn


def test_malformed_normalized_review_is_reported_and_excluded_without_aborting(
    tmp_path: Path,
) -> None:
    conn = _prepared_review_business(tmp_path / "malformed-review.sqlite")
    observation_id, evidence_id = conn.execute(
        "SELECT id,evidence_id FROM observations "
        "WHERE predicate='reputation.customer_review'"
    ).fetchone()

    conn.execute("DROP TRIGGER observations_immutable")
    conn.execute(
        "UPDATE observations SET normalized_value_json='{' WHERE id=?",
        (observation_id,),
    )
    conn.commit()

    dossier = build_business_dossier(
        conn,
        business_id=1,
        evaluated_at="2026-09-28T10:00:00+00:00",
    )
    assert dossier["customer_voice"]["review_count"] == 0
    assert any(
        issue["code"] == "customer_review_normalized_value_malformed"
        and issue["observation_id"] == observation_id
        and issue["evidence_id"] == evidence_id
        for issue in dossier["integrity_issues"]
    )

    assessment = persist_dossier_assessment(
        conn,
        business_id=1,
        now=lambda: "2026-09-28T10:00:00+00:00",
    )
    assert assessment.analysis_ready is False
    assert conn.execute("SELECT COUNT(*) FROM dossier_assessments").fetchone()[0] == 1
    conn.close()


def _preview_reputation(*, retrieved_at: str, evaluated_at: datetime) -> dict:
    domains = preview_domains(
        facts=[],
        unknowns=[],
        integrity_issues=[],
        customer_voice=[
            {
                "observation_id": "review-preview-1",
                "evidence": {"retrieved_at": retrieved_at},
            }
        ],
        evaluated_at=evaluated_at,
    )
    return next(item for item in domains if item["domain"] == "reputation")


def test_review_only_preview_is_stale_when_review_collection_is_expired() -> None:
    reputation = _preview_reputation(
        retrieved_at="2026-08-01T00:00:00+00:00",
        evaluated_at=datetime(2026, 9, 15, tzinfo=timezone.utc),
    )
    assert reputation["state"] == "stale"
    assert reputation["reasons"] == ["no_current_reputation_evidence_remains"]


def test_review_only_preview_remains_partial_while_review_collection_is_current() -> None:
    reputation = _preview_reputation(
        retrieved_at="2026-09-01T00:00:00+00:00",
        evaluated_at=datetime(2026, 9, 15, tzinfo=timezone.utc),
    )
    assert reputation["state"] == "partial"
    assert reputation["reasons"] == [
        "current_reputation_evidence_exists_but_preview_never_claims_sufficiency"
    ]
