import pytest

from sara.config import BoundingBox
from sara.storage import connect, ingest_records


def make_run(conn, run_id):
    conn.execute(
        """
        INSERT INTO runs(id, area_name, bbox_json, cell_km, depth, queries_json,
                         scraper_image, status, started_at)
        VALUES (?, 'x', '{}', 1.0, 5, '[]', 'image', 'running', 'now')
        """,
        (run_id,),
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
