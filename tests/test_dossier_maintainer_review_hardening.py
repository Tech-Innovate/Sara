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
    assert apply_migrations(conn) == (1, 2, 3, 4)
    seed_business_understanding_vocabulary(conn)
    conn.execute(
        "INSERT INTO runs("
        "id,area_name,bbox_json,cell_km,depth,queries_json,scraper_image,config_json,"
        "raw_path,status,started_at"
        ") VALUES (?,?,?,?,?,?,?,?,?,'running',?)",
        (
            "r1",
            "maintainer-review-hardening",
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
        "place_id": "place-maintainer",
        "cid": "cid-maintainer",
        "data_id": "data-maintainer",
        "title": "Maintainer Review Restaurant",
        "category": "Restaurant",
        "address": "Maintainer Review Street",
        "latitude": 21.55,
        "longitude": 39.18,
        "phone": "+966500000000",
        "website": "https://maintainer-review.example",
        "review_rating": 4.6,
        "review_count": 17,
        "status": "Open",
        "link": "https://maps.example/maintainer-review",
    }
    if with_review:
        record["user_reviews"] = [
            {
                "review_id": "review-maintainer-1",
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


def _review_ids(conn) -> tuple[str, str, str]:
    row = conn.execute(
        "SELECT o.id,e.id,e.acquisition_session_id FROM observations o "
        "JOIN evidence_items e ON e.id=o.evidence_id "
        "WHERE o.predicate='reputation.customer_review'"
    ).fetchone()
    assert row is not None
    return str(row[0]), str(row[1]), str(row[2])


def _fact_evidence_session(conn) -> str:
    row = conn.execute(
        "SELECT DISTINCT e.acquisition_session_id FROM facts f "
        "JOIN fact_observation_support fos ON fos.fact_id=f.id "
        "JOIN observations o ON o.id=fos.observation_id "
        "JOIN evidence_items e ON e.id=o.evidence_id "
        "WHERE f.predicate='business.name.trading' AND f.valid_to IS NULL"
    ).fetchone()
    assert row is not None
    return str(row[0])


def _fact_evidence_id(conn) -> str:
    row = conn.execute(
        "SELECT DISTINCT e.id FROM facts f "
        "JOIN fact_observation_support fos ON fos.fact_id=f.id "
        "JOIN observations o ON o.id=fos.observation_id "
        "JOIN evidence_items e ON e.id=o.evidence_id "
        "WHERE f.predicate='business.name.trading' AND f.valid_to IS NULL"
    ).fetchone()
    assert row is not None
    return str(row[0])


def test_malformed_review_acquisition_timestamp_is_localized_and_excluded(
    tmp_path: Path,
) -> None:
    conn = _prepared(tmp_path / "malformed-review-acquisition.sqlite", with_review=True)
    observation_id, evidence_id, session_id = _review_ids(conn)
    conn.execute("DROP TRIGGER acquisition_sessions_terminal_immutable")
    conn.execute(
        "UPDATE acquisition_sessions SET finished_at='not-a-timestamp' WHERE id=?",
        (session_id,),
    )
    conn.commit()

    dossier = build_business_dossier(
        conn,
        business_id=1,
        evaluated_at="2026-09-28T10:00:00+00:00",
    )
    assert dossier["customer_voice"]["review_count"] == 0
    assert any(
        issue["code"] == "customer_review_acquisition_finished_at_invalid"
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


def test_malformed_review_extraction_timestamp_is_localized_and_excluded(
    tmp_path: Path,
) -> None:
    conn = _prepared(tmp_path / "malformed-review-extracted.sqlite", with_review=True)
    observation_id, evidence_id, _session_id = _review_ids(conn)
    conn.execute("DROP TRIGGER observations_immutable")
    conn.execute(
        "UPDATE observations SET extracted_at='not-a-timestamp' WHERE id=?",
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
        issue["code"] == "customer_review_extracted_at_invalid"
        and issue["observation_id"] == observation_id
        and issue["evidence_id"] == evidence_id
        for issue in dossier["integrity_issues"]
    )
    conn.close()


def test_reversed_review_acquisition_lifecycle_is_excluded(tmp_path: Path) -> None:
    conn = _prepared(tmp_path / "reversed-review-acquisition.sqlite", with_review=True)
    observation_id, evidence_id, session_id = _review_ids(conn)
    conn.execute("DROP TRIGGER acquisition_sessions_terminal_immutable")
    conn.execute(
        "UPDATE acquisition_sessions SET finished_at='2026-09-27T11:00:00+00:00' WHERE id=?",
        (session_id,),
    )
    conn.commit()

    dossier = build_business_dossier(
        conn,
        business_id=1,
        evaluated_at="2026-09-28T10:00:00+00:00",
    )
    assert dossier["customer_voice"]["review_count"] == 0
    assert any(
        issue["code"] == "customer_review_acquisition_chronology_invalid"
        and issue["observation_id"] == observation_id
        and issue["evidence_id"] == evidence_id
        and issue["acquisition_session_id"] == session_id
        for issue in dossier["integrity_issues"]
    )
    conn.close()


def test_fact_observation_support_acquisition_completion_participates_in_chronology(
    tmp_path: Path,
) -> None:
    conn = _prepared(tmp_path / "fact-support-acquisition-future.sqlite")
    session_id = _fact_evidence_session(conn)
    conn.execute("DROP TRIGGER acquisition_sessions_terminal_immutable")
    conn.execute(
        "UPDATE acquisition_sessions SET finished_at='2026-09-30T13:00:00+00:00' WHERE id=?",
        (session_id,),
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


def test_fact_support_acquisition_lifecycle_changes_deterministic_signature(
    tmp_path: Path,
) -> None:
    conn = _prepared(tmp_path / "fact-support-acquisition-signature.sqlite")
    session_id = _fact_evidence_session(conn)
    location_id = location_id_for_maps_business(1)
    stable_later_watermark = "2026-09-29T00:00:00+00:00"
    conn.execute(
        "UPDATE external_identifiers SET last_observed_at=? "
        "WHERE subject_id=? AND namespace='place_id' AND status='active'",
        (stable_later_watermark, location_id),
    )
    conn.commit()

    first = persist_dossier_assessment(
        conn,
        business_id=1,
        now=lambda: "2026-09-30T10:00:00+00:00",
    )
    assert first.facts_as_of == stable_later_watermark

    conn.execute("DROP TRIGGER acquisition_sessions_terminal_immutable")
    conn.execute(
        "UPDATE acquisition_sessions SET finished_at='2026-09-28T12:00:00+00:00' WHERE id=?",
        (session_id,),
    )
    conn.commit()

    second = persist_dossier_assessment(
        conn,
        business_id=1,
        now=lambda: "2026-09-30T10:05:00+00:00",
    )
    assert second.facts_as_of == first.facts_as_of
    assert second.assessment_id != first.assessment_id
    assert second.already_assessed is False
    conn.close()


def test_non_google_place_identifier_does_not_satisfy_maps_identity_anchor(
    tmp_path: Path,
) -> None:
    conn = _prepared(tmp_path / "non-google-identity-anchor.sqlite")
    location_id = location_id_for_maps_business(1)
    conn.execute(
        "UPDATE external_identifiers "
        "SET status='retired',status_changed_at='2026-09-27T12:00:00+00:00' "
        "WHERE subject_id=?",
        (location_id,),
    )
    conn.execute(
        "INSERT INTO sources(id,source_type,name,base_url,created_at,active) "
        "VALUES ('src_directory_identity','business_directory','Directory',NULL,?,1)",
        ("2026-09-27T12:00:00+00:00",),
    )
    conn.execute(
        "INSERT INTO external_identifiers("
        "id,subject_id,source_id,namespace,value,status,first_observed_at,last_observed_at,created_at"
        ") VALUES (?,?,?,?,?,'active',?,?,?)",
        (
            "xid_directory_place",
            location_id,
            "src_directory_identity",
            "place_id",
            "directory-place-like-value",
            "2026-09-27T12:00:00+00:00",
            "2026-09-27T12:00:00+00:00",
            "2026-09-27T12:00:00+00:00",
        ),
    )
    conn.commit()

    assessment = persist_dossier_assessment(
        conn,
        business_id=1,
        now=lambda: "2026-09-28T10:00:00+00:00",
    )
    identity = next(item for item in assessment.domains if item["domain"] == "identity")
    assert identity["state"] == "partial"
    assert identity["reason"]["code"] == "fresh_name_without_strong_location_identity_anchor"
    conn.close()


def test_external_identifier_created_at_participates_in_assessment_chronology(
    tmp_path: Path,
) -> None:
    conn = _prepared(tmp_path / "identifier-created-at-future.sqlite")
    location_id = location_id_for_maps_business(1)
    conn.execute(
        "UPDATE external_identifiers "
        "SET status='retired',status_changed_at='2026-09-27T12:00:00+00:00' "
        "WHERE subject_id=?",
        (location_id,),
    )
    conn.execute(
        "INSERT INTO external_identifiers("
        "id,subject_id,source_id,namespace,value,status,first_observed_at,last_observed_at,created_at"
        ") VALUES (?,?,?,?,?,'active',?,?,?)",
        (
            "xid_future_created_place",
            location_id,
            "src_google_maps",
            "place_id",
            "place-future-created",
            "2026-09-25T10:00:00+00:00",
            "2026-09-25T10:00:00+00:00",
            "2026-09-30T13:00:00+00:00",
        ),
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


def test_business_entity_created_at_participates_in_assessment_chronology(
    tmp_path: Path,
) -> None:
    conn = _prepared(tmp_path / "entity-created-at-future.sqlite")
    entity_id = str(conn.execute("SELECT id FROM business_entities").fetchone()[0])
    conn.execute(
        "UPDATE business_entities SET created_at='2026-09-30T13:00:00+00:00' WHERE id=?",
        (entity_id,),
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


def test_business_location_created_at_participates_in_assessment_chronology(
    tmp_path: Path,
) -> None:
    conn = _prepared(tmp_path / "location-created-at-future.sqlite")
    location_id = location_id_for_maps_business(1)
    conn.execute(
        "UPDATE business_locations SET created_at='2026-09-30T13:00:00+00:00' WHERE id=?",
        (location_id,),
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


def test_maps_linked_at_participates_in_assessment_chronology(tmp_path: Path) -> None:
    conn = _prepared(tmp_path / "maps-linked-at-future.sqlite")
    conn.execute(
        "UPDATE maps_business_location_links "
        "SET linked_at='2026-09-30T13:00:00+00:00' WHERE business_id=1"
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


def test_maps_linked_at_changes_signature_below_stable_watermark(tmp_path: Path) -> None:
    conn = _prepared(tmp_path / "maps-linked-at-signature.sqlite")
    location_id = location_id_for_maps_business(1)
    stable_later_watermark = "2026-09-29T00:00:00+00:00"
    conn.execute(
        "UPDATE external_identifiers SET last_observed_at=? "
        "WHERE subject_id=? AND namespace='place_id' AND status='active'",
        (stable_later_watermark, location_id),
    )
    conn.commit()

    first = persist_dossier_assessment(
        conn,
        business_id=1,
        now=lambda: "2026-09-30T10:00:00+00:00",
    )
    assert first.facts_as_of == stable_later_watermark

    conn.execute(
        "UPDATE maps_business_location_links "
        "SET linked_at='2026-09-28T12:00:00+00:00' WHERE business_id=1"
    )
    conn.commit()

    second = persist_dossier_assessment(
        conn,
        business_id=1,
        now=lambda: "2026-09-30T10:05:00+00:00",
    )
    assert second.facts_as_of == first.facts_as_of
    assert second.assessment_id != first.assessment_id
    assert second.already_assessed is False
    conn.close()


def test_understanding_entity_subject_created_at_participates_in_chronology(
    tmp_path: Path,
) -> None:
    conn = _prepared(tmp_path / "subject-entity-created-future.sqlite")
    entity_id = str(conn.execute("SELECT id FROM business_entities").fetchone()[0])
    conn.execute(
        "UPDATE knowledge_subjects SET created_at='2026-09-30T13:00:00+00:00' WHERE id=?",
        (entity_id,),
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


def test_understanding_location_subject_updated_at_participates_in_chronology(
    tmp_path: Path,
) -> None:
    conn = _prepared(tmp_path / "subject-location-updated-future.sqlite")
    location_id = location_id_for_maps_business(1)
    conn.execute(
        "UPDATE knowledge_subjects SET updated_at='2026-09-30T13:00:00+00:00' WHERE id=?",
        (location_id,),
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


def test_fact_evidence_published_at_participates_in_chronology(tmp_path: Path) -> None:
    conn = _prepared(tmp_path / "evidence-published-future.sqlite")
    evidence_id = _fact_evidence_id(conn)
    conn.execute("DROP TRIGGER evidence_items_immutable")
    conn.execute(
        "UPDATE evidence_items SET published_at='2026-09-30T13:00:00+00:00' WHERE id=?",
        (evidence_id,),
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


def test_review_evidence_published_at_participates_in_chronology(tmp_path: Path) -> None:
    conn = _prepared(tmp_path / "review-published-future.sqlite", with_review=True)
    _observation_id, evidence_id, _session_id = _review_ids(conn)
    conn.execute("DROP TRIGGER evidence_items_immutable")
    conn.execute(
        "UPDATE evidence_items SET published_at='2026-09-30T13:00:00+00:00' WHERE id=?",
        (evidence_id,),
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


def test_evidence_published_at_changes_signature_below_stable_watermark(
    tmp_path: Path,
) -> None:
    conn = _prepared(tmp_path / "evidence-published-signature.sqlite")
    location_id = location_id_for_maps_business(1)
    evidence_id = _fact_evidence_id(conn)
    stable_later_watermark = "2026-09-29T00:00:00+00:00"
    conn.execute(
        "UPDATE external_identifiers SET last_observed_at=? "
        "WHERE subject_id=? AND namespace='place_id' AND status='active'",
        (stable_later_watermark, location_id),
    )
    conn.commit()

    first = persist_dossier_assessment(
        conn,
        business_id=1,
        now=lambda: "2026-09-30T10:00:00+00:00",
    )
    assert first.facts_as_of == stable_later_watermark

    conn.execute("DROP TRIGGER evidence_items_immutable")
    conn.execute(
        "UPDATE evidence_items SET published_at='2026-09-28T12:00:00+00:00' WHERE id=?",
        (evidence_id,),
    )
    conn.commit()

    second = persist_dossier_assessment(
        conn,
        business_id=1,
        now=lambda: "2026-09-30T10:05:00+00:00",
    )
    assert second.facts_as_of == first.facts_as_of
    assert second.assessment_id != first.assessment_id
    assert second.already_assessed is False
    conn.close()
