from __future__ import annotations

import json
from pathlib import Path

import pytest

import sara.reviews.core as review_core
from sara.maps_backfill import (
    BACKFILL_COLLECTOR_NAME,
    BACKFILL_VERSION,
    GOOGLE_MAPS_SOURCE_ID,
    backfill_maps_business_understanding,
    business_entity_id_for_maps_business,
    location_id_for_maps_business,
)
from sara.maps_sync import SYNC_COLLECTOR_NAME, SYNC_VERSION, sync_maps_business_understanding
from sara.migrations import apply_migrations
from sara.reviews import ReviewIntelligenceError, extract_retained_reviews
from sara.reviews.model import REVIEW_PREDICATE
from sara.storage import connect as storage_connect, ingest_records
from sara.understanding_vocabulary import seed_business_understanding_vocabulary


def prepared_conn(path: Path):
    conn = storage_connect(path)
    assert apply_migrations(conn) == (1, 2, 3, 4, 5)
    seed_business_understanding_vocabulary(conn)
    return conn


def add_run(
    conn,
    run_id: str,
    *,
    started_at: str = "2026-09-27T07:00:00+00:00",
) -> None:
    conn.execute(
        "INSERT INTO runs("
        "id,area_name,bbox_json,cell_km,depth,queries_json,scraper_image,config_json,"
        "raw_path,status,started_at"
        ") VALUES (?,?,?,?,?,?,?,?,?,'running',?)",
        (
            run_id,
            "review-hardening",
            '{"max_lat":22,"max_lon":40,"min_lat":21,"min_lon":39}',
            2.0,
            1,
            '["restaurant"]',
            "gosom/google-maps-scraper:v1.18.1",
            '{"strict_bounds":true}',
            f"/evidence/{run_id}.jsonl",
            started_at,
        ),
    )
    conn.commit()


def record(identity: str, *, latitude: float) -> dict:
    return {
        "place_id": f"place-{identity}",
        "cid": f"cid-{identity}",
        "data_id": f"data-{identity}",
        "title": f"Review Restaurant {identity}",
        "category": "Restaurant",
        "address": f"Street {identity}",
        "latitude": latitude,
        "longitude": 39.18,
        "website": f"https://{identity}.example",
        "review_rating": 4.5,
        "review_count": 10,
        "status": "Open",
        "link": f"https://maps.example/{identity}",
        "user_reviews": [
            {
                "review_id": f"review-{identity}",
                "source": "Google",
                "Rating": 5,
                "Description": f"Review for {identity}",
                "language": "en",
                "text_original": f"Review for {identity}",
                "posted_at_unix_micros": 1_756_684_800_000_000,
            }
        ],
    }


def ingest_businesses(conn, records: list[dict]) -> list[int]:
    add_run(conn, "r1")
    ingest_records(conn, "r1", records, finalize_run=("complete", 0, None))
    backfill_maps_business_understanding(conn)
    return [int(row[0]) for row in conn.execute("SELECT id FROM businesses ORDER BY id")]


def test_parent_maps_evidence_must_resolve_to_selected_current_location(tmp_path: Path) -> None:
    conn = prepared_conn(tmp_path / "identity.sqlite")
    business_a, business_b = ingest_businesses(
        conn,
        [record("a", latitude=21.55), record("b", latitude=21.56)],
    )
    location_b = location_id_for_maps_business(business_b)

    # Simulate a corrupt Maps -> Understanding anchor. The selected Maps row now
    # points at another active location, while its immutable retained Maps
    # evidence still carries the original deterministic location lineage.
    conn.execute(
        "DELETE FROM maps_business_location_links WHERE business_id=?",
        (business_b,),
    )
    conn.execute(
        "UPDATE maps_business_location_links SET location_id=? WHERE business_id=?",
        (location_b, business_a),
    )
    conn.commit()

    with pytest.raises(ReviewIntelligenceError, match="does not resolve to the selected current location"):
        extract_retained_reviews(conn, business_id=business_a)

    assert conn.execute(
        "SELECT COUNT(*) FROM acquisition_sessions WHERE collector_name=?",
        (review_core.COLLECTOR_NAME,),
    ).fetchone()[0] == 0
    conn.close()


