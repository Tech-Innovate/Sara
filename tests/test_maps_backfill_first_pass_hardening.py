from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from sara import maps_backfill as mb
from sara.migrations import apply_migrations
from sara.storage import connect as storage_connect, ingest_records
from sara.understanding_vocabulary import seed_business_understanding_vocabulary


def prepared_conn(path: Path):
    conn = storage_connect(path)
    assert apply_migrations(conn) == (1,)
    seed_business_understanding_vocabulary(conn)
    return conn


def record() -> dict:
    return {
        "place_id": "place-a",
        "cid": "cid-a",
        "data_id": "data-a",
        "title": "Example Restaurant",
        "category": "Restaurant",
        "address": "Example Street",
        "latitude": 21.55,
        "longitude": 39.18,
        "phone": "+966500000000",
        "website": "https://example.test",
        "review_rating": 4.4,
        "review_count": 120,
        "status": "Open",
        "link": "https://maps.example/a",
    }


def add_complete_business(conn) -> int:
    conn.execute(
        "INSERT INTO runs("
        "id,area_name,bbox_json,cell_km,depth,queries_json,scraper_image,config_json,"
        "raw_path,status,started_at"
        ") VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            "r1",
            "test-area",
            '{"max_lat":22,"max_lon":40,"min_lat":21,"min_lon":39}',
            2.0,
            1,
            '["restaurant"]',
            "gosom/google-maps-scraper:v1.18.1",
            '{"strict_bounds":true}',
            "/evidence/r1.jsonl",
            "running",
            "2026-09-25T10:00:00+00:00",
        ),
    )
    conn.commit()
    ingest_records(conn, "r1", [record()], finalize_run=("complete", 0, None))
    return int(conn.execute("SELECT id FROM businesses").fetchone()[0])


def test_rerun_rejects_anchor_only_state_without_phase3_provenance(tmp_path: Path) -> None:
    conn = prepared_conn(tmp_path / "anchor-only.sqlite")
    business_id = add_complete_business(conn)
    entity_id = mb.business_entity_id_for_maps_business(business_id)
    location_id = mb.location_id_for_maps_business(business_id)
    created_at = "2026-09-25T12:00:00+00:00"

    conn.execute(
        "INSERT INTO knowledge_subjects(id,kind,created_at,updated_at) VALUES (?,?,?,?)",
        (entity_id, "business_entity", created_at, created_at),
    )
    conn.execute(
        "INSERT INTO business_entities("
        "id,display_name,entity_type,lifecycle_status,created_at,updated_at"
        ") VALUES (?,?,'unknown','unknown',?,?)",
        (entity_id, "Example Restaurant", created_at, created_at),
    )
    conn.execute(
        "INSERT INTO knowledge_subjects(id,kind,created_at,updated_at) VALUES (?,?,?,?)",
        (location_id, "location", created_at, created_at),
    )
    conn.execute(
        "INSERT INTO business_locations("
        "id,business_entity_id,location_type,created_at,updated_at"
        ") VALUES (?,?,'unknown',?,?)",
        (location_id, entity_id, created_at, created_at),
    )
    conn.execute(
        "INSERT INTO maps_business_location_links(business_id,location_id,linked_at) "
        "VALUES (?,?,?)",
        (business_id, location_id, created_at),
    )
    conn.commit()

    with pytest.raises(mb.MapsBackfillError, match="backfill source registry drift"):
        mb.backfill_maps_business_understanding(conn)

    assert conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM evidence_items").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 0


def test_rerun_rejects_retained_evidence_with_zero_observations_and_facts(tmp_path: Path) -> None:
    conn = prepared_conn(tmp_path / "zero-provenance.sqlite")
    add_complete_business(conn)
    mb.backfill_maps_business_understanding(conn)

    assert conn.execute("SELECT COUNT(*) FROM evidence_items").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 10
    assert conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 10

    conn.execute("DELETE FROM fact_observation_support")
    conn.execute("DELETE FROM facts")
    conn.execute("DELETE FROM observations")
    conn.commit()

    with pytest.raises(
        mb.MapsBackfillError,
        match="observation provenance is incomplete or inconsistent",
    ):
        mb.backfill_maps_business_understanding(conn)

    assert conn.execute("SELECT COUNT(*) FROM evidence_items").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 0


def test_rerun_rejects_equal_counts_with_substituted_provenance_value(tmp_path: Path) -> None:
    conn = prepared_conn(tmp_path / "substituted-provenance.sqlite")
    add_complete_business(conn)
    mb.backfill_maps_business_understanding(conn)

    observation_id = str(
        conn.execute("SELECT id FROM observations ORDER BY id LIMIT 1").fetchone()[0]
    )
    fact_id = str(
        conn.execute(
            "SELECT fact_id FROM fact_observation_support WHERE observation_id=?",
            (observation_id,),
        ).fetchone()[0]
    )
    replacement_json = json.dumps(
        "substituted", ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    replacement_hash = hashlib.sha256(replacement_json.encode("utf-8")).hexdigest()
    conn.execute(
        "UPDATE observations SET value_json=?,normalized_value_json=?,value_hash=? WHERE id=?",
        (replacement_json, replacement_json, replacement_hash, observation_id),
    )
    conn.execute(
        "UPDATE facts SET value_json=?,normalized_value_json=?,value_hash=? WHERE id=?",
        (replacement_json, replacement_json, replacement_hash, fact_id),
    )
    conn.commit()

    assert conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 10
    assert conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 10
    assert conn.execute("SELECT COUNT(*) FROM fact_observation_support").fetchone()[0] == 10

    with pytest.raises(
        mb.MapsBackfillError,
        match="observation provenance is incomplete or inconsistent",
    ):
        mb.backfill_maps_business_understanding(conn)


def test_rerun_accepts_original_fact_after_later_version_closure(tmp_path: Path) -> None:
    conn = prepared_conn(tmp_path / "closed-fact-version.sqlite")
    add_complete_business(conn)
    mb.backfill_maps_business_understanding(conn)

    conn.execute("UPDATE facts SET valid_to='2026-09-26T00:00:00+00:00'")
    conn.commit()

    rerun = mb.backfill_maps_business_understanding(conn)
    assert rerun.already_backfilled is True
    assert rerun.business_count == 1
    assert rerun.facts_created == 0


def test_external_identifier_timestamps_record_import_observation_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = prepared_conn(tmp_path / "identifier-time.sqlite")
    add_complete_business(conn)
    imported_at = "2026-09-25T20:00:00+00:00"
    monkeypatch.setattr(mb, "_utc_now", lambda: imported_at)

    stats = mb.backfill_maps_business_understanding(conn)

    assert stats.external_identifiers_created == 3
    rows = list(
        conn.execute(
            "SELECT first_observed_at,last_observed_at,created_at "
            "FROM external_identifiers ORDER BY namespace"
        )
    )
    assert rows
    assert {tuple(row) for row in rows} == {(imported_at, imported_at, imported_at)}
