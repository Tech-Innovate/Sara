from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from sara.dossier import DossierAssessmentError, build_business_dossier, persist_dossier_assessment
from sara.maps_backfill import backfill_maps_business_understanding, location_id_for_maps_business
from sara.migrations import apply_migrations
from sara.reviews import extract_retained_reviews
from sara.storage import connect, ingest_records
from sara.understanding_vocabulary import seed_business_understanding_vocabulary


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


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
            "codex-reconciliation-round3",
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
        "place_id": "place-codex-round3",
        "cid": "cid-codex-round3",
        "data_id": "data-codex-round3",
        "title": "Codex Round 3 Restaurant",
        "category": "Restaurant",
        "address": "Round 3 Street",
        "latitude": 21.55,
        "longitude": 39.18,
        "phone": "+966500000000",
        "website": "https://round3.example",
        "review_rating": 4.6,
        "review_count": 17,
        "status": "Open",
        "link": "https://maps.example/round3",
    }
    if with_review:
        record["user_reviews"] = [
            {
                "review_id": "round3-review-1",
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


def test_hash_valid_review_schema_extension_is_rejected_without_exposing_pii(
    tmp_path: Path,
) -> None:
    conn = _prepared(tmp_path / "review-schema-boundary.sqlite", with_review=True)
    observation_id, evidence_id, normalized_text = conn.execute(
        "SELECT id,evidence_id,normalized_value_json FROM observations "
        "WHERE predicate='reputation.customer_review'"
    ).fetchone()
    normalized = json.loads(normalized_text)
    normalized["reviewer_name"] = "Sensitive Reviewer Name"
    normalized["reviewer_profile_url"] = "https://profiles.example/private-reviewer"
    tampered_text = _canonical_json(normalized)

    conn.execute("DROP TRIGGER observations_immutable")
    conn.execute(
        "UPDATE observations SET normalized_value_json=?,value_hash=? WHERE id=?",
        (tampered_text, _sha256(tampered_text), observation_id),
    )
    conn.commit()

    dossier = build_business_dossier(
        conn,
        business_id=1,
        evaluated_at="2026-09-28T10:00:00+00:00",
    )
    assert dossier["customer_voice"]["review_count"] == 0
    assert any(
        issue["code"] == "customer_review_normalized_schema_invalid"
        and issue["observation_id"] == observation_id
        and issue["evidence_id"] == evidence_id
        for issue in dossier["integrity_issues"]
    )
    serialized = _canonical_json(dossier)
    assert "Sensitive Reviewer Name" not in serialized
    assert "profiles.example" not in serialized

    assessment = persist_dossier_assessment(
        conn,
        business_id=1,
        now=lambda: "2026-09-28T10:00:00+00:00",
    )
    assert assessment.analysis_ready is False
    conn.close()


def test_identifier_status_change_requires_transition_timestamp(tmp_path: Path) -> None:
    conn = _prepared(tmp_path / "identifier-status-trigger.sqlite")
    location_id = location_id_for_maps_business(1)
    with pytest.raises(sqlite3.IntegrityError, match="require a new transition timestamp"):
        conn.execute(
            "UPDATE external_identifiers SET status='retired' "
            "WHERE subject_id=? AND namespace='place_id'",
            (location_id,),
        )
    conn.rollback()
    conn.close()


def test_identifier_status_transition_participates_in_assessment_chronology(
    tmp_path: Path,
) -> None:
    conn = _prepared(tmp_path / "identifier-status-future.sqlite")
    location_id = location_id_for_maps_business(1)
    conn.execute(
        "UPDATE external_identifiers SET status='retired',status_changed_at=? "
        "WHERE subject_id=? AND namespace='place_id'",
        ("2026-09-30T13:00:00+00:00", location_id),
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


def test_nonactive_identifier_without_transition_timestamp_is_unknown_legacy_chronology(
    tmp_path: Path,
) -> None:
    conn = _prepared(tmp_path / "identifier-status-legacy-null.sqlite")
    location_id = location_id_for_maps_business(1)
    conn.execute("DROP TRIGGER external_identifiers_status_transition_timestamp")
    conn.execute(
        "UPDATE external_identifiers SET status='retired' "
        "WHERE subject_id=? AND namespace='place_id'",
        (location_id,),
    )
    conn.commit()

    assessment = persist_dossier_assessment(
        conn,
        business_id=1,
        now=lambda: "2026-09-28T10:00:00+00:00",
    )
    assert assessment.assessment_id
    row = conn.execute(
        "SELECT status,status_changed_at FROM external_identifiers "
        "WHERE subject_id=? AND namespace='place_id'",
        (location_id,),
    ).fetchone()
    assert tuple(row) == ("retired", None)
    conn.close()


def test_fact_freshness_policy_changes_deterministic_assessment_identity(
    tmp_path: Path,
) -> None:
    conn = _prepared(tmp_path / "freshness-policy-signature.sqlite")
    entity_id = str(conn.execute("SELECT id FROM business_entities").fetchone()[0])
    location_id = location_id_for_maps_business(1)
    stable_later_watermark = "2026-09-29T00:00:00+00:00"
    conn.execute(
        "UPDATE external_identifiers SET last_observed_at=? "
        "WHERE subject_id=? AND namespace='place_id' AND status='active'",
        (stable_later_watermark, location_id),
    )
    conn.execute(
        "INSERT INTO predicate_definitions("
        "name,domain,subject_kind,value_type,cardinality,reconciliation_policy,"
        "freshness_days,description,active"
        ") VALUES ("
        "'business.custom_freshness_probe','offerings','business_entity','text','single',"
        "'latest_official',30,'test-only custom controlled predicate',1"
        ")"
    )
    value_text = _canonical_json("probe")
    conn.execute(
        "INSERT INTO facts("
        "id,subject_id,predicate,fact_slot,value_json,normalized_value_json,value_hash,status,"
        "valid_from,valid_to,last_verified_at,reconciled_at,reconciliation_version,created_at"
        ") VALUES (?,?,?,'__single__',?,?,?,'single_source',?,NULL,?,?,?,?)",
        (
            "fact_custom_freshness_probe",
            entity_id,
            "business.custom_freshness_probe",
            value_text,
            value_text,
            _sha256(value_text),
            "2026-09-27T10:00:00+00:00",
            "2026-09-27T10:00:00+00:00",
            "2026-09-27T10:00:00+00:00",
            "test-v1",
            "2026-09-27T10:00:00+00:00",
        ),
    )
    conn.commit()

    first = persist_dossier_assessment(
        conn,
        business_id=1,
        now=lambda: "2026-09-30T10:00:00+00:00",
    )
    assert first.facts_as_of == stable_later_watermark

    conn.execute(
        "UPDATE predicate_definitions SET freshness_days=60 "
        "WHERE name='business.custom_freshness_probe'"
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
