from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from sara.dossier import build_business_dossier, persist_dossier_assessment
from sara.maps_backfill import (
    backfill_maps_business_understanding,
    business_entity_id_for_maps_business,
    location_id_for_maps_business,
)
from sara.migrations import apply_migrations
from sara.reviews import extract_retained_reviews
from sara.storage import connect, ingest_records
from sara.understanding_vocabulary import seed_business_understanding_vocabulary


def _prepared(path: Path):
    conn = connect(path)
    assert apply_migrations(conn) == (1,)
    seed_business_understanding_vocabulary(conn)
    conn.execute(
        "INSERT INTO runs("
        "id,area_name,bbox_json,cell_km,depth,queries_json,scraper_image,config_json,"
        "raw_path,status,started_at"
        ") VALUES (?,?,?,?,?,?,?,?,?,'running',?)",
        (
            "r1",
            "dossier-hardening",
            '{"max_lat":22,"max_lon":40,"min_lat":21,"min_lon":39}',
            2.0,
            1,
            '["restaurant"]',
            "gosom/google-maps-scraper:v1.18.1",
            '{"strict_bounds":true}',
            "/evidence/r1.jsonl",
            "2026-09-25T10:00:00+00:00",
        ),
    )
    conn.commit()
    return conn


def _backfill(conn):
    with patch("sara.maps_backfill._utc_now", return_value="2026-09-26T10:00:00+00:00"):
        return backfill_maps_business_understanding(conn)


def _record(identity: str, *, latitude: float, with_review: bool) -> dict:
    record = {
        "place_id": f"place-{identity}",
        "cid": f"cid-{identity}",
        "data_id": f"data-{identity}",
        "title": f"Restaurant {identity}",
        "category": "Restaurant",
        "address": f"Street {identity}",
        "latitude": latitude,
        "longitude": 39.18,
        "phone": "+966500000000",
        "website": f"https://{identity}.example",
        "review_rating": 4.5,
        "review_count": 10,
        "status": "Open",
        "link": f"https://maps.example/{identity}",
    }
    if with_review:
        record["user_reviews"] = [
            {
                "review_id": f"review-{identity}",
                "source": "Google",
                "Rating": 5,
                "Description": f"Review for {identity}",
                "language": "en",
                "posted_at_unix_micros": 1_758_758_400_000_000,
            }
        ]
    return record


def test_customer_voice_survives_cross_owner_location_convergence(tmp_path: Path) -> None:
    conn = _prepared(tmp_path / "cross-owner.sqlite")
    ingest_records(
        conn,
        "r1",
        [
            _record("a", latitude=21.55, with_review=True),
            _record("b", latitude=21.56, with_review=False),
        ],
        finalize_run=("complete", 0, None),
    )
    _backfill(conn)
    business_a, business_b = [
        int(row[0]) for row in conn.execute("SELECT id FROM businesses ORDER BY id")
    ]
    source_location = location_id_for_maps_business(business_a)
    source_entity = business_entity_id_for_maps_business(business_a)
    target_location = location_id_for_maps_business(business_b)
    target_entity = business_entity_id_for_maps_business(business_b)
    assert source_entity != target_entity

    extracted = extract_retained_reviews(
        conn,
        business_id=business_a,
        now=lambda: "2026-09-27T12:00:00+00:00",
    )
    assert extracted.source_location_id == source_location
    assert extracted.observations_created == 1

    # Preserve the source Location/Entity as immutable provenance while the
    # surviving Maps business becomes anchored to the target Location/Entity.
    conn.execute("DELETE FROM businesses WHERE id=?", (business_b,))
    conn.execute(
        "UPDATE maps_business_location_links SET location_id=? WHERE business_id=?",
        (target_location, business_a),
    )
    merged_at = "2026-09-27T13:00:00+00:00"
    conn.execute(
        "UPDATE knowledge_subjects SET record_state='merged',merged_into_subject_id=?,"
        "merged_at=?,updated_at=? WHERE id=?",
        (target_location, merged_at, merged_at, source_location),
    )
    conn.commit()

    dossier = build_business_dossier(
        conn,
        business_id=business_a,
        evaluated_at="2026-09-28T10:00:00+00:00",
    )
    assert dossier["business_entity"]["id"] == target_entity
    assert dossier["customer_voice"]["review_count"] == 1
    review = dossier["customer_voice"]["reviews"][0]
    assert review["source_location_id"] == source_location
    assert review["canonical_location_id"] == target_location
    assert review["location_resolution_chain"] == [source_location, target_location]
    assert review["normalized_value"]["review_id"] == "review-a"
    conn.close()


def test_unattempted_capability_domain_is_not_started_not_insufficient(tmp_path: Path) -> None:
    conn = _prepared(tmp_path / "not-started.sqlite")
    ingest_records(
        conn,
        "r1",
        [_record("a", latitude=21.55, with_review=False)],
        finalize_run=("complete", 0, None),
    )
    _backfill(conn)

    result = persist_dossier_assessment(
        conn,
        business_id=1,
        now=lambda: "2026-09-28T10:00:00+00:00",
    )
    states = {item["domain"]: item["state"] for item in result.domains}
    assert states["digital_capabilities"] == "not_started"
    assert states["offerings"] == "not_started"
    assert states["customer_market"] == "not_started"
    conn.close()
