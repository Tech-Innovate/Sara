from __future__ import annotations

from pathlib import Path

from sara.dossier import build_business_dossier, persist_dossier_assessment
from sara.maps_backfill import backfill_maps_business_understanding
from sara.migrations import apply_migrations
from sara.reviews import extract_retained_reviews
from sara.storage import connect, ingest_records
from sara.understanding_vocabulary import seed_business_understanding_vocabulary


def _prepared(path: Path, *, with_review: bool = False):
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
            "dossier-integrity",
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
    record = {
        "place_id": "place-integrity",
        "cid": "cid-integrity",
        "data_id": "data-integrity",
        "title": "Integrity Restaurant",
        "category": "Restaurant",
        "address": "Integrity Street",
        "latitude": 21.55,
        "longitude": 39.18,
        "phone": "+966500000000",
        "website": "https://integrity.example",
        "review_rating": 4.6,
        "review_count": 17,
        "status": "Open",
        "link": "https://maps.example/integrity",
    }
    if with_review:
        record["user_reviews"] = [
            {
                "review_id": "review-integrity-1",
                "source": "Google",
                "Rating": 5,
                "Description": "Good service",
                "language": "en",
                "posted_at_unix_micros": 1_758_758_400_000_000,
            }
        ]
    ingest_records(conn, "r1", [record], finalize_run=("complete", 0, None))
    backfill_maps_business_understanding(conn)
    return conn


def test_assessment_clock_is_obtained_after_writer_transaction_begins(tmp_path: Path) -> None:
    conn = _prepared(tmp_path / "clock.sqlite")
    seen = {"called": False}

    def now() -> str:
        assert conn.in_transaction
        seen["called"] = True
        return "2026-09-28T10:00:00+00:00"

    result = persist_dossier_assessment(conn, business_id=1, now=now)
    assert seen["called"] is True
    assert result.computed_at == "2026-09-28T10:00:00+00:00"
    assert conn.in_transaction is False
    conn.close()


def test_invalid_review_evidence_is_reported_but_not_promoted_to_customer_voice(
    tmp_path: Path,
) -> None:
    conn = _prepared(tmp_path / "review-integrity.sqlite", with_review=True)
    extracted = extract_retained_reviews(
        conn,
        business_id=1,
        now=lambda: "2026-09-27T12:00:00+00:00",
    )
    assert extracted.observations_created == 1
    evidence_id = conn.execute(
        "SELECT evidence_id FROM observations WHERE predicate='reputation.customer_review'"
    ).fetchone()[0]

    # Simulate durable-state corruption that bypassed the normal immutable writer.
    # The dossier must fail closed at the projection boundary rather than count it
    # as valid customer voice merely because the observation row is reachable.
    conn.execute("DROP TRIGGER evidence_items_immutable")
    conn.execute("UPDATE evidence_items SET status='incomplete' WHERE id=?", (evidence_id,))
    conn.commit()

    dossier = build_business_dossier(
        conn,
        business_id=1,
        evaluated_at="2026-09-28T10:00:00+00:00",
    )
    assert dossier["customer_voice"]["review_count"] == 0
    assert any(
        issue["code"] == "customer_review_uses_nonusable_evidence"
        and issue["observation_id"]
        for issue in dossier["integrity_issues"]
    )

    assessment = persist_dossier_assessment(
        conn,
        business_id=1,
        now=lambda: "2026-09-28T10:00:00+00:00",
    )
    states = {item["domain"]: item["state"] for item in assessment.domains}
    assert states["reputation"] == "partial"
    assert assessment.analysis_ready is False
    conn.close()
