from __future__ import annotations

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
