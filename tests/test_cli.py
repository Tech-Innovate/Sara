import argparse
import json

import pytest

from sara.cli import RunLock, _ensure_run, _run_config_json, _validate_run_id, cmd_collect, cmd_ingest
from sara.config import AreaConfig, BoundingBox
from sara.scraper import ScrapeOptions
from sara.storage import connect


def test_run_id_rejects_path_components():
    with pytest.raises(ValueError):
        _validate_run_id("../escape")
    assert _validate_run_id("run-2026_09.22") == "run-2026_09.22"


def test_run_lock_rejects_second_live_writer(tmp_path):
    first = RunLock(tmp_path / ".sara.lock")
    second = RunLock(tmp_path / ".sara.lock")
    first.acquire()
    try:
        with pytest.raises(RuntimeError, match="already locked"):
            second.acquire()
    finally:
        first.release()


def test_resume_configuration_change_is_rejected(tmp_path):
    conn = connect(tmp_path / "sara.db")
    area = AreaConfig("x", BoundingBox(0, 0, 1, 1))
    queries = ["dentist"]
    first_options = ScrapeOptions(zoom=15)
    second_options = ScrapeOptions(zoom=16)
    first_config = _run_config_json(area=area, queries=queries, options=first_options, strict_bounds=True)
    second_config = _run_config_json(area=area, queries=queries, options=second_options, strict_bounds=True)

    _ensure_run(
        conn,
        run_id="r1",
        area=area,
        queries=queries,
        cell_km=1.0,
        depth=5,
        image=first_options.image,
        raw_path=str(tmp_path / "results.jsonl"),
        config_json=first_config,
        status="running",
    )

    with pytest.raises(ValueError, match="different crawl configuration"):
        _ensure_run(
            conn,
            run_id="r1",
            area=area,
            queries=queries,
            cell_km=1.0,
            depth=5,
            image=second_options.image,
            raw_path=str(tmp_path / "results.jsonl"),
            config_json=second_config,
            status="running",
        )


def test_ingest_rejects_file_not_recorded_for_run(tmp_path):
    db = tmp_path / "sara.db"
    expected = (tmp_path / "results.jsonl").resolve()
    expected.write_text("", encoding="utf-8")
    other = tmp_path / "other.jsonl"
    other.write_text("", encoding="utf-8")
    conn = connect(db)
    bbox = {"min_lat": 0, "min_lon": 0, "max_lat": 1, "max_lon": 1}
    conn.execute(
        """
        INSERT INTO runs(id, area_name, bbox_json, cell_km, depth, queries_json,
                         scraper_image, config_json, raw_path, status, started_at)
        VALUES ('r1', 'x', ?, 1.0, 5, '[]', 'image', ?, ?, 'complete', 'now')
        """,
        (json.dumps(bbox), json.dumps({"strict_bounds": True}), str(expected)),
    )
    conn.commit()

    result = cmd_ingest(argparse.Namespace(db=str(db), run_id="r1", file=str(other)))

    assert result == 2


def test_new_run_rejects_preexisting_scraper_output(tmp_path):
    area_file = tmp_path / "area.json"
    area_file.write_text(
        json.dumps({
            "name": "x",
            "bbox": {"min_lat": 0, "min_lon": 0, "max_lat": 1, "max_lon": 1},
        }),
        encoding="utf-8",
    )
    query_file = tmp_path / "queries.txt"
    query_file.write_text("dentist\n", encoding="utf-8")
    output_root = tmp_path / "output"
    run_dir = output_root / "r1"
    run_dir.mkdir(parents=True)
    (run_dir / "results.jsonl").write_text("{}\n", encoding="utf-8")

    args = argparse.Namespace(
        db=str(tmp_path / "sara.db"),
        area=str(area_file),
        run_id="r1",
        queries=str(query_file),
        cell_km=1.0,
        depth=5,
        concurrency=4,
        browser_pool_size=1,
        pages_per_browser=4,
        lang="en",
        zoom=15,
        image="gosom/google-maps-scraper:v1.18.1",
        proxy_file=None,
        output_dir=str(output_root),
        no_resume=False,
        include_out_of_bounds=False,
        dry_run=False,
    )

    assert cmd_collect(args) == 2