def test_review_session_and_observation_stay_on_source_subject_after_location_convergence(
    tmp_path: Path,
) -> None:
    conn = prepared_conn(tmp_path / "merge-stability.sqlite")
    business_a, business_b = ingest_businesses(
        conn,
        [record("a", latitude=21.55), record("b", latitude=21.56)],
    )
    source_location = location_id_for_maps_business(business_a)
    source_entity = business_entity_id_for_maps_business(business_a)
    target_location = location_id_for_maps_business(business_b)
    target_entity = business_entity_id_for_maps_business(business_b)

    first = extract_retained_reviews(
        conn,
        business_id=business_a,
        now=lambda: "2026-09-27T08:00:00+00:00",
    )
    assert first.source_location_id == source_location
    assert first.canonical_location_id == source_location
    assert first.source_business_entity_id == source_entity
    assert first.business_entity_id == source_entity

    # Model the post-acquisition Understanding state after the second Maps row
    # has converged away: its Maps row/link disappears, the surviving Maps row
    # now points to the chosen target Location, and the original Location is a
    # durable historical alias. Location ownership itself remains immutable.
    conn.execute("DELETE FROM businesses WHERE id=?", (business_b,))
    conn.execute(
        "UPDATE maps_business_location_links SET location_id=? WHERE business_id=?",
        (target_location, business_a),
    )
    merged_at = "2026-09-27T08:30:00+00:00"
    conn.execute(
        "UPDATE knowledge_subjects SET record_state='merged',merged_into_subject_id=?,"
        "merged_at=?,updated_at=? WHERE id=?",
        (target_location, merged_at, merged_at, source_location),
    )
    conn.commit()

    counts_before = tuple(
        int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in ("acquisition_sessions", "evidence_items", "observations")
    )
    second = extract_retained_reviews(
        conn,
        business_id=business_a,
        now=lambda: "2026-09-27T09:00:00+00:00",
    )

    assert second.session_id == first.session_id
    assert second.already_extracted is True
    assert second.source_location_id == source_location
    assert second.canonical_location_id == target_location
    assert second.source_business_entity_id == source_entity
    assert second.business_entity_id == target_entity
    assert tuple(
        int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in ("acquisition_sessions", "evidence_items", "observations")
    ) == counts_before
    assert conn.execute(
        "SELECT target_subject_id FROM acquisition_sessions WHERE id=?",
        (first.session_id,),
    ).fetchone()[0] == source_location
    assert {
        row[0]
        for row in conn.execute(
            "SELECT subject_id FROM observations WHERE predicate=?",
            (REVIEW_PREDICATE,),
        )
    } == {source_location}
    conn.close()


def test_unrelated_malformed_maps_metadata_does_not_poison_selected_business(
    tmp_path: Path,
) -> None:
    conn = prepared_conn(tmp_path / "unrelated-corruption.sqlite")
    business_id = ingest_businesses(conn, [record("selected", latitude=21.55)])[0]

    # A single-target extraction must not decode every other Maps evidence row
    # in the database. This unrelated, structurally valid evidence row has
    # deliberately malformed metadata and would have poisoned the old broad
    # source scan before the selected snapshot was isolated.
    add_run(conn, "unrelated-run")
    config_json = "{}"
    session_id = "acq_unrelated_corrupt_maps"
    conn.execute(
        "INSERT INTO acquisition_sessions("
        "id,target_subject_id,source_id,collector_name,collector_version,config_json,config_hash,"
        "status,started_at,finished_at,error,legacy_run_id,evidence_count,observation_count"
        ") VALUES (?,NULL,?,?,?,?,?,'complete',?,?,NULL,?,1,0)",
        (
            session_id,
            GOOGLE_MAPS_SOURCE_ID,
            BACKFILL_COLLECTOR_NAME,
            BACKFILL_VERSION,
            config_json,
            review_core.sha256_text(config_json),
            "2026-09-27T07:00:00+00:00",
            "2026-09-27T07:01:00+00:00",
            "unrelated-run",
        ),
    )
    corrupt_evidence_id = "ev_unrelated_corrupt_maps"
    conn.execute(
        "INSERT INTO evidence_items("
        "id,acquisition_session_id,source_id,source_locator,source_role,status,retrieved_at,"
        "published_at,language,media_type,content_sha256,artifact_ref,metadata_json,created_at"
        ") VALUES (?,?,?,?,'platform','usable',?,NULL,NULL,'application/json',?,NULL,?,?)",
        (
            corrupt_evidence_id,
            session_id,
            GOOGLE_MAPS_SOURCE_ID,
            "https://maps.example/unrelated-corrupt",
            "2026-09-27T07:01:00+00:00",
            "0" * 64,
            "{not-valid-json",
            "2026-09-27T07:01:00+00:00",
        ),
    )
    conn.commit()

    stats = extract_retained_reviews(
        conn,
        business_id=business_id,
        now=lambda: "2026-09-27T08:00:00+00:00",
    )

    assert stats.business_id == business_id
    assert stats.observations_created == 1
    assert stats.source_evidence_id != corrupt_evidence_id
    assert conn.execute(
        "SELECT metadata_json FROM evidence_items WHERE id=?",
        (corrupt_evidence_id,),
    ).fetchone()[0] == "{not-valid-json"
    conn.close()


