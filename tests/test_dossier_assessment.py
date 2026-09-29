from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from sara.dossier import (
    DossierAssessmentError,
    build_business_dossier,
    persist_dossier_assessment,
)
from sara.dossier.assessment_cli import main as assessment_main
from sara.maps_backfill import backfill_maps_business_understanding
from sara.migrations import MIGRATIONS, apply_migrations
from sara.reviews import extract_retained_reviews
from sara.storage import connect, connect_readonly, ingest_records
from sara.understanding_vocabulary import DOSSIER_DOMAIN_SEED_V1, seed_business_understanding_vocabulary


def _prepared(path: Path, *, reviews: bool = False, migrations=MIGRATIONS):
    conn = connect(path)
    assert apply_migrations(conn, migrations=migrations) == tuple(
        migration.version for migration in migrations
    )
    seed_business_understanding_vocabulary(conn)
    conn.execute(
        "INSERT INTO runs("
        "id,area_name,bbox_json,cell_km,depth,queries_json,scraper_image,config_json,"
        "raw_path,status,started_at"
        ") VALUES (?,?,?,?,?,?,?,?,?,'running',?)",
        (
            "r1",
            "assessment-test",
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
        "place_id": "place-assessment",
        "cid": "cid-assessment",
        "data_id": "data-assessment",
        "title": "Assessment Restaurant",
        "category": "Restaurant",
        "address": "Assessment Street",
        "latitude": 21.55,
        "longitude": 39.18,
        "phone": "+966500000000",
        "website": "https://assessment.example",
        "review_rating": 4.6,
        "review_count": 91,
        "status": "Open",
        "link": "https://maps.example/assessment",
    }
    if reviews:
        record["user_reviews"] = [
            {
                "review_id": "review-assessment-1",
                "source": "Google",
                "Rating": 5,
                "Description": "Excellent service",
                "language": "en",
                "posted_at_unix_micros": 1_758_758_400_000_000,
                "Name": "Sensitive Reviewer Display Name",
                "ProfilePicture": "https://profiles.example/sensitive.jpg",
            }
        ]
    with patch("sara.storage.utc_now", return_value="2026-09-26T09:59:00+00:00"):
        ingest_records(conn, "r1", [record], finalize_run=("complete", 0, None))
    with patch("sara.maps_backfill._utc_now", return_value="2026-09-26T10:00:00+00:00"):
        backfill_maps_business_understanding(conn)
    return conn


def _states(result) -> dict[str, str]:
    return {item["domain"]: item["state"] for item in result.domains}


def test_assessment_seals_complete_policy_snapshot_and_is_conservative(tmp_path: Path) -> None:
    conn = _prepared(tmp_path / "assessment.sqlite")
    result = persist_dossier_assessment(
        conn,
        business_id=1,
        now=lambda: "2026-09-28T10:00:00+00:00",
    )

    states = _states(result)
    assert result.analysis_ready is False
    assert states["identity"] == "sufficient"
    assert states["classification"] == "sufficient"
    assert states["locations"] == "sufficient"
    assert states["communication"] == "sufficient"
    assert states["digital_presence"] == "sufficient"
    assert states["reputation"] == "partial"
    assert states["offerings"] == "not_started"
    assert states["business_model"] == "not_started"
    assert states["customer_journey"] == "partial"
    assert states["competitive_context"] == "not_started"
    assert states["provenance"] == "sufficient"
    assert states["unknowns"] == "sufficient"
    assert {"offerings", "business_model", "customer_journey", "reputation", "competitive_context"} <= set(
        result.blocking_mandatory_domains
    )

    assert conn.execute("SELECT COUNT(*) FROM dossier_assessments").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM dossier_assessment_seals").fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM dossier_domain_assessments WHERE assessment_id=?",
        (result.assessment_id,),
    ).fetchone()[0] == len(DOSSIER_DOMAIN_SEED_V1)
    assert list(conn.execute("PRAGMA foreign_key_check")) == []
    conn.close()


