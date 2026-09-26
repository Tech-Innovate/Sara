from __future__ import annotations

import json
from pathlib import Path

import pytest

from sara.maps_backfill import backfill_maps_business_understanding
from sara.maps_source import MapsSourceShapeError, official_website
from sara.migrations import apply_migrations
from sara.storage import connect, ingest_records
from sara.understanding_vocabulary import seed_business_understanding_vocabulary


def _add_run(conn, run_id: str, started_at: str = "2026-09-26T00:00:00+00:00") -> None:
    conn.execute(
        "INSERT INTO runs("
        "id,area_name,bbox_json,cell_km,depth,queries_json,scraper_image,config_json,"
        "raw_path,status,started_at"
        ") VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            run_id,
            "test-area",
            '{"max_lat":22,"max_lon":40,"min_lat":21,"min_lon":39}',
            1.0,
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


def _verbatim_v1181_shape() -> dict:
    return {
        "place_id": "ChIJ-real-shape",
        "cid": "1234567890123456789",
        "title": "Real Upstream Shape",
        "category": "Restaurant",
        "address": "Jeddah",
        "latitude": 21.543333,
        "longitude": 39.172778,
        "phone": "+966500000000",
        "web_site": "https://example.test/",
        "review_rating": 4.5,
        "review_count": 42,
        "status": "Open",
        "link": "https://www.google.com/maps?cid=1234567890123456789",
    }


def test_official_website_accepts_pinned_and_canonical_spellings() -> None:
    assert official_website({"web_site": " https://legacy.example/ "}) == "https://legacy.example/"
    assert official_website({"website": "https://canonical.example/"}) == "https://canonical.example/"
    assert official_website(
        {"website": "https://same.example/", "web_site": " https://same.example/ "}
    ) == "https://same.example/"
    assert official_website({}) is None


def test_official_website_conflict_fails_closed() -> None:
    with pytest.raises(MapsSourceShapeError, match="conflicting Maps website fields"):
        official_website(
            {"website": "https://one.example/", "web_site": "https://two.example/"}
        )


def test_storage_normalizes_v1181_web_site_and_fallback_identity(tmp_path: Path) -> None:
    conn = connect(tmp_path / "storage.sqlite")
    _add_run(conn, "r1")
    record = _verbatim_v1181_shape()
    record.pop("place_id")
    record.pop("cid")
    ingest_records(conn, "r1", [record], finalize_run=("complete", 0, None))

    row = conn.execute("SELECT id,website,raw_json FROM businesses").fetchone()
    assert row["website"] == "https://example.test/"
    assert json.loads(row["raw_json"])["web_site"] == "https://example.test/"
    first_id = int(row["id"])

    _add_run(conn, "r2", "2026-09-26T01:00:00+00:00")
    canonical_shape = dict(record)
    canonical_shape["website"] = canonical_shape.pop("web_site")
    ingest_records(conn, "r2", [canonical_shape], finalize_run=("complete", 0, None))
    rows = list(conn.execute("SELECT id,website FROM businesses"))
    assert [(int(item["id"]), item["website"]) for item in rows] == [
        (first_id, "https://example.test/")
    ]
    conn.close()


def test_storage_rejects_conflicting_aliases_before_any_business_mutation(tmp_path: Path) -> None:
    conn = connect(tmp_path / "conflict.sqlite")
    _add_run(conn, "r1")
    record = _verbatim_v1181_shape()
    record["website"] = "https://different.example/"
    with pytest.raises(MapsSourceShapeError):
        ingest_records(conn, "r1", [record], finalize_run=("complete", 0, None))
    assert conn.execute("SELECT COUNT(*) FROM businesses").fetchone()[0] == 0
    conn.close()


def test_backfill_reads_legacy_web_site_without_mutating_legacy_business_row(tmp_path: Path) -> None:
    conn = connect(tmp_path / "legacy.sqlite")
    _add_run(conn, "r1")
    record = _verbatim_v1181_shape()
    raw_json = json.dumps(record, ensure_ascii=False, sort_keys=True)
    conn.execute(
        "INSERT INTO businesses("
        "canonical_key,place_id,cid,title,category,address,latitude,longitude,phone,website,"
        "review_rating,review_count,status,first_seen_at,last_seen_at,first_run_id,last_run_id,raw_json"
        ") VALUES (?,?,?,?,?,?,?,?,?,NULL,?,?,?,?,?,?,?,?)",
        (
            "place:ChIJ-real-shape",
            record["place_id"],
            record["cid"],
            record["title"],
            record["category"],
            record["address"],
            record["latitude"],
            record["longitude"],
            record["phone"],
            record["review_rating"],
            record["review_count"],
            record["status"],
            "2026-09-26T00:00:00+00:00",
            "2026-09-26T00:00:00+00:00",
            "r1",
            "r1",
            raw_json,
        ),
    )
    business_id = int(conn.execute("SELECT id FROM businesses").fetchone()[0])
    conn.execute(
        "INSERT INTO run_businesses(run_id,business_id,first_observed_at) VALUES (?,?,?)",
        ("r1", business_id, "2026-09-26T00:00:00+00:00"),
    )
    conn.execute(
        "UPDATE runs SET status='complete',finished_at=?,exit_code=0,raw_records=1,accepted_records=1,"
        "unique_seen=1,new_businesses=1 WHERE id='r1'",
        ("2026-09-26T00:01:00+00:00",),
    )
    conn.commit()

    assert apply_migrations(conn) == (1,)
    seed_business_understanding_vocabulary(conn)
    before = tuple(conn.execute("SELECT website,raw_json FROM businesses WHERE id=?", (business_id,)).fetchone())
    backfill_maps_business_understanding(conn)
    after = tuple(conn.execute("SELECT website,raw_json FROM businesses WHERE id=?", (business_id,)).fetchone())
    assert after == before
    fact = conn.execute(
        "SELECT value_json,status FROM facts WHERE predicate='business.website.official' AND valid_to IS NULL"
    ).fetchone()
    assert fact is not None
    assert json.loads(fact["value_json"]) == "https://example.test/"
    assert fact["status"] == "single_source"
    conn.close()
