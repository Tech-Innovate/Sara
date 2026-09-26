from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from sara import maps_backfill as mb
from sara import maps_sync as ms
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


def _insert_legacy_business(conn, record: dict) -> int:
    raw_json = json.dumps(record, ensure_ascii=False, sort_keys=True)
    conn.execute(
        "INSERT INTO businesses("
        "canonical_key,place_id,cid,title,category,address,latitude,longitude,phone,website,"
        "review_rating,review_count,status,first_seen_at,last_seen_at,first_run_id,last_run_id,raw_json"
        ") VALUES (?,?,?,?,?,?,?,?,?,NULL,?,?,?,?,?,?,?,?)",
        (
            f"place:{record['place_id']}",
            record["place_id"],
            record.get("cid"),
            record.get("title"),
            record.get("category"),
            record.get("address"),
            record.get("latitude"),
            record.get("longitude"),
            record.get("phone"),
            record.get("review_rating"),
            record.get("review_count"),
            record.get("status"),
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
    return business_id


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


def test_maps_extraction_versions_advance_with_source_shape_semantics() -> None:
    assert mb.BACKFILL_VERSION == "2"
    assert mb.RECONCILIATION_VERSION == "maps-backfill-v2"
    assert ms.SYNC_VERSION == "2"
    assert ms.SYNC_RECONCILIATION_VERSION == "maps-sync-v2"


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
    business_id = _insert_legacy_business(conn, record)

    assert apply_migrations(conn) == (1,)
    seed_business_understanding_vocabulary(conn)
    before = tuple(conn.execute("SELECT website,raw_json FROM businesses WHERE id=?", (business_id,)).fetchone())
    mb.backfill_maps_business_understanding(conn)
    after = tuple(conn.execute("SELECT website,raw_json FROM businesses WHERE id=?", (business_id,)).fetchone())
    assert after == before
    fact = conn.execute(
        "SELECT value_json,status FROM facts WHERE predicate='business.website.official' AND valid_to IS NULL"
    ).fetchone()
    assert fact is not None
    assert json.loads(fact["value_json"]) == "https://example.test/"
    assert fact["status"] == "single_source"
    conn.close()


def test_backfill_conflicting_website_aliases_rolls_back_understanding_bootstrap(tmp_path: Path) -> None:
    conn = connect(tmp_path / "legacy-conflict.sqlite")
    _add_run(conn, "r1")
    record = _verbatim_v1181_shape()
    record["website"] = "https://different.example/"
    _insert_legacy_business(conn, record)
    assert apply_migrations(conn) == (1,)
    seed_business_understanding_vocabulary(conn)

    with pytest.raises(mb.MapsBackfillError, match="conflicting Maps website fields"):
        mb.backfill_maps_business_understanding(conn)

    assert conn.execute("SELECT COUNT(*) FROM maps_business_location_links").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM acquisition_sessions").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM evidence_items").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 0
    conn.close()


def _pre_alias_fallback_key(record: dict) -> str:
    longitude = record.get("longitude")
    if longitude is None:
        longitude = record.get("longtitude")
    values = (
        record.get("link"),
        record.get("title"),
        record.get("address"),
        record.get("phone"),
        record.get("website"),
        None if record.get("latitude") is None else str(float(record["latitude"])),
        None if longitude is None else str(float(longitude)),
    )
    normalized = [
        str(value).strip().lower() if value not in (None, "") else ""
        for value in values
    ]
    digest = hashlib.sha256("|".join(normalized).encode("utf-8")).hexdigest()
    return f"fallback:{digest}"


def _insert_pre_alias_weak_row(conn, record: dict, *, run_id: str = "r1") -> int:
    canonical_key = _pre_alias_fallback_key(record)
    raw_json = json.dumps(record, ensure_ascii=False, sort_keys=True)
    cursor = conn.execute(
        "INSERT INTO businesses("
        "canonical_key,title,address,latitude,longitude,phone,website,first_seen_at,last_seen_at,"
        "first_run_id,last_run_id,raw_json"
        ") VALUES (?,?,?,?,?,?,NULL,?,?,?,?,?)",
        (
            canonical_key,
            record.get("title"),
            record.get("address"),
            record.get("latitude"),
            record.get("longitude"),
            record.get("phone"),
            "2026-09-26T00:00:00+00:00",
            "2026-09-26T00:00:00+00:00",
            run_id,
            run_id,
            raw_json,
        ),
    )
    business_id = int(cursor.lastrowid)
    conn.execute(
        "INSERT INTO run_businesses(run_id,business_id,first_observed_at) VALUES (?,?,?)",
        (run_id, business_id, "2026-09-26T00:00:00+00:00"),
    )
    conn.execute(
        "UPDATE runs SET status='complete',finished_at=?,exit_code=0,raw_records=1,accepted_records=1,"
        "unique_seen=1,new_businesses=1 WHERE id=?",
        ("2026-09-26T00:01:00+00:00", run_id),
    )
    conn.commit()
    return business_id


def test_storage_reuses_pre_alias_fallback_identity_when_raw_website_agrees(
    tmp_path: Path,
) -> None:
    conn = connect(tmp_path / "legacy-fallback.sqlite")
    _add_run(conn, "r1")
    record = _verbatim_v1181_shape()
    record.pop("place_id")
    record.pop("cid")
    business_id = _insert_pre_alias_weak_row(conn, record)
    legacy_key = _pre_alias_fallback_key(record)

    _add_run(conn, "r2", "2026-09-26T01:00:00+00:00")
    ingest_records(conn, "r2", [record], finalize_run=("complete", 0, None))

    canonical_shape = dict(record)
    canonical_shape["website"] = canonical_shape.pop("web_site")
    _add_run(conn, "r3", "2026-09-26T02:00:00+00:00")
    ingest_records(conn, "r3", [canonical_shape], finalize_run=("complete", 0, None))

    rows = list(
        conn.execute("SELECT id,canonical_key,website FROM businesses ORDER BY id")
    )
    assert len(rows) == 1
    assert int(rows[0]["id"]) == business_id
    assert rows[0]["canonical_key"] == legacy_key
    assert rows[0]["website"] == "https://example.test/"
    conn.close()


def test_storage_does_not_reuse_legacy_fallback_collision_with_different_raw_website(
    tmp_path: Path,
) -> None:
    conn = connect(tmp_path / "legacy-fallback-collision.sqlite")
    _add_run(conn, "r1")
    incoming = _verbatim_v1181_shape()
    incoming.pop("place_id")
    incoming.pop("cid")
    historical = dict(incoming)
    historical["web_site"] = "https://different.example/"
    old_id = _insert_pre_alias_weak_row(conn, historical)

    _add_run(conn, "r2", "2026-09-26T01:00:00+00:00")
    ingest_records(conn, "r2", [incoming], finalize_run=("complete", 0, None))

    rows = list(conn.execute("SELECT id,website FROM businesses ORDER BY id"))
    assert len(rows) == 2
    assert int(rows[0]["id"]) == old_id
    assert rows[0]["website"] is None
    assert rows[1]["website"] == "https://example.test/"
    conn.close()

