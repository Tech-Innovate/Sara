from __future__ import annotations

import json
from pathlib import Path

from sara.maps_backfill import backfill_maps_business_understanding
from sara.migrations import apply_migrations
from sara.reviews import main as reviews_main
from sara.storage import connect as storage_connect, ingest_records
from sara.understanding_vocabulary import seed_business_understanding_vocabulary


def _prepare_db(path: Path) -> tuple[int, str]:
    conn = storage_connect(path)
    assert apply_migrations(conn) == (1,)
    seed_business_understanding_vocabulary(conn)
    conn.execute(
        "INSERT INTO runs("
        "id,area_name,bbox_json,cell_km,depth,queries_json,scraper_image,config_json,"
        "raw_path,status,started_at"
        ") VALUES (?,?,?,?,?,?,?,?,?,'running',?)",
        (
            "review-cli-run",
            "review-cli",
            '{"max_lat":22,"max_lon":40,"min_lat":21,"min_lon":39}',
            2.0,
            1,
            '["restaurant"]',
            "gosom/google-maps-scraper:v1.18.1",
            '{"strict_bounds":true}',
            "/evidence/review-cli-run.jsonl",
            "2026-09-27T07:00:00+00:00",
        ),
    )
    conn.commit()
    ingest_records(
        conn,
        "review-cli-run",
        [
            {
                "place_id": "place-review-cli",
                "cid": "cid-review-cli",
                "data_id": "data-review-cli",
                "title": "Review CLI Restaurant",
                "category": "Restaurant",
                "address": "Review CLI Street",
                "latitude": 21.55,
                "longitude": 39.18,
                "website": "https://review-cli.example",
                "review_rating": 5.0,
                "review_count": 1,
                "status": "Open",
                "link": "https://maps.example/review-cli",
                "user_reviews": [
                    {
                        "review_id": "review-cli-001",
                        "source": "Google",
                        "Rating": 5,
                        "Description": "Excellent",
                        "language": "en",
                        "text_original": "Excellent",
                        "posted_at_unix_micros": 1_756_684_800_000_000,
                    }
                ],
            }
        ],
        finalize_run=("complete", 0, None),
    )
    row = conn.execute("SELECT id,canonical_key FROM businesses").fetchone()
    business_id, canonical_key = int(row[0]), str(row[1])
    backfill_maps_business_understanding(conn)
    conn.close()
    return business_id, canonical_key


def test_cli_extracts_by_business_id_and_prints_stable_json(
    tmp_path: Path, capsys
) -> None:
    db = tmp_path / "review-cli.sqlite"
    business_id, _canonical_key = _prepare_db(db)

    assert reviews_main(["--db", str(db), "--business-id", str(business_id), "--pretty"]) == 0
    first = json.loads(capsys.readouterr().out)

    assert first["business_id"] == business_id
    assert first["source_review_records"] == 1
    assert first["review_evidence_records"] == 1
    assert first["evidence_items_created"] == 1
    assert first["observations_created"] == 1
    assert first["already_extracted"] is False

    assert reviews_main(["--db", str(db), "--business-id", str(business_id)]) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["session_id"] == first["session_id"]
    assert second["evidence_items_created"] == 0
    assert second["observations_created"] == 0
    assert second["already_extracted"] is True


def test_cli_accepts_canonical_key_and_rejects_unknown_business(
    tmp_path: Path, capsys
) -> None:
    db = tmp_path / "review-cli-key.sqlite"
    _business_id, canonical_key = _prepare_db(db)

    assert reviews_main(["--db", str(db), "--canonical-key", canonical_key]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["review_evidence_records"] == 1

    assert reviews_main(["--db", str(db), "--business-id", "999999"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "review extraction failed:" in captured.err
