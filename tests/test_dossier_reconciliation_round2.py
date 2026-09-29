from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from sara.dossier import (
    DossierAssessmentError,
    build_business_dossier,
    persist_dossier_assessment,
)
from sara.maps_backfill import backfill_maps_business_understanding, location_id_for_maps_business
from sara.migrations import apply_migrations
from sara.reviews import extract_retained_reviews
from sara.storage import connect, ingest_records
from sara.understanding_vocabulary import seed_business_understanding_vocabulary


def _prepared(path: Path, *, with_review: bool = False):
    conn = connect(path)
    assert apply_migrations(conn) == (1, 2)
    seed_business_understanding_vocabulary(conn)
    conn.execute(
        "INSERT INTO runs("
        "id,area_name,bbox_json,cell_km,depth,queries_json,scraper_image,config_json,"
        "raw_path,status,started_at"
        ") VALUES (?,?,?,?,?,?,?,?,?,'running',?)",
        (
            "r1",
            "reconciliation-round2",
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
        "place_id": "place-reconcile-2",
        "cid": "cid-reconcile-2",
        "data_id": "data-reconcile-2",
        "title": "Reconciliation Restaurant",
        "category": "Restaurant",
        "address": "Reconciliation Street",
        "latitude": 21.55,
        "longitude": 39.18,
        "phone": "+966500000000",
        "website": "https://reconciliation.example",
        "review_rating": 4.6,
        "review_count": 17,
        "status": "Open",
        "link": "https://maps.example/reconciliation",
    }
    if with_review:
        record["user_reviews"] = [
            {
                "review_id": "review-reconcile-2",
                "source": "Google",
                "Rating": 5,
                "Description": "Good service",
                "language": "en",
                "posted_at_unix_micros": 1_758_758_400_000_000,
            }
        ]
    ingest_records(conn, "r1", [record], finalize_run=("complete", 0, None))
    conn.execute(
        "UPDATE runs SET finished_at = '2026-09-26T09:59:00+00:00' "
        "WHERE id = 'r1' AND finished_at > '2026-09-26T09:59:00+00:00'"
    )
    conn.commit()
    with patch("sara.maps_backfill._utc_now", return_value="2026-09-26T10:00:00+00:00"):
        backfill_maps_business_understanding(conn)
    if with_review:
        extracted = extract_retained_reviews(
            conn,
            business_id=1,
            now=lambda: "2026-09-27T12:00:00+00:00",
        )
        assert extracted.observations_created == 1
    return conn


def _name_fact_id(conn) -> str:
    row = conn.execute(
        "SELECT id FROM facts WHERE predicate='business.name.trading' AND valid_to IS NULL"
    ).fetchone()
    assert row is not None
    return str(row[0])


def _review_ids(conn) -> tuple[str, str]:
    row = conn.execute(
        "SELECT o.id,e.id FROM observations o "
        "JOIN evidence_items e ON e.id=o.evidence_id "
        "WHERE o.predicate='reputation.customer_review'"
    ).fetchone()
    assert row is not None
    return str(row[0]), str(row[1])


def _pin_later_identifier_watermark(conn, stamp: str) -> None:
    conn.execute(
        "UPDATE external_identifiers SET last_observed_at=? "
        "WHERE subject_id=? AND namespace='place_id' AND status='active'",
        (stamp, location_id_for_maps_business(1)),
    )
    conn.commit()


def test_fact_created_at_participates_in_assessment_chronology(tmp_path: Path) -> None:
    conn = _prepared(tmp_path / "fact-created-future.sqlite")
    fact_id = _name_fact_id(conn)
    conn.execute("DROP TRIGGER facts_version_immutable")
    conn.execute(
        "UPDATE facts SET created_at='2026-09-30T13:00:00+00:00' WHERE id=?",
        (fact_id,),
    )
    conn.commit()

    with pytest.raises(DossierAssessmentError, match="later than the assessment clock"):
        persist_dossier_assessment(
            conn,
            business_id=1,
            now=lambda: "2026-09-28T10:00:00+00:00",
        )
    assert conn.in_transaction is False
    assert conn.execute("SELECT COUNT(*) FROM dossier_assessments").fetchone()[0] == 0
    conn.close()


def test_fact_created_at_changes_deterministic_signature_below_stable_watermark(
    tmp_path: Path,
) -> None:
    conn = _prepared(tmp_path / "fact-created-signature.sqlite")
    stable_later_watermark = "2026-09-29T00:00:00+00:00"
    _pin_later_identifier_watermark(conn, stable_later_watermark)

    first = persist_dossier_assessment(
        conn,
        business_id=1,
        now=lambda: "2026-09-30T10:00:00+00:00",
    )
    assert first.facts_as_of == stable_later_watermark

    fact_id = _name_fact_id(conn)
    conn.execute("DROP TRIGGER facts_version_immutable")
    conn.execute(
        "UPDATE facts SET created_at='2026-09-28T12:00:00+00:00' WHERE id=?",
        (fact_id,),
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
    conn.close()


def test_reconciled_at_changes_chronology_signature_below_stable_watermark(
    tmp_path: Path,
) -> None:
    conn = _prepared(tmp_path / "reconciled-at-signature.sqlite")
    stable_later_watermark = "2026-09-29T00:00:00+00:00"
    _pin_later_identifier_watermark(conn, stable_later_watermark)

    first = persist_dossier_assessment(
        conn,
        business_id=1,
        now=lambda: "2026-09-30T10:00:00+00:00",
    )
    assert first.facts_as_of == stable_later_watermark

    fact_id = _name_fact_id(conn)
    conn.execute("DROP TRIGGER facts_version_immutable")
    conn.execute(
        "UPDATE facts SET reconciled_at='2026-09-28T14:00:00+00:00' WHERE id=?",
        (fact_id,),
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
    conn.close()


def test_review_observed_after_extraction_is_reported_and_excluded(tmp_path: Path) -> None:
    conn = _prepared(tmp_path / "review-observed-after-extraction.sqlite", with_review=True)
    observation_id, evidence_id = _review_ids(conn)
    conn.execute("DROP TRIGGER observations_immutable")
    conn.execute(
        "UPDATE observations SET observed_at='2026-09-27T13:00:00+00:00' WHERE id=?",
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
        issue["code"] == "customer_review_observation_chronology_invalid"
        and issue["observation_id"] == observation_id
        and issue["evidence_id"] == evidence_id
        and issue["observed_at"] == "2026-09-27T13:00:00+00:00"
        for issue in dossier["integrity_issues"]
    )

    assessment = persist_dossier_assessment(
        conn,
        business_id=1,
        now=lambda: "2026-09-28T10:00:00+00:00",
    )
    assert assessment.analysis_ready is False
    conn.close()


def test_review_observation_time_changes_signature_below_stable_watermark(
    tmp_path: Path,
) -> None:
    conn = _prepared(tmp_path / "review-time-signature.sqlite", with_review=True)
    stable_later_watermark = "2026-09-29T00:00:00+00:00"
    _pin_later_identifier_watermark(conn, stable_later_watermark)

    first = persist_dossier_assessment(
        conn,
        business_id=1,
        now=lambda: "2026-09-30T10:00:00+00:00",
    )
    assert first.facts_as_of == stable_later_watermark

    observation_id, _evidence_id = _review_ids(conn)
    conn.execute("DROP TRIGGER observations_immutable")
    conn.execute(
        "UPDATE observations SET observed_at='2026-09-27T11:30:00+00:00' WHERE id=?",
        (observation_id,),
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
    conn.close()
