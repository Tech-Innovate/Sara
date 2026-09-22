import argparse
import json

import sara.cli as cli
from sara.cli import cmd_collect, cmd_ingest
from sara.config import AreaConfig, BoundingBox
from sara.scraper import expected_resume_input_ids
from sara.storage import connect


def _collect_args(tmp_path):
    area_file = tmp_path / "area.json"
    area_file.write_text(
        json.dumps({
            "name": "smoke",
            "bbox": {
                "min_lat": 21.52,
                "min_lon": 39.17,
                "max_lat": 21.535,
                "max_lon": 39.185,
            },
        }),
        encoding="utf-8",
    )
    query_file = tmp_path / "queries.txt"
    query_file.write_text("restaurant\n", encoding="utf-8")
    return argparse.Namespace(
        db=str(tmp_path / "sara.db"),
        area=str(area_file),
        run_id="exit-proof",
        queries=str(query_file),
        cell_km=2.0,
        depth=2,
        concurrency=1,
        browser_pool_size=1,
        pages_per_browser=1,
        lang="en",
        zoom=15,
        image="gosom/google-maps-scraper:v1.18.1",
        proxy_file=None,
        output_dir=str(tmp_path / "output"),
        no_resume=False,
        include_out_of_bounds=False,
        dry_run=False,
    )


def test_post_scraper_ingest_failure_preserves_zero_exit_code(tmp_path, monkeypatch):
    args = _collect_args(tmp_path)
    area = AreaConfig("smoke", BoundingBox(21.52, 39.17, 21.535, 39.185))
    expected = expected_resume_input_ids(area, ["restaurant"], 2.0)

    monkeypatch.setattr(cli, "run_scraper", lambda _command: 0)
    monkeypatch.setattr(cli, "load_resume_completed_input_ids", lambda _output, _image: expected)
    monkeypatch.setattr(
        cli,
        "ingest_records",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("ingest failed after scrape")),
    )

    assert cmd_collect(args) == 1
    row = connect(args.db).execute(
        "SELECT status, exit_code, error FROM runs WHERE id = 'exit-proof'"
    ).fetchone()
    assert row["status"] == "failed"
    assert row["exit_code"] == 0
    assert "ingest failed after scrape" in row["error"]


def test_verified_reingest_preserves_recorded_scraper_exit_code(tmp_path):
    db = tmp_path / "sara.db"
    raw = (tmp_path / "results.jsonl").resolve()
    raw.write_text("", encoding="utf-8")
    area = AreaConfig("smoke", BoundingBox(21.52, 39.17, 21.535, 39.185))
    queries = ["restaurant"]
    expected = expected_resume_input_ids(area, queries, 2.0)
    raw.with_name(raw.name + ".resume.json").write_text(
        json.dumps({"version": 1, "completed_inputs": sorted(expected)}),
        encoding="utf-8",
    )

    conn = connect(db)
    conn.execute(
        """
        INSERT INTO runs(
            id, area_name, bbox_json, cell_km, depth, queries_json,
            scraper_image, config_json, raw_path, status, started_at, exit_code
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "r1",
            area.name,
            json.dumps(area.bbox.__dict__, sort_keys=True),
            2.0,
            2,
            json.dumps(queries),
            "gosom/google-maps-scraper:v1.18.1",
            json.dumps({"strict_bounds": True, "resume": True}),
            str(raw),
            "failed",
            "2026-09-22T00:00:00+00:00",
            7,
        ),
    )
    conn.commit()

    assert cmd_ingest(argparse.Namespace(db=str(db), run_id="r1", file=str(raw))) == 0
    row = connect(db).execute("SELECT status, exit_code, error FROM runs WHERE id = 'r1'").fetchone()
    assert row["status"] == "complete"
    assert row["exit_code"] == 7
    assert row["error"] is None
