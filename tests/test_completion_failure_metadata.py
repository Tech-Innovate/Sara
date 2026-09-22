import argparse
import json

import pytest

import sara.cli as cli
from sara.cli import cmd_collect
from sara.storage import connect


def test_completion_proof_failure_preserves_zero_scraper_exit_code(tmp_path, monkeypatch):
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
    args = argparse.Namespace(
        db=str(tmp_path / "sara.db"),
        area=str(area_file),
        run_id="proof-failure",
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

    monkeypatch.setattr(cli, "run_scraper", lambda _command: 0)
    monkeypatch.setattr(
        cli,
        "load_resume_completed_input_ids",
        lambda _output, _image: (_ for _ in ()).throw(RuntimeError("malformed state")),
    )
    monkeypatch.setattr(cli, "ingest_records", lambda *_args, **_kwargs: pytest.fail("must not ingest"))

    assert cmd_collect(args) == 1
    row = connect(args.db).execute(
        "SELECT status, exit_code, error FROM runs WHERE id = 'proof-failure'"
    ).fetchone()
    assert row["status"] == "failed"
    assert row["exit_code"] == 0
    assert "completion verification failed after scraper exit 0" in row["error"]
