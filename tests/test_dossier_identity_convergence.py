from __future__ import annotations

from pathlib import Path

from sara import maps_backfill as mb
from sara.dossier import build_business_dossier
from sara.maps_sync import sync_maps_business_understanding
from sara.migrations import apply_migrations
from sara.storage import connect, connect_readonly, ingest_records
from sara.understanding_vocabulary import seed_business_understanding_vocabulary


def _prepared(path: Path):
    conn = connect(path)
    assert apply_migrations(conn) == (1,)
    seed_business_understanding_vocabulary(conn)
    return conn


def _add_run(conn, run_id: str, started_at: str) -> None:
    conn.execute(
        "INSERT INTO runs("
        "id,area_name,bbox_json,cell_km,depth,queries_json,scraper_image,config_json,"
        "raw_path,status,started_at"
        ") VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            run_id,
            "test-area",
            '{"max_lat":22,"max_lon":40,"min_lat":21,"min_lon":39}',
            2.0,
            1,
            '["restaurant"]',
            "gosom/google-maps-scraper:v1.18.1",
            '{"strict_bounds":true}',
            f"/evidence/{run_id}.jsonl",
            "running",
            started_at,
        ),
    )
    conn.commit()


def _ingest(conn, run_id: str, records: list[dict], started_at: str) -> None:
    _add_run(conn, run_id, started_at)
    ingest_records(conn, run_id, records, finalize_run=("complete", 0, None))


def test_dossier_exposes_identifiers_on_inbound_merged_location_alias(tmp_path: Path) -> None:
    db = tmp_path / "dossier-convergence.sqlite"
    conn = _prepared(db)
    _ingest(
        conn,
        "r1",
        [
            {
                "place_id": "place-a",
                "title": "Example",
                "latitude": 21.55,
                "longitude": 39.18,
            },
            {
                "cid": "cid-b",
                "title": "Example",
                "latitude": 21.551,
                "longitude": 39.181,
            },
        ],
        "2026-09-23T10:00:00+00:00",
    )
    mb.backfill_maps_business_understanding(conn)

    rows = list(conn.execute("SELECT id,place_id,cid FROM businesses ORDER BY id"))
    primary_business_id = int(rows[0]["id"])
    duplicate_business_id = int(rows[1]["id"])
    primary_location = mb.location_id_for_maps_business(primary_business_id)
    duplicate_location = mb.location_id_for_maps_business(duplicate_business_id)

    _ingest(
        conn,
        "r2",
        [
            {
                "place_id": "place-a",
                "cid": "cid-b",
                "title": "Example",
                "latitude": 21.55,
                "longitude": 39.18,
            }
        ],
        "2026-09-25T10:00:00+00:00",
    )
    sync_maps_business_understanding(conn)
    assert conn.execute(
        "SELECT merged_into_subject_id FROM knowledge_subjects WHERE id=?",
        (duplicate_location,),
    ).fetchone()[0] == primary_location
    assert conn.execute(
        "SELECT subject_id FROM external_identifiers "
        "WHERE source_id=? AND namespace='cid' AND value='cid-b'",
        (mb.GOOGLE_MAPS_SOURCE_ID,),
    ).fetchone()[0] == duplicate_location
    conn.close()

    ro = connect_readonly(db)
    dossier = build_business_dossier(
        ro,
        business_id=primary_business_id,
        evaluated_at="2026-09-26T00:00:00+00:00",
    )
    ro.close()

    by_id = {location["id"]: location for location in dossier["locations"]}
    assert set(by_id) == {primary_location, duplicate_location}
    assert by_id[primary_location]["relationship_to_entity"] == "current"
    assert by_id[primary_location]["current_for_entity"] is True
    assert by_id[duplicate_location]["relationship_to_entity"] == "merged_alias"
    assert by_id[duplicate_location]["current_for_entity"] is False
    assert by_id[duplicate_location]["canonical_location_id"] == primary_location
    assert {(
        identifier["namespace"], identifier["value"]
    ) for identifier in by_id[duplicate_location]["external_identifiers"]} >= {("cid", "cid-b")}
    assert dossier["maps_businesses"][0]["id"] == primary_business_id
    assert all(
        fact["subject_id"] != duplicate_location for fact in dossier["facts"]
    )
