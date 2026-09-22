import pytest

from sara.storage import connect, ingest_records


def _insert_running_run(conn, tmp_path, run_id="r1"):
    conn.execute(
        """
        INSERT INTO runs(
            id, area_name, bbox_json, cell_km, depth, queries_json,
            scraper_image, config_json, raw_path, status, started_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
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


def test_keyboard_interrupt_rolls_back_partial_ingestion_before_status_commit(tmp_path):
    conn = connect(tmp_path / "sara.db")
    _insert_running_run(conn, tmp_path)

    def interrupted_records():
        yield {"place_id": "p1", "title": "Partial"}
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        ingest_records(
            conn,
            "r1",
            interrupted_records(),
            finalize_run=("complete", 0, None),
        )

    # Simulate cmd_collect's interrupt handler committing only lifecycle state.
    conn.execute("UPDATE runs SET status = 'interrupted' WHERE id = 'r1'")
    conn.commit()

    assert conn.execute("SELECT COUNT(*) AS n FROM businesses").fetchone()["n"] == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM run_businesses").fetchone()["n"] == 0
    row = conn.execute(
        "SELECT status, raw_records, accepted_records, unique_seen, new_businesses FROM runs WHERE id = 'r1'"
    ).fetchone()
    assert dict(row) == {
        "status": "interrupted",
        "raw_records": 0,
        "accepted_records": 0,
        "unique_seen": 0,
        "new_businesses": 0,
    }


def test_successful_ingestion_commits_complete_lifecycle_in_same_transaction(tmp_path):
    db = tmp_path / "sara.db"
    conn = connect(db)
    _insert_running_run(conn, tmp_path)

    stats = ingest_records(
        conn,
        "r1",
        [{"place_id": "p1", "title": "Complete"}],
        finalize_run=("complete", 0, None),
    )

    assert stats.raw_records == 1
    assert stats.accepted_records == 1
    assert stats.unique_seen == 1

    # Read through a separate connection so the assertion proves the lifecycle
    # and canonical rows were committed together, not merely visible locally.
    verify = connect(db)
    row = verify.execute(
        "SELECT status, exit_code, error, raw_records, accepted_records, unique_seen FROM runs WHERE id = 'r1'"
    ).fetchone()
    assert dict(row) == {
        "status": "complete",
        "exit_code": 0,
        "error": None,
        "raw_records": 1,
        "accepted_records": 1,
        "unique_seen": 1,
    }
    assert verify.execute("SELECT COUNT(*) AS n FROM businesses").fetchone()["n"] == 1
