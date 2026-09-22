import pytest

from sara.config import BoundingBox
from sara.storage import IdentityConflict, connect, ingest_records


def make_run(conn, run_id, started_at="now"):
    conn.execute(
        """
        INSERT INTO runs(id, area_name, bbox_json, cell_km, depth, queries_json,
                         scraper_image, status, started_at)
        VALUES (?, 'x', '{}', 1.0, 5, '[]', 'image', 'running', ?)
        """,
        (run_id, started_at),
    )
    conn.commit()


def test_deduplicates_across_runs_by_place_id(tmp_path):
    conn = connect(tmp_path / "sara.db")
    make_run(conn, "r1")
    make_run(conn, "r2")

    a = {"place_id": "abc", "title": "A", "latitude": 1, "longitude": 2}
    b = {"place_id": "abc", "title": "A updated", "latitude": 1, "longitude": 2}

    s1 = ingest_records(conn, "r1", [a, a])
    s2 = ingest_records(conn, "r2", [b])

    assert s1.raw_records == 2
    assert s1.unique_seen == 1
    assert s1.new_businesses == 1
    assert s2.new_businesses == 0
    assert conn.execute("SELECT COUNT(*) FROM businesses").fetchone()[0] == 1
    assert conn.execute("SELECT title FROM businesses").fetchone()[0] == "A updated"


def test_merges_rows_when_identifiers_converge_and_refreshes_history(tmp_path):
    conn = connect(tmp_path / "sara.db")
    make_run(conn, "r1")
    make_run(conn, "r2")
    make_run(conn, "r3")

    ingest_records(conn, "r1", [{"place_id": "p1", "title": "Alpha"}])
    ingest_records(conn, "r2", [{"cid": "c1", "title": "Alpha"}])
    assert conn.execute("SELECT COUNT(*) FROM businesses").fetchone()[0] == 2

    stats = ingest_records(conn, "r3", [{"place_id": "p1", "cid": "c1", "title": "Alpha"}])

    assert stats.new_businesses == 0
    assert conn.execute("SELECT COUNT(*) FROM businesses").fetchone()[0] == 1
    row = conn.execute("SELECT place_id, cid FROM businesses").fetchone()
    assert tuple(row) == ("p1", "c1")
    assert conn.execute("SELECT COUNT(*) FROM run_businesses").fetchone()[0] == 3
    r1 = conn.execute("SELECT unique_seen, new_businesses FROM runs WHERE id = 'r1'").fetchone()
    r2 = conn.execute("SELECT unique_seen, new_businesses FROM runs WHERE id = 'r2'").fetchone()
    assert tuple(r1) == (1, 1)
    assert tuple(r2) == (1, 0)


def test_same_run_convergence_counts_one_canonical_business(tmp_path):
    conn = connect(tmp_path / "sara.db")
    make_run(conn, "r1")
    records = [
        {"place_id": "p1", "title": "Alpha"},
        {"cid": "c1", "title": "Alpha"},
        {"place_id": "p1", "cid": "c1", "title": "Alpha"},
    ]

    stats = ingest_records(conn, "r1", records)

    assert stats.raw_records == 3
    assert stats.accepted_records == 3
    assert stats.unique_seen == 1
    assert stats.new_businesses == 1
    assert conn.execute("SELECT COUNT(*) FROM businesses").fetchone()[0] == 1


def test_reingest_is_metric_idempotent(tmp_path):
    conn = connect(tmp_path / "sara.db")
    make_run(conn, "r1")
    records = [{"place_id": "p1", "title": "Alpha"}]

    first = ingest_records(conn, "r1", records)
    second = ingest_records(conn, "r1", records)

    assert first.new_businesses == 1
    assert second.new_businesses == 1
    assert second.unique_seen == 1


