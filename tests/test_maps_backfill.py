from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from sara.maps_backfill import (
    GOOGLE_MAPS_SOURCE_ID,
    MapsBackfillError,
    backfill_maps_business_understanding,
    business_entity_id_for_maps_business,
    location_id_for_maps_business,
)
from sara.migrations import apply_migrations
from sara.storage import connect as storage_connect, ingest_records
from sara.understanding_vocabulary import seed_business_understanding_vocabulary


def prepared_conn(path: Path):
    conn = storage_connect(path)
    assert apply_migrations(conn) == (1,)
    seed_business_understanding_vocabulary(conn)
    return conn


def add_run(conn, run_id: str, *, started_at: str, status: str = "running") -> None:
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
            status,
            started_at,
        ),
    )
    conn.commit()


def ingest_complete(conn, run_id: str, records: list[dict], *, started_at: str) -> None:
    add_run(conn, run_id, started_at=started_at)
    ingest_records(conn, run_id, records, finalize_run=("complete", 0, None))


def full_record(identity: str, *, title: str = "Same Brand") -> dict:
    return {
        "place_id": f"place-{identity}",
        "cid": f"cid-{identity}",
        "data_id": f"data-{identity}",
        "title": title,
        "category": "Restaurant",
        "address": f"Street {identity}",
        "latitude": 21.55 + (0.001 if identity == "b" else 0),
        "longitude": 39.18 + (0.001 if identity == "b" else 0),
        "phone": "+966500000000",
        "website": "https://same.example",
        "review_rating": 4.4,
        "review_count": 120,
        "status": "Open",
        "link": f"https://maps.example/{identity}",
    }


