from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from sara.dossier import (
    DossierAssessmentError,
    build_business_dossier,
    persist_dossier_assessment,
)
from sara.maps_backfill import backfill_maps_business_understanding
from sara.migrations import apply_migrations
from sara.reviews import extract_retained_reviews
from sara.storage import connect, ingest_records
from sara.understanding_vocabulary import seed_business_understanding_vocabulary


def _prepared(path: Path, *, with_review: bool = False):
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
            "dossier-integrity",
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
    record = {
        "place_id": "place-integrity",
        "cid": "cid-integrity",
        "data_id": "data-integrity",
        "title": "Integrity Restaurant",
        "category": "Restaurant",
        "address": "Integrity Street",
        "latitude": 21.55,
        "longitude": 39.18,
        "phone": "+966500000000",
        "website": "https://integrity.example",
        "review_rating": 4.6,
        "review_count": 17,
        "status": "Open",
        "link": "https://maps.example/integrity",
    }
    if with_review:
        record["user_reviews"] = [
            {
                "review_id": "review-integrity-1",
                "source": "Google",
                "Rating": 5,
                "Description": "Good service",
                "language": "en",
                "posted_at_unix_micros": 1_758_758_400_000_000,
            }
        ]
    ingest_records(conn, "r1", [record], finalize_run=("complete", 0, None))
    with patch("sara.maps_backfill._utc_now", return_value="2026-09-26T10:00:00+00:00"):
        backfill_maps_business_understanding(conn)
    return conn


def test_assessment_clock_is_obtained_after_writer_transaction_begins(tmp_path: Path) -> None:
    conn = _prepared(tmp_path / "clock.sqlite")
    seen = {"called": False}

    def now() -> str:
        assert conn.in_transaction
        seen["called"] = True
        return "2026-09-28T10:00:00+00:00"

    result = persist_dossier_assessment(conn, business_id=1, now=now)
    assert seen["called"] is True
    assert result.computed_at == "2026-09-28T10:00:00+00:00"
    assert conn.in_transaction is False
    conn.close()


def test_invalid_review_evidence_is_reported_but_not_promoted_to_customer_voice(
    tmp_path: Path,
) -> None:
    conn = _prepared(tmp_path / "review-integrity.sqlite", with_review=True)
    extracted = extract_retained_reviews(
        conn,
        business_id=1,
        now=lambda: "2026-09-27T12:00:00+00:00",
    )
    assert extracted.observations_created == 1
    evidence_id = conn.execute(
        "SELECT evidence_id FROM observations WHERE predicate='reputation.customer_review'"
    ).fetchone()[0]

    # Simulate durable-state corruption that bypassed the normal immutable writer.
    # The dossier must fail closed at the projection boundary rather than count it
    # as valid customer voice merely because the observation row is reachable.
    conn.execute("DROP TRIGGER evidence_items_immutable")
    conn.execute("UPDATE evidence_items SET status='incomplete' WHERE id=?", (evidence_id,))
    conn.commit()

    dossier = build_business_dossier(
        conn,
        business_id=1,
        evaluated_at="2026-09-28T10:00:00+00:00",
    )
    assert dossier["customer_voice"]["review_count"] == 0
    assert any(
        issue["code"] == "customer_review_uses_nonusable_evidence"
        and issue["observation_id"]
        for issue in dossier["integrity_issues"]
    )

    assessment = persist_dossier_assessment(
        conn,
        business_id=1,
        now=lambda: "2026-09-28T10:00:00+00:00",
    )
    states = {item["domain"]: item["state"] for item in assessment.domains}
    assert states["reputation"] == "partial"
    assert assessment.analysis_ready is False
    conn.close()


def test_corrupted_review_value_hash_is_not_promoted_to_customer_voice(tmp_path: Path) -> None:
    conn = _prepared(tmp_path / "review-hash.sqlite", with_review=True)
    extracted = extract_retained_reviews(
        conn,
        business_id=1,
        now=lambda: "2026-09-27T12:00:00+00:00",
    )
    assert extracted.observations_created == 1
    observation_id = conn.execute(
        "SELECT id FROM observations WHERE predicate='reputation.customer_review'"
    ).fetchone()[0]

    conn.execute("DROP TRIGGER observations_immutable")
    conn.execute(
        "UPDATE observations SET normalized_value_json='{}' WHERE id=?",
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
        issue["code"] == "customer_review_value_hash_mismatch"
        and issue["observation_id"] == observation_id
        for issue in dossier["integrity_issues"]
    )
    conn.close()


