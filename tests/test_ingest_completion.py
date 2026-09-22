import argparse
import json

from sara.cli import cmd_ingest
from sara.config import AreaConfig, BoundingBox
from sara.scraper import expected_resume_input_ids
from sara.storage import connect


def _insert_run(tmp_path, *, status="interrupted"):
    db = tmp_path / "sara.db"
    raw = (tmp_path / "results.jsonl").resolve()
    raw.write_text("", encoding="utf-8")
    area = AreaConfig("smoke", BoundingBox(21.52, 39.17, 21.535, 39.185))
    queries = ["restaurant"]
    config = {"strict_bounds": True, "resume": True}

    conn = connect(db)
    conn.execute(
        """
        INSERT INTO runs(
            id, area_name, bbox_json, cell_km, depth, queries_json,
            scraper_image, config_json, raw_path, status, started_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "r1",
            area.name,
            json.dumps(area.bbox.__dict__, sort_keys=True),
            2.0,
            2,
            json.dumps(queries),
            "gosom/google-maps-scraper:v1.18.1",
            json.dumps(config),
            str(raw),
            status,
            "2026-09-22T00:00:00+00:00",
        ),
    )
    conn.commit()
    return db, raw, area, queries


def test_ingest_cannot_bypass_incomplete_crawl(tmp_path):
    db, raw, _area, _queries = _insert_run(tmp_path, status="interrupted")

    result = cmd_ingest(argparse.Namespace(db=str(db), run_id="r1", file=str(raw)))

    assert result == 2
    conn = connect(db)
    row = conn.execute("SELECT status, raw_records FROM runs WHERE id = 'r1'").fetchone()
    assert row["status"] == "interrupted"
    assert row["raw_records"] == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM businesses").fetchone()["n"] == 0


def test_verified_reingest_can_recover_noncomplete_run(tmp_path):
    db, raw, area, queries = _insert_run(tmp_path, status="failed")
    expected = expected_resume_input_ids(area, queries, 2.0)
    state = raw.with_name(raw.name + ".resume.json")
    state.write_text(
        json.dumps({"version": 1, "completed_inputs": sorted(expected)}),
        encoding="utf-8",
    )

    result = cmd_ingest(argparse.Namespace(db=str(db), run_id="r1", file=str(raw)))

    assert result == 0
    row = connect(db).execute("SELECT status, exit_code, error FROM runs WHERE id = 'r1'").fetchone()
    assert row["status"] == "complete"
    assert row["exit_code"] == 0
    assert row["error"] is None


def test_legacy_complete_run_without_completion_evidence_is_not_reingested(tmp_path):
    db, raw, _area, _queries = _insert_run(tmp_path, status="complete")

    result = cmd_ingest(argparse.Namespace(db=str(db), run_id="r1", file=str(raw)))

    assert result == 2
    assert connect(db).execute("SELECT status FROM runs WHERE id = 'r1'").fetchone()["status"] == "complete"