def test_current_maps_sync_snapshot_remains_valid_review_parent(tmp_path: Path) -> None:
    conn = prepared_conn(tmp_path / "maps-sync-parent.sqlite")
    business_id = ingest_businesses(conn, [record("sync", latitude=21.55)])[0]

    updated = record("sync", latitude=21.55)
    updated_review = updated["user_reviews"][0]
    updated_review["Description"] = "Updated retained review"
    updated_review["text_original"] = "Updated retained review"
    updated_review["updated_at_unix_micros"] = 1_756_771_200_000_000
    add_run(conn, "r2", started_at="2026-09-27T08:00:00+00:00")
    ingest_records(conn, "r2", [updated], finalize_run=("complete", 0, None))

    sync_stats = sync_maps_business_understanding(conn)
    assert sync_stats.evidence_items_created == 1

    stats = extract_retained_reviews(
        conn,
        business_id=business_id,
        now=lambda: "2026-09-27T09:00:00+00:00",
    )

    parent = conn.execute(
        "SELECT a.collector_name,a.collector_version,a.legacy_run_id,e.metadata_json "
        "FROM evidence_items e JOIN acquisition_sessions a ON a.id=e.acquisition_session_id "
        "WHERE e.id=?",
        (stats.source_evidence_id,),
    ).fetchone()
    assert tuple(parent[:3]) == (SYNC_COLLECTOR_NAME, SYNC_VERSION, "r2")
    assert json.loads(parent["metadata_json"])["import_kind"] == "maps_sync_snapshot"
    value = json.loads(
        conn.execute(
            "SELECT value_json FROM observations WHERE predicate=? "
            "AND extractor_name=? AND extractor_version=?",
            (REVIEW_PREDICATE, review_core.COLLECTOR_NAME, review_core.COLLECTOR_VERSION),
        ).fetchone()[0]
    )
    assert value["review_id"] == "review-sync"
    assert value["text_original"] == "Updated retained review"
    conn.close()


def test_current_state_and_source_resolution_run_under_one_writer_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = prepared_conn(tmp_path / "transaction.sqlite")
    business_id = ingest_businesses(conn, [record("a", latitude=21.55)])[0]
    original_resolve = review_core._resolve_target
    original_source = review_core._maps_source_evidence
    seen = {"resolve": False, "source": False}

    def guarded_resolve(connection, **kwargs):
        assert connection.in_transaction
        seen["resolve"] = True
        return original_resolve(connection, **kwargs)

    def guarded_source(connection, **kwargs):
        assert connection.in_transaction
        seen["source"] = True
        return original_source(connection, **kwargs)

    monkeypatch.setattr(review_core, "_resolve_target", guarded_resolve)
    monkeypatch.setattr(review_core, "_maps_source_evidence", guarded_source)

    stats = extract_retained_reviews(
        conn,
        business_id=business_id,
        now=lambda: "2026-09-27T08:00:00+00:00",
    )

    assert seen == {"resolve": True, "source": True}
    assert stats.observations_created == 1
    assert conn.in_transaction is False
    conn.close()


def test_failure_after_source_resolution_rolls_back_the_writer_transaction(tmp_path: Path) -> None:
    conn = prepared_conn(tmp_path / "rollback.sqlite")
    business_id = ingest_businesses(conn, [record("a", latitude=21.55)])[0]
    sessions_before = int(conn.execute("SELECT COUNT(*) FROM acquisition_sessions").fetchone()[0])

    with pytest.raises(ReviewIntelligenceError, match="not valid ISO-8601"):
        extract_retained_reviews(
            conn,
            business_id=business_id,
            now=lambda: "not-a-timestamp",
        )

    assert conn.in_transaction is False
    assert int(conn.execute("SELECT COUNT(*) FROM acquisition_sessions").fetchone()[0]) == sessions_before
    assert conn.execute(
        "SELECT COUNT(*) FROM acquisition_sessions WHERE collector_name=?",
        (review_core.COLLECTOR_NAME,),
    ).fetchone()[0] == 0
    conn.close()
