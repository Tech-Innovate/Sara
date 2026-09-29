from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from sara.dossier import DossierAssessmentError, persist_dossier_assessment
from sara.maps_backfill import backfill_maps_business_understanding, location_id_for_maps_business
from sara.migrations import apply_migrations
from sara.storage import connect, ingest_records
from sara.understanding_vocabulary import seed_business_understanding_vocabulary


def _prepared(path: Path):
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
            "provenance-creation-chronology",
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
                "place_id": "place-provenance-created",
                "cid": "cid-provenance-created",
                "data_id": "data-provenance-created",
                "title": "Provenance Creation Restaurant",
                "category": "Restaurant",
                "address": "Provenance Creation Street",
                "latitude": 21.55,
                "longitude": 39.18,
                "phone": "+966500000000",
                "website": "https://provenance-created.example",
                "review_rating": 4.6,
                "review_count": 17,
                "status": "Open",
                "link": "https://maps.example/provenance-created",
            }
        ],
        finalize_run=("complete", 0, None),
    )
    conn.execute(
        "UPDATE runs SET finished_at = '2026-09-26T09:59:00+00:00' "
        "WHERE id = 'r1' AND finished_at > '2026-09-26T09:59:00+00:00'"
    )
    conn.commit()
    with patch("sara.maps_backfill._utc_now", return_value="2026-09-26T10:00:00+00:00"):
        backfill_maps_business_understanding(conn)
    return conn


def _fact_provenance_ids(conn) -> tuple[str, str]:
    row = conn.execute(
        "SELECT o.id,e.id FROM facts f "
        "JOIN fact_observation_support fos ON fos.fact_id=f.id "
        "JOIN observations o ON o.id=fos.observation_id "
        "JOIN evidence_items e ON e.id=o.evidence_id "
        "WHERE f.predicate='business.name.trading' AND f.valid_to IS NULL"
    ).fetchone()
    assert row is not None
    return str(row[0]), str(row[1])


def _assert_future_input_rolls_back(conn) -> None:
    with pytest.raises(DossierAssessmentError, match="later than the assessment clock"):
        persist_dossier_assessment(
            conn,
            business_id=1,
            now=lambda: "2026-09-28T10:00:00+00:00",
        )
    assert conn.in_transaction is False
    assert conn.execute("SELECT COUNT(*) FROM dossier_assessments").fetchone()[0] == 0


def test_material_source_created_at_participates_in_assessment_chronology(
    tmp_path: Path,
) -> None:
    conn = _prepared(tmp_path / "source-created-at-future.sqlite")
    conn.execute("DROP TRIGGER sources_identity_immutable")
    conn.execute(
        "UPDATE sources SET created_at='2026-09-30T13:00:00+00:00' "
        "WHERE id='src_google_maps'"
    )
    conn.commit()

    _assert_future_input_rolls_back(conn)
    conn.close()


def test_material_evidence_created_at_participates_in_assessment_chronology(
    tmp_path: Path,
) -> None:
    conn = _prepared(tmp_path / "evidence-created-at-future.sqlite")
    _observation_id, evidence_id = _fact_provenance_ids(conn)
    conn.execute("DROP TRIGGER evidence_items_immutable")
    conn.execute(
        "UPDATE evidence_items SET created_at='2026-09-30T13:00:00+00:00' WHERE id=?",
        (evidence_id,),
    )
    conn.commit()

    _assert_future_input_rolls_back(conn)
    conn.close()


def test_material_observation_created_at_participates_in_assessment_chronology(
    tmp_path: Path,
) -> None:
    conn = _prepared(tmp_path / "observation-created-at-future.sqlite")
    observation_id, _evidence_id = _fact_provenance_ids(conn)
    conn.execute("DROP TRIGGER observations_immutable")
    conn.execute(
        "UPDATE observations SET created_at='2026-09-30T13:00:00+00:00' WHERE id=?",
        (observation_id,),
    )
    conn.commit()

    _assert_future_input_rolls_back(conn)
    conn.close()


def test_maps_business_first_seen_at_participates_in_assessment_chronology(
    tmp_path: Path,
) -> None:
    conn = _prepared(tmp_path / "maps-first-seen-at-future.sqlite")
    conn.execute(
        "UPDATE businesses SET first_seen_at='2026-09-30T13:00:00+00:00' WHERE id=1"
    )
    conn.commit()

    _assert_future_input_rolls_back(conn)
    conn.close()


def test_provenance_created_at_changes_signature_below_stable_watermark(
    tmp_path: Path,
) -> None:
    conn = _prepared(tmp_path / "provenance-created-at-signature.sqlite")
    observation_id, _evidence_id = _fact_provenance_ids(conn)
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

    conn.execute("DROP TRIGGER observations_immutable")
    conn.execute(
        "UPDATE observations SET created_at='2026-09-28T12:00:00+00:00' WHERE id=?",
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
