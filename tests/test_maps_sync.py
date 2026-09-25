from __future__ import annotations

import json
from pathlib import Path

import pytest

from sara import maps_backfill as mb
from sara.maps_sync import MapsSyncError, sync_maps_business_understanding
from sara.migrations import apply_migrations
from sara.storage import connect as storage_connect, ingest_records
from sara.understanding_vocabulary import seed_business_understanding_vocabulary


def prepared_conn(path: Path):
    conn = storage_connect(path)
    assert apply_migrations(conn) == (1,)
    seed_business_understanding_vocabulary(conn)
    return conn


def add_run(conn, run_id: str, *, started_at: str) -> None:
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


def ingest_complete(conn, run_id: str, records: list[dict], *, started_at: str) -> None:
    add_run(conn, run_id, started_at=started_at)
    ingest_records(conn, run_id, records, finalize_run=("complete", 0, None))


def record(
    identity: str,
    *,
    place_id: str | None = None,
    cid: str | None = None,
    title: str | None = None,
    rating: float = 4.4,
) -> dict:
    return {
        "place_id": place_id if place_id is not None else f"place-{identity}",
        "cid": cid if cid is not None else f"cid-{identity}",
        "data_id": f"data-{identity}",
        "title": title or f"Business {identity}",
        "category": "Restaurant",
        "address": f"Street {identity}",
        "latitude": 21.55 + (0.001 if identity.endswith("b") else 0),
        "longitude": 39.18 + (0.001 if identity.endswith("b") else 0),
        "phone": "+966500000000",
        "website": "https://same.example",
        "review_rating": rating,
        "review_count": 120,
        "status": "Open",
        "link": f"https://maps.example/{identity}",
    }