def test_historical_reingest_does_not_regress_latest_state(tmp_path):
    conn = connect(tmp_path / "sara.db")
    old_time = "2026-01-01T00:00:00+00:00"
    new_time = "2026-02-01T00:00:00+00:00"
    make_run(conn, "old", old_time)
    make_run(conn, "new", new_time)

    ingest_records(
        conn,
        "new",
        [{"place_id": "p1", "cid": "c1", "title": "Current", "phone": "+200"}],
    )
    ingest_records(
        conn,
        "old",
        [{"place_id": "p1", "cid": "c1", "title": "Historical", "phone": "+100"}],
    )

    business = conn.execute(
        """
        SELECT title, phone, first_run_id, last_run_id, first_seen_at, last_seen_at, raw_json
        FROM businesses WHERE place_id = 'p1'
        """
    ).fetchone()
    assert business["title"] == "Current"
    assert business["phone"] == "+200"
    assert business["first_run_id"] == "old"
    assert business["last_run_id"] == "new"
    assert business["first_seen_at"] == old_time
    assert business["last_seen_at"] == new_time
    assert '"title": "Current"' in business["raw_json"]

    old_metrics = conn.execute("SELECT new_businesses FROM runs WHERE id = 'old'").fetchone()
    new_metrics = conn.execute("SELECT new_businesses FROM runs WHERE id = 'new'").fetchone()
    assert old_metrics["new_businesses"] == 1
    assert new_metrics["new_businesses"] == 0


def test_conflicting_strong_identifiers_roll_back(tmp_path):
    conn = connect(tmp_path / "sara.db")
    make_run(conn, "r1", "2026-01-01T00:00:00+00:00")
    make_run(conn, "r2", "2026-02-01T00:00:00+00:00")
    ingest_records(conn, "r1", [{"place_id": "p1", "cid": "c1", "title": "Alpha"}])

    with pytest.raises(IdentityConflict, match="strong identity conflict"):
        ingest_records(conn, "r2", [{"place_id": "p1", "cid": "different", "title": "Wrong"}])

    business = conn.execute("SELECT cid, title, last_run_id FROM businesses WHERE place_id = 'p1'").fetchone()
    assert tuple(business) == ("c1", "Alpha", "r1")
    r2 = conn.execute("SELECT raw_records, unique_seen, new_businesses FROM runs WHERE id = 'r2'").fetchone()
    assert tuple(r2) == (0, 0, 0)


def test_strict_bbox_excludes_outside_and_unlocated_rows(tmp_path):
    conn = connect(tmp_path / "sara.db")
    make_run(conn, "r1")
    bbox = BoundingBox(0, 0, 1, 1)
    records = [
        {"place_id": "inside", "latitude": 0.5, "longitude": 0.5},
        {"place_id": "outside", "latitude": 2, "longitude": 2},
        {"place_id": "missing"},
    ]

    stats = ingest_records(conn, "r1", records, bbox=bbox)

    assert stats.raw_records == 3
    assert stats.accepted_records == 1
    assert stats.out_of_bounds_records == 1
    assert stats.unlocated_records == 1
    assert stats.unique_seen == 1
    assert conn.execute("SELECT place_id FROM businesses").fetchone()[0] == "inside"


def test_unidentifiable_record_is_reported_not_collapsed(tmp_path):
    conn = connect(tmp_path / "sara.db")
    make_run(conn, "r1")

    stats = ingest_records(conn, "r1", [{}])

    assert stats.unidentified_records == 1
    assert stats.accepted_records == 0
    assert conn.execute("SELECT COUNT(*) FROM businesses").fetchone()[0] == 0


def test_ingest_rolls_back_when_record_stream_fails(tmp_path):
    conn = connect(tmp_path / "sara.db")
    make_run(conn, "r1")

    def records():
        yield {"place_id": "p1", "title": "Alpha"}
        raise ValueError("broken JSONL")

    with pytest.raises(ValueError, match="broken JSONL"):
        ingest_records(conn, "r1", records())

    assert conn.execute("SELECT COUNT(*) FROM businesses").fetchone()[0] == 0
    run = conn.execute("SELECT raw_records, unique_seen, new_businesses FROM runs WHERE id = 'r1'").fetchone()
    assert tuple(run) == (0, 0, 0)