def test_v1_non_active_external_identifier_migrates_without_fabricated_chronology(
    tmp_path: Path,
) -> None:
    conn = _prepared(
        tmp_path / "legacy-non-active-identifier.sqlite",
        migrations=MIGRATIONS[:1],
    )
    identifier_id = conn.execute(
        "SELECT id FROM external_identifiers WHERE status='active' ORDER BY id LIMIT 1"
    ).fetchone()[0]
    conn.execute(
        "UPDATE external_identifiers SET status='retired' WHERE id=?",
        (identifier_id,),
    )
    conn.commit()

    assert apply_migrations(conn) == (2,)
    row = conn.execute(
        "SELECT status,status_changed_at FROM external_identifiers WHERE id=?",
        (identifier_id,),
    ).fetchone()
    assert tuple(row) == ("retired", None)

    result = persist_dossier_assessment(
        conn,
        business_id=1,
        now=lambda: "2026-09-28T10:00:00+00:00",
    )
    assert result.assessment_id
    assert conn.execute("SELECT COUNT(*) FROM dossier_assessments").fetchone()[0] == 1
    row = conn.execute(
        "SELECT status,status_changed_at FROM external_identifiers WHERE id=?",
        (identifier_id,),
    ).fetchone()
    assert tuple(row) == ("retired", None)
    conn.close()


def test_same_logical_state_reuses_deterministic_sealed_snapshot(tmp_path: Path) -> None:
    conn = _prepared(tmp_path / "idempotent.sqlite")
    first = persist_dossier_assessment(
        conn,
        business_id=1,
        now=lambda: "2026-09-28T10:00:00+00:00",
    )
    second = persist_dossier_assessment(
        conn,
        business_id=1,
        now=lambda: "2026-09-29T10:00:00+00:00",
    )

    assert second.assessment_id == first.assessment_id
    assert second.facts_as_of == first.facts_as_of
    assert second.computed_at == first.computed_at
    assert second.already_assessed is True
    assert conn.execute("SELECT COUNT(*) FROM dossier_assessments").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM dossier_domain_assessments").fetchone()[0] == len(
        DOSSIER_DOMAIN_SEED_V1
    )
    conn.close()


def test_freshness_transition_creates_new_snapshot_without_rewriting_history(tmp_path: Path) -> None:
    conn = _prepared(tmp_path / "freshness.sqlite")
    fresh = persist_dossier_assessment(
        conn,
        business_id=1,
        now=lambda: "2026-09-28T10:00:00+00:00",
    )
    stale = persist_dossier_assessment(
        conn,
        business_id=1,
        now=lambda: "2027-09-28T10:00:00+00:00",
    )

    assert stale.assessment_id != fresh.assessment_id
    assert stale.facts_as_of == fresh.facts_as_of
    assert _states(stale)["identity"] == "stale"
    assert conn.execute("SELECT COUNT(*) FROM dossier_assessments").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM dossier_assessment_seals").fetchone()[0] == 2
    old = conn.execute(
        "SELECT analysis_ready,computed_at FROM dossier_assessments WHERE id=?",
        (fresh.assessment_id,),
    ).fetchone()
    assert tuple(old) == (0, "2026-09-28T10:00:00+00:00")
    conn.close()


def test_review_observations_project_as_customer_voice_without_profile_metadata(tmp_path: Path) -> None:
    db = tmp_path / "reviews.sqlite"
    conn = _prepared(db, reviews=True)
    extraction = extract_retained_reviews(
        conn,
        business_id=1,
        now=lambda: "2026-09-27T12:00:00+00:00",
    )
    assert extraction.observations_created == 1

    dossier = build_business_dossier(
        conn,
        business_id=1,
        evaluated_at="2026-09-28T10:00:00+00:00",
    )
    assert dossier["customer_voice"]["review_count"] == 1
    review = dossier["customer_voice"]["reviews"][0]
    assert review["normalized_value"]["review_id"] == "review-assessment-1"
    assert review["normalized_value"]["text_original"] == "Excellent service"
    assert "Sensitive Reviewer Display Name" not in json.dumps(
        dossier["customer_voice"], ensure_ascii=False, sort_keys=True
    )
    assert "profiles.example" not in json.dumps(
        dossier["customer_voice"], ensure_ascii=False, sort_keys=True
    )
    assert conn.execute(
        "SELECT COUNT(*) FROM facts WHERE predicate='reputation.customer_review'"
    ).fetchone()[0] == 0

    assessment = persist_dossier_assessment(
        conn,
        business_id=1,
        now=lambda: "2026-09-28T10:00:00+00:00",
    )
    assert _states(assessment)["reputation"] == "sufficient"
    assert assessment.analysis_ready is False
    conn.close()