def count(conn, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def test_sync_adds_new_maps_business_and_is_idempotent(tmp_path: Path) -> None:
    conn = prepared_conn(tmp_path / "sync.sqlite")
    ingest_complete(
        conn,
        "r1",
        [record("a")],
        started_at="2026-09-24T10:00:00+00:00",
    )
    mb.backfill_maps_business_understanding(conn)

    ingest_complete(
        conn,
        "r2",
        [record("b")],
        started_at="2026-09-25T10:00:00+00:00",
    )
    assert count(conn, "businesses") == 2
    assert count(conn, "maps_business_location_links") == 1

    stats = sync_maps_business_understanding(conn)

    assert stats.business_count == 2
    assert stats.entities_created == 1
    assert stats.locations_created == 1
    assert stats.links_created == 1
    assert stats.external_identifiers_created == 3
    assert stats.acquisition_sessions_created == 1
    assert stats.evidence_items_created == 1
    assert stats.observations_created == 10
    assert stats.facts_created == 10
    assert stats.already_synchronized is False
    assert count(conn, "maps_business_location_links") == 2
    assert count(conn, "business_entities") == 2
    assert count(conn, "business_locations") == 2
    assert list(conn.execute("PRAGMA foreign_key_check")) == []

    before = {
        table: count(conn, table)
        for table in (
            "knowledge_subjects",
            "business_entities",
            "business_locations",
            "maps_business_location_links",
            "external_identifiers",
            "acquisition_sessions",
            "evidence_items",
            "observations",
            "facts",
            "fact_observation_support",
        )
    }
    again = sync_maps_business_understanding(conn)
    assert again.already_synchronized is True
    assert all(
        getattr(again, field) == 0
        for field in (
            "entities_created",
            "locations_created",
            "links_created",
            "links_repointed",
            "locations_merged",
            "external_identifiers_created",
            "external_identifiers_refreshed",
            "acquisition_sessions_created",
            "evidence_items_created",
            "observations_created",
            "facts_created",
            "fact_support_links_created",
            "fact_updates_deferred",
        )
    )
    assert {table: count(conn, table) for table in before} == before


def test_sync_redirects_duplicate_location_after_maps_identity_convergence(
    tmp_path: Path,
) -> None:
    conn = prepared_conn(tmp_path / "convergence.sqlite")
    first = {
        "place_id": "place-a",
        "title": "Example",
        "latitude": 21.55,
        "longitude": 39.18,
    }
    second = {
        "cid": "cid-b",
        "title": "Example",
        "latitude": 21.551,
        "longitude": 39.181,
    }
    ingest_complete(
        conn,
        "r1",
        [first, second],
        started_at="2026-09-23T10:00:00+00:00",
    )
    mb.backfill_maps_business_understanding(conn)

    rows = list(conn.execute("SELECT id,place_id,cid FROM businesses ORDER BY id"))
    assert len(rows) == 2
    primary_business_id = int(rows[0]["id"])
    duplicate_business_id = int(rows[1]["id"])
    primary_location = mb.location_id_for_maps_business(primary_business_id)
    duplicate_location = mb.location_id_for_maps_business(duplicate_business_id)
    duplicate_obs_before = int(
        conn.execute(
            "SELECT COUNT(*) FROM observations WHERE subject_id=?",
            (duplicate_location,),
        ).fetchone()[0]
    )
    evidence_before = count(conn, "evidence_items")

    ingest_complete(
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
        started_at="2026-09-25T10:00:00+00:00",
    )
    assert count(conn, "businesses") == 1
    assert count(conn, "maps_business_location_links") == 1

    stats = sync_maps_business_understanding(conn)

    assert stats.locations_merged == 1
    subject = conn.execute(
        "SELECT record_state,merged_into_subject_id FROM knowledge_subjects WHERE id=?",
        (duplicate_location,),
    ).fetchone()
    assert tuple(subject) == ("merged", primary_location)
    link = conn.execute(
        "SELECT business_id,location_id FROM maps_business_location_links"
    ).fetchone()
    assert tuple(link) == (primary_business_id, primary_location)
    assert int(
        conn.execute(
            "SELECT COUNT(*) FROM observations WHERE subject_id=?",
            (duplicate_location,),
        ).fetchone()[0]
    ) == duplicate_obs_before
    assert count(conn, "evidence_items") == evidence_before + 1
    assert conn.execute(
        "SELECT subject_id FROM external_identifiers "
        "WHERE source_id=? AND namespace='cid' AND value='cid-b'",
        (mb.GOOGLE_MAPS_SOURCE_ID,),
    ).fetchone()[0] == duplicate_location
    assert list(conn.execute("PRAGMA foreign_key_check")) == []

    again = sync_maps_business_understanding(conn)
    assert again.already_synchronized is True
    assert again.locations_merged == 0


def test_sync_does_not_group_businesses_from_weak_similarity(tmp_path: Path) -> None:
    conn = prepared_conn(tmp_path / "weak.sqlite")
    ingest_complete(
        conn,
        "r1",
        [record("seed")],
        started_at="2026-09-23T10:00:00+00:00",
    )
    mb.backfill_maps_business_understanding(conn)
    ingest_complete(
        conn,
        "r2",
        [
            record("a", title="Same Brand"),
            record("b", title="Same Brand"),
        ],
        started_at="2026-09-25T10:00:00+00:00",
    )

    sync_maps_business_understanding(conn)

    assert count(conn, "businesses") == 3
    assert count(conn, "business_entities") == 3
    assert count(conn, "business_locations") == 3
    assert count(conn, "maps_business_location_links") == 3
    assert conn.execute(
        "SELECT COUNT(*) FROM knowledge_subjects WHERE record_state='merged'"
    ).fetchone()[0] == 0


def test_sync_versions_same_value_to_refresh_fact_verification_time(tmp_path: Path) -> None:
    conn = prepared_conn(tmp_path / "refresh.sqlite")
    ingest_complete(
        conn,
        "r1",
        [record("a", rating=4.4)],
        started_at="2026-09-23T10:00:00+00:00",
    )
    mb.backfill_maps_business_understanding(conn)
    business_id = int(conn.execute("SELECT id FROM businesses").fetchone()[0])
    location_id = mb.location_id_for_maps_business(business_id)
    old = conn.execute(
        "SELECT id,valid_from,last_verified_at FROM facts "
        "WHERE subject_id=? AND predicate='reputation.rating' AND valid_to IS NULL",
        (location_id,),
    ).fetchone()
    assert old is not None

    ingest_complete(
        conn,
        "r2",
        [record("a", rating=4.4)],
        started_at="2026-09-25T10:00:00+00:00",
    )
    stats = sync_maps_business_understanding(conn)

    assert stats.facts_created >= 1
    closed = conn.execute(
        "SELECT valid_to FROM facts WHERE id=?", (old["id"],)
    ).fetchone()[0]
    assert closed == "2026-09-25T10:00:00+00:00"
    current = conn.execute(
        "SELECT value_json,valid_from,last_verified_at,reconciliation_version "
        "FROM facts WHERE subject_id=? AND predicate='reputation.rating' "
        "AND valid_to IS NULL",
        (location_id,),
    ).fetchone()
    assert tuple(current) == (
        "4.4",
        "2026-09-25T10:00:00+00:00",
        "2026-09-25T10:00:00+00:00",
        "maps-sync-v1",
    )


def test_sync_defers_conflicting_non_sync_current_fact(tmp_path: Path) -> None:
    conn = prepared_conn(tmp_path / "defer.sqlite")
    ingest_complete(
        conn,
        "r1",
        [record("a", title="Maps Name")],
        started_at="2026-09-23T10:00:00+00:00",
    )
    mb.backfill_maps_business_understanding(conn)
    business_id = int(conn.execute("SELECT id FROM businesses").fetchone()[0])
    entity_id = mb.business_entity_id_for_maps_business(business_id)
    original = conn.execute(
        "SELECT id FROM facts WHERE subject_id=? AND predicate='business.name.trading' "
        "AND valid_to IS NULL",
        (entity_id,),
    ).fetchone()
    conn.execute(
        "UPDATE facts SET valid_to='2026-09-24T00:00:00+00:00' WHERE id=?",
        (original["id"],),
    )
    manual_json = json.dumps("Verified Name", separators=(",", ":"))
    manual_hash = mb._sha256_text(manual_json)
    conn.execute(
        "INSERT INTO facts("
        "id,subject_id,predicate,fact_slot,value_json,normalized_value_json,value_hash,status,"
        "valid_from,valid_to,last_verified_at,reconciled_at,reconciliation_version,created_at"
        ") VALUES ('fact_manual_name',?,'business.name.trading','__single__',?,?,?,'confirmed',"
        "'2026-09-24T00:00:00+00:00',NULL,'2026-09-24T00:00:00+00:00',"
        "'2026-09-24T00:00:00+00:00','human-v1','2026-09-24T00:00:00+00:00')",
        (entity_id, manual_json, manual_json, manual_hash),
    )
    conn.commit()

    ingest_complete(
        conn,
        "r2",
        [record("a", title="Maps Name Changed")],
        started_at="2026-09-25T10:00:00+00:00",
    )
    stats = sync_maps_business_understanding(conn)

    assert stats.fact_updates_deferred >= 1
    current = conn.execute(
        "SELECT id,value_json,reconciliation_version FROM facts "
        "WHERE subject_id=? AND predicate='business.name.trading' AND valid_to IS NULL",
        (entity_id,),
    ).fetchone()
    assert tuple(current) == ("fact_manual_name", manual_json, "human-v1")
    sync_obs = conn.execute(
        "SELECT COUNT(*) FROM observations WHERE subject_id=? "
        "AND predicate='business.name.trading' AND extractor_name='sara.maps_sync'",
        (entity_id,),
    ).fetchone()[0]
    assert sync_obs == 1


def test_sync_rolls_back_all_new_understanding_state_on_late_failure(tmp_path: Path) -> None:
    conn = prepared_conn(tmp_path / "rollback.sqlite")
    ingest_complete(
        conn,
        "r1",
        [record("seed")],
        started_at="2026-09-23T10:00:00+00:00",
    )
    mb.backfill_maps_business_understanding(conn)
    ingest_complete(
        conn,
        "r2",
        [record("a"), record("b")],
        started_at="2026-09-25T10:00:00+00:00",
    )
    bad_id = int(
        conn.execute("SELECT MAX(id) FROM businesses").fetchone()[0]
    )
    conn.execute("UPDATE businesses SET raw_json='not-json' WHERE id=?", (bad_id,))
    conn.commit()
    before = {
        table: count(conn, table)
        for table in (
            "knowledge_subjects",
            "business_entities",
            "business_locations",
            "maps_business_location_links",
            "external_identifiers",
            "acquisition_sessions",
            "evidence_items",
            "observations",
            "facts",
        )
    }

    with pytest.raises(MapsSyncError, match="raw_json is malformed"):
        sync_maps_business_understanding(conn)

    assert {table: count(conn, table) for table in before} == before
    assert list(conn.execute("PRAGMA foreign_key_check")) == []
