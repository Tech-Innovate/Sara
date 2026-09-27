from __future__ import annotations

from pathlib import Path

import pytest

import sara.reviews.core as review_core
from sara.maps_backfill import backfill_maps_business_understanding, location_id_for_maps_business
from sara.migrations import apply_migrations
from sara.reviews import ReviewIntelligenceError, extract_retained_reviews
from sara.storage import connect as storage_connect, ingest_records
from sara.understanding_vocabulary import seed_business_understanding_vocabulary


def prepared_conn(path: Path):
    conn = storage_connect(path)
    assert apply_migrations(conn) == (1,)
    seed_business_understanding_vocabulary(conn)
    return conn


def add_run(conn, run_id: str) -> None:
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
            "2026-09-27T07:00:00+00:00",
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
