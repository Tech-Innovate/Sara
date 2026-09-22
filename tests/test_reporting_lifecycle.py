import argparse
import json

import sara.cli as cli
from sara.cli import cmd_collect
from sara.config import AreaConfig, BoundingBox
from sara.scraper import expected_resume_input_ids
from sara.storage import connect


def _args(tmp_path):
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
    queries = tmp_path / "queries.txt"
    queries.write_text("restaurant\n", encoding="utf-8")
    return argparse.Namespace(
        db=str(tmp_path / "sara.db"),
        area=str(area_file),
        run_id="reporting",
        queries=str(queries),
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


def test_stdout_reporting_failure_does_not_downgrade_complete_run(tmp_path, monkeypatch):
    args = _args(tmp_path)
    area = AreaConfig("smoke", BoundingBox(21.52, 39.17, 21.535, 39.185))
    completed = set(expected_resume_input_ids(area, ["restaurant"], 2.0))

    monkeypatch.setattr(cli, "run_scraper", lambda _command: 0)
    monkeypatch.setattr(cli, "load_resume_completed_input_ids", lambda _output, _image: set(completed))
    monkeypatch.setattr(cli, "_print_stats", lambda _stats: (_ for _ in ()).throw(BrokenPipeError("closed")))

    assert cmd_collect(args) == 1
    row = connect(args.db).execute(
        "SELECT status, exit_code, error, raw_records FROM runs WHERE id = 'reporting'"
    ).fetchone()
    assert row["status"] == "complete"
    assert row["exit_code"] == 0
    assert row["error"] is None
    assert row["raw_records"] == 0


def test_reporting_interrupt_does_not_reclassify_complete_run(tmp_path, monkeypatch):
    args = _args(tmp_path)
    area = AreaConfig("smoke", BoundingBox(21.52, 39.17, 21.535, 39.185))
    completed = set(expected_resume_input_ids(area, ["restaurant"], 2.0))

    monkeypatch.setattr(cli, "run_scraper", lambda _command: 0)
    monkeypatch.setattr(cli, "load_resume_completed_input_ids", lambda _output, _image: set(completed))

    def interrupt(_stats):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_print_stats", interrupt)

    assert cmd_collect(args) == 130
    row = connect(args.db).execute(
        "SELECT status, exit_code, error, raw_records FROM runs WHERE id = 'reporting'"
    ).fetchone()
    assert row["status"] == "complete"
    assert row["exit_code"] == 0
    assert row["error"] is None
    assert row["raw_records"] == 0
