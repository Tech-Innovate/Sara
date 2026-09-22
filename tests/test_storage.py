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


def test_merges_rows_when_identifiers_converge(tmp_path):
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
