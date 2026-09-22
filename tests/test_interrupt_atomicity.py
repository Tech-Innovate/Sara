import pytest

from sara.storage import connect, ingest_records


def test_keyboard_interrupt_rolls_back_partial_ingestion_before_status_commit(tmp_path):
    conn = connect(tmp_path / "sara.db")
    conn.execute(
        """
        INSERT INTO runs(
            id, area_name, bbox_json, cell_km, depth, queries_json,
            scraper_image, config_json, raw_path, status, started_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "r1",
            "smoke",
            '{"min_lat":0,"min_lon":0,"max_lat":1,"max_lon":1}',
            1.0,
            2,
            '["restaurant"]',
            "image",
            '{"resume":true,"strict_bounds":true}',
            str(tmp_path / "results.jsonl"),
            "running",
            "2026-09-22T00:00:00+00:00",
        ),
    )
    conn.commit()

    def interrupted_records():
        yield {"place_id": "p1", "title": "Partial"}
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        ingest_records(conn, "r1", interrupted_records())

    # Simulate cmd_collect's interrupt handler committing only lifecycle state.
    conn.execute("UPDATE runs SET status = 'interrupted' WHERE id = 'r1'")
    conn.commit()

    assert conn.execute("SELECT COUNT(*) AS n FROM businesses").fetchone()["n"] == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM run_businesses").fetchone()["n"] == 0
    row = conn.execute(
        "SELECT raw_records, accepted_records, unique_seen, new_businesses FROM runs WHERE id = 'r1'"
    ).fetchone()
    assert dict(row) == {
        "raw_records": 0,
        "accepted_records": 0,
        "unique_seen": 0,
        "new_businesses": 0,
    }