def test_impossible_assessment_clock_rolls_back_all_writes(tmp_path: Path) -> None:
    conn = _prepared(tmp_path / "rollback.sqlite")
    with pytest.raises(DossierAssessmentError, match="later than the assessment clock"):
        persist_dossier_assessment(
            conn,
            business_id=1,
            now=lambda: "2020-01-01T00:00:00+00:00",
        )
    assert conn.in_transaction is False
    assert conn.execute("SELECT COUNT(*) FROM dossier_assessments").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM dossier_domain_assessments").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM dossier_assessment_seals").fetchone()[0] == 0
    conn.close()


def test_assessment_cli_writes_only_explicit_assessment_state(tmp_path: Path, capsys) -> None:
    db = tmp_path / "cli.sqlite"
    conn = _prepared(db)
    before_maps = conn.execute("SELECT COUNT(*) FROM businesses").fetchone()[0]
    before_facts = conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
    conn.close()

    rc = assessment_main(["--db", str(db), "--business-id", "1"])
    captured = capsys.readouterr()
    assert rc == 0
    payload = json.loads(captured.out)
    assert payload["schema"] == "sara-dossier-assessment-result-v1"
    assert payload["analysis_ready"] is False
    assert payload["already_assessed"] is False

    check = connect(db)
    assert check.execute("SELECT COUNT(*) FROM businesses").fetchone()[0] == before_maps
    assert check.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == before_facts
    assert check.execute("SELECT COUNT(*) FROM dossier_assessments").fetchone()[0] == 1
    check.close()


def test_readonly_dossier_surfaces_latest_sealed_assessment_after_writer(tmp_path: Path) -> None:
    db = tmp_path / "reader.sqlite"
    conn = _prepared(db)
    written = persist_dossier_assessment(
        conn,
        business_id=1,
        now=lambda: "2026-09-28T10:00:00+00:00",
    )
    conn.close()

    ro = connect_readonly(db)
    dossier = build_business_dossier(
        ro,
        business_id=1,
        evaluated_at="2026-09-28T11:00:00+00:00",
    )
    ro.close()
    persisted = dossier["dossier_status"]["persisted_current_policy"]
    assert persisted["id"] == written.assessment_id
    assert persisted["analysis_ready"] is False
    assert len(persisted["domains"]) == len(DOSSIER_DOMAIN_SEED_V1)


def test_assessment_on_v1_only_schema_fails_closed(tmp_path: Path) -> None:
    """A database migrated only through v1 refuses assessment cleanly."""
    conn = _prepared(
        tmp_path / "assessment-v1-only.sqlite",
        migrations=MIGRATIONS[:1],
    )
    try:
        with pytest.raises(DossierAssessmentError, match="schema v2"):
            persist_dossier_assessment(
                conn,
                business_id=1,
                now=lambda: "2026-09-28T10:00:00+00:00",
            )
        assert conn.execute("SELECT COUNT(*) FROM dossier_assessments").fetchone()[0] == 0
        assert not conn.in_transaction
    finally:
        conn.close()


def test_dossier_reader_on_v1_only_schema_fails_closed(tmp_path: Path) -> None:
    """The read-only dossier surface refuses a v1-only database cleanly."""
    from sara.dossier import build_business_dossier
    from sara.dossier.core import DossierQueryError

    conn = _prepared(
        tmp_path / "reader-v1-only.sqlite",
        migrations=MIGRATIONS[:1],
    )
    try:
        with pytest.raises(DossierQueryError, match="schema v2"):
            build_business_dossier(conn, business_id=1)
        assert not conn.in_transaction
    finally:
        conn.close()