def test_assessment_identity_changes_when_fact_support_graph_changes(tmp_path: Path) -> None:
    conn = _prepared(tmp_path / "support-signature.sqlite")
    first = persist_dossier_assessment(
        conn,
        business_id=1,
        now=lambda: "2026-09-28T10:00:00+00:00",
    )
    first_states = {item["domain"]: item["state"] for item in first.domains}

    fact_id = conn.execute(
        "SELECT id FROM facts WHERE predicate='business.name.trading' AND valid_to IS NULL"
    ).fetchone()[0]
    acquisition_id = conn.execute(
        "SELECT id FROM acquisition_sessions WHERE legacy_run_id='r1'"
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO fact_acquisition_support(fact_id,acquisition_session_id,support_role) "
        "VALUES (?,?,'context')",
        (fact_id, acquisition_id),
    )
    conn.commit()

    second = persist_dossier_assessment(
        conn,
        business_id=1,
        now=lambda: "2026-09-28T10:00:00+00:00",
    )
    second_states = {item["domain"]: item["state"] for item in second.domains}
    assert second.assessment_id != first.assessment_id
    assert second_states == first_states

    source_observation_id = conn.execute(
        "SELECT fos.observation_id FROM fact_observation_support fos "
        "WHERE fos.fact_id=? AND fos.support_role='supports' ORDER BY fos.observation_id LIMIT 1",
        (fact_id,),
    ).fetchone()[0]
    duplicate_observation_id = "obs_test_additional_same_source_support"
    conn.execute(
        "INSERT INTO observations("
        "id,subject_id,predicate,evidence_id,value_json,normalized_value_json,value_hash,"
        "observation_kind,observed_at,extracted_at,extraction_method,extractor_name,"
        "extractor_version,confidence,created_at"
        ") SELECT ?,subject_id,predicate,evidence_id,value_json,normalized_value_json,value_hash,"
        "observation_kind,observed_at,extracted_at,extraction_method,extractor_name,"
        "extractor_version,confidence,created_at FROM observations WHERE id=?",
        (duplicate_observation_id, source_observation_id),
    )
    conn.execute(
        "INSERT INTO fact_observation_support(fact_id,observation_id,support_role) "
        "VALUES (?,?,'supports')",
        (fact_id, duplicate_observation_id),
    )
    conn.commit()

    third = persist_dossier_assessment(
        conn,
        business_id=1,
        now=lambda: "2026-09-28T10:00:00+00:00",
    )
    third_states = {item["domain"]: item["state"] for item in third.domains}
    assert third.assessment_id != second.assessment_id
    assert third_states == second_states
    assert conn.execute("SELECT COUNT(*) FROM dossier_assessments").fetchone()[0] == 3
    conn.close()


def test_existing_assessment_with_impossible_seal_chronology_fails_closed(tmp_path: Path) -> None:
    conn = _prepared(tmp_path / "seal-chronology.sqlite")
    first = persist_dossier_assessment(
        conn,
        business_id=1,
        now=lambda: "2026-09-28T10:00:00+00:00",
    )

    conn.execute("DROP TRIGGER dossier_assessment_seals_immutable")
    conn.execute(
        "UPDATE dossier_assessment_seals SET sealed_at='2026-09-27T10:00:00+00:00' "
        "WHERE assessment_id=?",
        (first.assessment_id,),
    )
    conn.commit()

    with pytest.raises(DossierAssessmentError, match="sealed before computation"):
        persist_dossier_assessment(
            conn,
            business_id=1,
            now=lambda: "2026-09-28T10:00:00+00:00",
        )
    assert conn.in_transaction is False
    assert conn.execute("SELECT COUNT(*) FROM dossier_assessments").fetchone()[0] == 1
    conn.close()
