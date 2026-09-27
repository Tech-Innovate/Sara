from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from sara.dossier import DossierAssessmentError, build_business_dossier, persist_dossier_assessment
from sara.maps_backfill import backfill_maps_business_understanding, location_id_for_maps_business
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
            "review-acquisition-chronology",
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
                "place_id": "place-review-acquisition-chronology",
                "cid": "cid-review-acquisition-chronology",
                "data_id": "data-review-acquisition-chronology",
                "title": "Review Acquisition Chronology Restaurant",
                "category": "Restaurant",
                "address": "Review Acquisition Chronology Street",
                "latitude": 21.55,
                "longitude": 39.18,
                "phone": "+966500000000",
                "website": "https://review-acquisition-chronology.example",
                "review_rating": 4.6,
                "review_count": 17,
                "status": "Open",
                "link": "https://maps.example/review-acquisition-chronology",
                "user_reviews": [
                    {
                        "review_id": "review-acquisition-chronology-1",
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


def _review_session_id(conn) -> str:
    row = conn.execute(
        "SELECT e.acquisition_session_id FROM observations o "
        "JOIN evidence_items e ON e.id=o.evidence_id "
        "WHERE o.predicate='reputation.customer_review'"
    ).fetchone()
    assert row is not None
    return str(row[0])


def test_review_acquisition_completion_participates_in_assessment_chronology(
    tmp_path: Path,
) -> None:
    conn = _prepared_review_business(tmp_path / "review-acquisition-future.sqlite")
    session_id = _review_session_id(conn)
    future_finished_at = "2026-09-30T13:00:00+00:00"
    conn.execute("DROP TRIGGER acquisition_sessions_terminal_immutable")
    conn.execute(
        "UPDATE acquisition_sessions SET finished_at=? WHERE id=?",
        (future_finished_at, session_id),
    )
    conn.commit()

    dossier = build_business_dossier(
        conn,
        business_id=1,
        evaluated_at="2026-09-28T10:00:00+00:00",
    )
    assert dossier["customer_voice"]["review_count"] == 1
    review_evidence = dossier["customer_voice"]["reviews"][0]["evidence"]
    assert review_evidence["acquisition_session_id"] == session_id
    assert review_evidence["acquisition_finished_at"] == future_finished_at

    with pytest.raises(DossierAssessmentError, match="later than the assessment clock"):
        persist_dossier_assessment(
            conn,
            business_id=1,
            now=lambda: "2026-09-28T10:00:00+00:00",
        )
    assert conn.in_transaction is False
    assert conn.execute("SELECT COUNT(*) FROM dossier_assessments").fetchone()[0] == 0
    conn.close()


def test_review_acquisition_lifecycle_changes_deterministic_input_signature(
    tmp_path: Path,
) -> None:
    conn = _prepared_review_business(tmp_path / "review-acquisition-signature.sqlite")
    session_id = _review_session_id(conn)
    location_id = location_id_for_maps_business(1)
    later_identifier_observation = "2026-09-29T00:00:00+00:00"
    conn.execute(
        "UPDATE external_identifiers SET last_observed_at=? "
        "WHERE subject_id=? AND namespace='place_id' AND status='active'",
        (later_identifier_observation, location_id),
    )
    conn.commit()

    first = persist_dossier_assessment(
        conn,
        business_id=1,
        now=lambda: "2026-09-30T10:00:00+00:00",
    )
    assert first.facts_as_of == later_identifier_observation

    conn.execute("DROP TRIGGER acquisition_sessions_terminal_immutable")
    conn.execute(
        "UPDATE acquisition_sessions SET finished_at=? WHERE id=?",
        ("2026-09-27T13:00:00+00:00", session_id),
    )
    conn.commit()

    second = persist_dossier_assessment(
        conn,
        business_id=1,
        now=lambda: "2026-09-30T10:00:00+00:00",
    )
    assert second.facts_as_of == first.facts_as_of
    assert second.assessment_id != first.assessment_id
    assert second.already_assessed is False
    assert conn.execute("SELECT COUNT(*) FROM dossier_assessments").fetchone()[0] == 2
    conn.close()