def table_count(conn, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def core_snapshot(conn):
    return {
        "runs": tuple(tuple(row) for row in conn.execute("SELECT * FROM runs ORDER BY id")),
        "businesses": tuple(tuple(row) for row in conn.execute("SELECT * FROM businesses ORDER BY id")),
        "run_businesses": tuple(
            tuple(row) for row in conn.execute(
                "SELECT * FROM run_businesses ORDER BY run_id,business_id"
            )
        ),
    }


def test_backfill_is_one_to_one_provenance_complete_and_idempotent(tmp_path: Path) -> None:
    conn = prepared_conn(tmp_path / "main.sqlite")
    ingest_complete(
        conn,
        "r1",
        [full_record("a"), full_record("b")],
        started_at="2026-09-25T10:00:00+00:00",
    )
    before_core = core_snapshot(conn)

    stats = backfill_maps_business_understanding(conn)

    assert stats.business_count == 2
    assert stats.entities_created == 2
    assert stats.locations_created == 2
    assert stats.links_created == 2
    assert stats.external_identifiers_created == 6
    assert stats.acquisition_sessions_created == 1
    assert stats.evidence_items_created == 2
    assert stats.observations_created == 20
    assert stats.facts_created == 20
    assert stats.already_backfilled is False
    assert core_snapshot(conn) == before_core

    assert table_count(conn, "business_entities") == 2
    assert table_count(conn, "business_locations") == 2
    assert table_count(conn, "maps_business_location_links") == 2
    assert table_count(conn, "dossier_assessments") == 0
    assert table_count(conn, "channels") == 0

    rows = list(conn.execute("SELECT id,title FROM businesses ORDER BY id"))
    entity_ids = [business_entity_id_for_maps_business(int(row["id"])) for row in rows]
    location_ids = [location_id_for_maps_business(int(row["id"])) for row in rows]
    assert len(set(entity_ids)) == 2
    assert len(set(location_ids)) == 2
    assert {row[0] for row in conn.execute("SELECT id FROM business_entities")} == set(entity_ids)
    assert {row[0] for row in conn.execute("SELECT id FROM business_locations")} == set(location_ids)

    # Same name/domain/phone never causes Phase-3 grouping.
    assert len({row["title"] for row in rows}) == 1
    assert table_count(conn, "business_entities") == len(rows)

    source = conn.execute(
        "SELECT source_type,name,base_url,active FROM sources WHERE id=?",
        (GOOGLE_MAPS_SOURCE_ID,),
    ).fetchone()
    assert tuple(source) == ("google_maps", "Google Maps", None, 1)
    session = conn.execute(
        "SELECT legacy_run_id,status,evidence_count,observation_count,target_subject_id "
        "FROM acquisition_sessions"
    ).fetchone()
    assert tuple(session) == ("r1", "complete", 2, 20, None)

    assert {row[0] for row in conn.execute("SELECT DISTINCT status FROM facts")} == {"single_source"}
    provenance_count = conn.execute(
        "SELECT COUNT(*) FROM facts f "
        "JOIN fact_observation_support fos ON fos.fact_id=f.id AND fos.support_role='supports' "
        "JOIN observations o ON o.id=fos.observation_id "
        "JOIN evidence_items e ON e.id=o.evidence_id "
        "JOIN acquisition_sessions a ON a.id=e.acquisition_session_id "
        "JOIN sources s ON s.id=a.source_id "
        "WHERE s.id=?",
        (GOOGLE_MAPS_SOURCE_ID,),
    ).fetchone()[0]
    assert provenance_count == 20
    assert list(conn.execute("PRAGMA foreign_key_check")) == []

    understanding_counts_before = {
        table: table_count(conn, table)
        for table in (
            "knowledge_subjects",
            "business_entities",
            "business_locations",
            "maps_business_location_links",
            "sources",
            "external_identifiers",
            "acquisition_sessions",
            "evidence_items",
            "observations",
            "facts",
            "fact_observation_support",
        )
    }
    again = backfill_maps_business_understanding(conn)
    assert again.already_backfilled is True
    assert again.business_count == 2
    assert all(
        getattr(again, field) == 0
        for field in (
            "entities_created",
            "locations_created",
            "links_created",
            "external_identifiers_created",
            "acquisition_sessions_created",
            "evidence_items_created",
            "observations_created",
            "facts_created",
        )
    )
    assert {
        table: table_count(conn, table) for table in understanding_counts_before
    } == understanding_counts_before


def test_observations_are_limited_to_values_supported_by_latest_raw_evidence(tmp_path: Path) -> None:
    conn = prepared_conn(tmp_path / "carry-forward.sqlite")
    ingest_complete(
        conn,
        "r1",
        [
            {
                "place_id": "p1",
                "title": "Legacy Name",
                "category": "Restaurant",
                "phone": "+966511111111",
                "address": "Old address",
                "latitude": 21.55,
                "longitude": 39.18,
            }
        ],
        started_at="2026-09-24T10:00:00+00:00",
    )
    ingest_complete(
        conn,
        "r2",
        [{"place_id": "p1", "title": "Current Name", "status": "Open"}],
        started_at="2026-09-25T10:00:00+00:00",
    )
    business = conn.execute("SELECT * FROM businesses").fetchone()
    assert business["category"] == "Restaurant"
    assert business["phone"] == "+966511111111"
    latest_raw = business["raw_json"]
    assert "category" not in json.loads(latest_raw)
    assert "phone" not in json.loads(latest_raw)

    stats = backfill_maps_business_understanding(conn)
    assert stats.observations_created == 2
    predicates = {row[0] for row in conn.execute("SELECT predicate FROM observations")}
    assert predicates == {"business.name.trading", "location.operating_status"}
    assert conn.execute(
        "SELECT COUNT(*) FROM facts WHERE predicate IN ('business.category.primary','location.phone')"
    ).fetchone()[0] == 0

    evidence = conn.execute(
        "SELECT content_sha256,metadata_json FROM evidence_items"
    ).fetchone()
    assert evidence["content_sha256"] == hashlib.sha256(latest_raw.encode("utf-8")).hexdigest()
    assert json.loads(evidence["metadata_json"])["raw_json"] == latest_raw


def test_backfill_requires_current_vocabulary_and_clean_completed_latest_run(tmp_path: Path) -> None:
    db = tmp_path / "preconditions.sqlite"
    conn = storage_connect(db)
    apply_migrations(conn)
    add_run(conn, "r1", started_at="2026-09-25T10:00:00+00:00", status="running")
    ingest_records(conn, "r1", [{"place_id": "p1", "title": "Example"}])

    with pytest.raises(MapsBackfillError, match="controlled predicate"):
        backfill_maps_business_understanding(conn)
    assert table_count(conn, "knowledge_subjects") == 0

    seed_business_understanding_vocabulary(conn)
    with pytest.raises(MapsBackfillError, match="not a clean completed run"):
        backfill_maps_business_understanding(conn)
    assert table_count(conn, "knowledge_subjects") == 0
    assert table_count(conn, "sources") == 0


def test_partial_state_and_deterministic_id_collision_fail_closed(tmp_path: Path) -> None:
    conn = prepared_conn(tmp_path / "partial.sqlite")
    ingest_complete(
        conn,
        "r1",
        [full_record("a"), full_record("b")],
        started_at="2026-09-25T10:00:00+00:00",
    )
    backfill_maps_business_understanding(conn)
    conn.execute(
        "DELETE FROM maps_business_location_links WHERE business_id=(SELECT MAX(id) FROM businesses)"
    )
    conn.commit()
    before = table_count(conn, "maps_business_location_links")
    with pytest.raises(MapsBackfillError, match="partial Maps understanding backfill state"):
        backfill_maps_business_understanding(conn)
    assert table_count(conn, "maps_business_location_links") == before

    collision = prepared_conn(tmp_path / "collision.sqlite")
    ingest_complete(
        collision,
        "r2",
        [full_record("c")],
        started_at="2026-09-25T11:00:00+00:00",
    )
    business_id = int(collision.execute("SELECT id FROM businesses").fetchone()[0])
    entity_id = business_entity_id_for_maps_business(business_id)
    collision.execute(
        "INSERT INTO knowledge_subjects(id,kind,created_at,updated_at) "
        "VALUES (?,'business_entity','t0','t0')",
        (entity_id,),
    )
    collision.commit()
    with pytest.raises(MapsBackfillError, match="deterministic subject id collision"):
        backfill_maps_business_understanding(collision)
    assert table_count(collision, "sources") == 0
    assert table_count(collision, "acquisition_sessions") == 0
    assert table_count(collision, "maps_business_location_links") == 0
    assert table_count(collision, "knowledge_subjects") == 1


def test_maps_identity_convergence_removes_link_but_preserves_understanding_history(tmp_path: Path) -> None:
    conn = prepared_conn(tmp_path / "merge.sqlite")
    ingest_complete(
        conn,
        "r1",
        [
            {"place_id": "p1", "title": "A", "latitude": 21.55, "longitude": 39.18},
            {"cid": "c2", "title": "A", "latitude": 21.551, "longitude": 39.181},
        ],
        started_at="2026-09-24T10:00:00+00:00",
    )
    backfill_maps_business_understanding(conn)
    assert table_count(conn, "businesses") == 2
    assert table_count(conn, "maps_business_location_links") == 2
    assert table_count(conn, "business_locations") == 2
    assert table_count(conn, "evidence_items") == 2

    ingest_complete(
        conn,
        "r2",
        [
            {
                "place_id": "p1",
                "cid": "c2",
                "title": "A",
                "latitude": 21.55,
                "longitude": 39.18,
            }
        ],
        started_at="2026-09-25T10:00:00+00:00",
    )

    assert table_count(conn, "businesses") == 1
    assert table_count(conn, "maps_business_location_links") == 1
    assert table_count(conn, "business_locations") == 2
    assert table_count(conn, "business_entities") == 2
    assert table_count(conn, "evidence_items") == 2
    assert table_count(conn, "observations") >= 2
    assert list(conn.execute("PRAGMA foreign_key_check")) == []


def test_backfill_is_row_factory_independent(tmp_path: Path) -> None:
    conn = prepared_conn(tmp_path / "row-factory.sqlite")
    ingest_complete(
        conn,
        "r1",
        [full_record("a")],
        started_at="2026-09-25T10:00:00+00:00",
    )
    conn.row_factory = None
    stats = backfill_maps_business_understanding(conn)
    assert stats.business_count == 1
    assert stats.facts_created == 10
