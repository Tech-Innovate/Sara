from __future__ import annotations

from pathlib import Path

import pytest

from sara import maps_backfill as mb
from sara.maps_sync import SYNC_VERSION, MapsSyncError, sync_maps_business_understanding
from sara.migrations import apply_migrations
from sara.storage import connect, ingest_records
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


def _record(identity: str) -> dict:
    return {
        "place_id": f"place-{identity}",
        "cid": f"cid-{identity}",
        "data_id": f"data-{identity}",
        "title": f"Business {identity}",
        "category": "Restaurant",
        "address": f"Street {identity}",
        "latitude": 21.55,
        "longitude": 39.18,
        "phone": "+966500000000",
        "website": f"https://{identity}.example",
        "review_rating": 4.4,
        "review_count": 120,
        "status": "Open",
        "link": f"https://maps.example/{identity}",
    }


def _ingest_complete(conn, run_id: str, identity: str, started_at: str) -> None:
    _add_run(conn, run_id, started_at)
    ingest_records(
        conn,
        run_id,
        [_record(identity)],
        finalize_run=("complete", 0, None),
    )


def _phase4_fixture(tmp_path: Path):
    conn = _prepared(tmp_path / "sync-hardening.sqlite")
    _ingest_complete(conn, "r1", "seed", "2026-09-23T10:00:00+00:00")
    mb.backfill_maps_business_understanding(conn)
    _ingest_complete(conn, "r2", "new", "2026-09-25T10:00:00+00:00")
    first = sync_maps_business_understanding(conn)
    assert first.evidence_items_created == 1
    assert first.observations_created == 10
    return conn


def test_sync_fails_closed_on_google_maps_source_identity_drift(tmp_path: Path) -> None:
    conn = _prepared(tmp_path / "source-drift.sqlite")
    _ingest_complete(conn, "r1", "seed", "2026-09-23T10:00:00+00:00")
    mb.backfill_maps_business_understanding(conn)
    conn.execute("DROP TRIGGER sources_identity_immutable")
    conn.execute(
        "UPDATE sources SET source_type='other_web' WHERE id=?",
        (mb.GOOGLE_MAPS_SOURCE_ID,),
    )
    conn.commit()

    with pytest.raises(MapsSyncError, match="source identity/type drift"):
        sync_maps_business_understanding(conn)


def test_sync_fails_closed_on_acquisition_session_count_drift(tmp_path: Path) -> None:
    conn = _phase4_fixture(tmp_path)
    session_id = conn.execute(
        "SELECT id FROM acquisition_sessions WHERE legacy_run_id='r2'"
    ).fetchone()[0]
    conn.execute(
        "UPDATE acquisition_sessions SET evidence_count=evidence_count+1 WHERE id=?",
        (session_id,),
    )
    conn.commit()

    with pytest.raises(MapsSyncError, match="count drift"):
        sync_maps_business_understanding(conn)


def test_sync_rejects_existing_snapshot_with_missing_fact_support(tmp_path: Path) -> None:
    conn = _phase4_fixture(tmp_path)
    row = conn.execute(
        "SELECT fos.fact_id,fos.observation_id "
        "FROM fact_observation_support fos "
        "JOIN observations o ON o.id=fos.observation_id "
        "WHERE o.extractor_name='sara.maps_sync' "
        f"AND o.extractor_version='{SYNC_VERSION}' "
        "ORDER BY o.id LIMIT 1"
    ).fetchone()
    assert row is not None

    conn.execute("DROP TRIGGER fact_observation_support_no_delete")
    conn.execute(
        "DELETE FROM fact_observation_support WHERE fact_id=? AND observation_id=?",
        (row["fact_id"], row["observation_id"]),
    )
    conn.commit()

    with pytest.raises(
        MapsSyncError,
        match="(fact/support|support) provenance is incomplete",
    ):
        sync_maps_business_understanding(conn)


def test_sync_rejects_phase4_snapshot_with_missing_external_identifier(tmp_path: Path) -> None:
    conn = _phase4_fixture(tmp_path)
    identifier_id = mb._external_identifier_id("place_id", "place-new")
    conn.execute("DROP TRIGGER external_identifiers_no_delete")
    conn.execute("DELETE FROM external_identifiers WHERE id=?", (identifier_id,))
    conn.commit()
    assert conn.execute(
        "SELECT 1 FROM external_identifiers WHERE id=?", (identifier_id,)
    ).fetchone() is None

    with pytest.raises(MapsSyncError, match="external identifier provenance"):
        sync_maps_business_understanding(conn)

    assert conn.execute(
        "SELECT 1 FROM external_identifiers WHERE id=?", (identifier_id,)
    ).fetchone() is None


def test_sync_rejects_phase3_snapshot_with_missing_external_identifier(tmp_path: Path) -> None:
    conn = _prepared(tmp_path / "phase3-identifier-loss.sqlite")
    _ingest_complete(conn, "r1", "seed", "2026-09-23T10:00:00+00:00")
    mb.backfill_maps_business_understanding(conn)
    identifier_id = mb._external_identifier_id("place_id", "place-seed")
    conn.execute("DROP TRIGGER external_identifiers_no_delete")
    conn.execute("DELETE FROM external_identifiers WHERE id=?", (identifier_id,))
    conn.commit()

    with pytest.raises(MapsSyncError, match="external identifier provenance"):
        sync_maps_business_understanding(conn)

    assert conn.execute(
        "SELECT 1 FROM external_identifiers WHERE id=?", (identifier_id,)
    ).fetchone() is None
