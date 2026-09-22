import argparse
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import sara.cli as cli
import sara.scraper as scraper
from sara.cli import cmd_collect
from sara.config import AreaConfig, BoundingBox
from sara.scraper import expected_resume_input_ids, load_resume_completed_input_ids
from sara.storage import connect


def _collect_args(tmp_path, *, run_id="guard", lang="en", no_resume=False):
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
        run_id=run_id,
        queries=str(query_file),
        cell_km=2.0,
        depth=2,
        concurrency=1,
        browser_pool_size=1,
        pages_per_browser=1,
        lang=lang,
        zoom=15,
        image="gosom/google-maps-scraper:v1.18.1",
        proxy_file=None,
        output_dir=str(tmp_path / "output"),
        no_resume=no_resume,
        include_out_of_bounds=False,
        dry_run=False,
    )


def _stats():
    return SimpleNamespace(
        raw_records=20,
        accepted_records=2,
        out_of_bounds_records=18,
        unlocated_records=0,
        unidentified_records=0,
        unique_seen=2,
        new_businesses=0,
    )


def test_expected_resume_id_matches_upstream_smoke_job():
    area = AreaConfig("smoke", BoundingBox(21.52, 39.17, 21.535, 39.185))

    assert expected_resume_input_ids(area, ["restaurant"], 2.0) == {
        "resume:9467a14e5b28588a84acfd173c50d808ee059cc5e55ab076080420722a8658a7"
    }


def test_expected_resume_ids_reject_zero_cell_grid():
    area = AreaConfig("tiny", BoundingBox(0, 0, 0.001, 0.001))

    with pytest.raises(ValueError, match="0 cells"):
        expected_resume_input_ids(area, ["restaurant"], 1.0)


def test_load_resume_completed_ids_from_readable_sidecar(tmp_path):
    output = tmp_path / "results.jsonl"
    output.write_text("", encoding="utf-8")
    state = tmp_path / "results.jsonl.resume.json"
    state.write_text(
        json.dumps({"version": 1, "completed_inputs": ["resume:abc"]}),
        encoding="utf-8",
    )

    assert load_resume_completed_input_ids(output, "image") == {"resume:abc"}


def test_root_owned_resume_sidecar_falls_back_to_isolated_container(tmp_path, monkeypatch):
    output = tmp_path / "results.jsonl"
    output.write_text("", encoding="utf-8")
    state_path = Path(str(output) + ".resume.json")
    state_path.write_text("unreadable-on-host", encoding="utf-8")
    original_read_text = Path.read_text
    seen = []

    def deny_state_read(self, *args, **kwargs):
        if self == state_path:
            raise PermissionError("root-owned")
        return original_read_text(self, *args, **kwargs)

    def fake_container_read(path, image):
        seen.append((path, image))
        return json.dumps({"version": 1, "completed_inputs": ["resume:abc"]})

    monkeypatch.setattr(Path, "read_text", deny_state_read)
    monkeypatch.setattr(scraper, "_read_file_via_container", fake_container_read)

    assert load_resume_completed_input_ids(output, "image") == {"resume:abc"}
    assert seen == [(state_path, "image")]


def test_container_sidecar_reader_is_network_disabled_and_read_only(tmp_path, monkeypatch):
    state_path = tmp_path / "results.jsonl.resume.json"
    seen = {}

    monkeypatch.setattr(scraper, "shutil_which", lambda binary: "/usr/bin/docker" if binary == "docker" else None)

    def fake_run(command, **kwargs):
        seen["command"] = command
        seen["kwargs"] = kwargs
        return SimpleNamespace(returncode=0, stdout="state", stderr="")

    monkeypatch.setattr(scraper.subprocess, "run", fake_run)

    assert scraper._read_file_via_container(state_path, "image:tag") == "state"
    command = seen["command"]
    assert command[:7] == [
        "docker", "run", "--rm", "--network", "none", "--entrypoint", "/bin/cat"
    ]
    assert f"{tmp_path.resolve()}:/out:ro" in command
    assert command[-2:] == ["image:tag", "/out/results.jsonl.resume.json"]
    assert seen["kwargs"] == {"check": False, "capture_output": True, "text": True}


def test_missing_resume_sidecar_is_incomplete_not_success(tmp_path):
    output = tmp_path / "results.jsonl"
    output.write_text("", encoding="utf-8")

    assert load_resume_completed_input_ids(output, "image") == set()


def test_invalid_resume_state_is_rejected(tmp_path):
    output = tmp_path / "results.jsonl"
    output.write_text("", encoding="utf-8")
    state = tmp_path / "results.jsonl.resume.json"
    state.write_text(json.dumps({"version": 2, "completed_inputs": []}), encoding="utf-8")

    with pytest.raises(RuntimeError, match="unsupported resume state version"):
        load_resume_completed_input_ids(output, "image")


def test_zero_exit_with_incomplete_resume_state_is_interrupted_and_not_ingested(tmp_path, monkeypatch):
    args = _collect_args(tmp_path)
    ingested = False

    monkeypatch.setattr(cli, "run_scraper", lambda _command: 0)
    monkeypatch.setattr(cli, "load_resume_completed_input_ids", lambda _output, _image: set())

    def forbidden_ingest(*_args, **_kwargs):
        nonlocal ingested
        ingested = True
        pytest.fail("partial output must not be ingested")

    monkeypatch.setattr(cli, "ingest_records", forbidden_ingest)

    assert cmd_collect(args) == 130
    assert ingested is False

    row = connect(args.db).execute("SELECT status, exit_code, error, raw_records FROM runs WHERE id = 'guard'").fetchone()
    assert row["status"] == "interrupted"
    assert row["exit_code"] == 0
    assert "completed 0/1" in row["error"]
    assert row["raw_records"] == 0


def test_unexpected_resume_identity_fails_closed(tmp_path, monkeypatch):
    args = _collect_args(tmp_path)
    monkeypatch.setattr(cli, "run_scraper", lambda _command: 0)
    monkeypatch.setattr(cli, "load_resume_completed_input_ids", lambda _output, _image: {"resume:foreign"})
    monkeypatch.setattr(cli, "ingest_records", lambda *_args, **_kwargs: pytest.fail("must not ingest"))

    assert cmd_collect(args) == 1

    row = connect(args.db).execute("SELECT status, error FROM runs WHERE id = 'guard'").fetchone()
    assert row["status"] == "failed"
    assert "unexpected completed input" in row["error"]


def test_complete_resume_state_allows_ingest_with_atomic_finalization(tmp_path, monkeypatch):
    args = _collect_args(tmp_path)
    area = AreaConfig("smoke", BoundingBox(21.52, 39.17, 21.535, 39.185))
    completed = set(expected_resume_input_ids(area, ["restaurant"], 2.0))
    calls = []

    monkeypatch.setattr(cli, "run_scraper", lambda _command: 0)
    monkeypatch.setattr(cli, "load_resume_completed_input_ids", lambda _output, _image: set(completed))

    def fake_ingest(conn, run_id, *_args, finalize_run=None, **_kwargs):
        calls.append(finalize_run)
        assert finalize_run == ("complete", 0, None)
        status, exit_code, error = finalize_run
        conn.execute(
            "UPDATE runs SET status = ?, exit_code = ?, error = ? WHERE id = ?",
            (status, exit_code, error, run_id),
        )
        conn.commit()
        return _stats()

    monkeypatch.setattr(cli, "ingest_records", fake_ingest)

    assert cmd_collect(args) == 0
    assert calls == [("complete", 0, None)]
    row = connect(args.db).execute("SELECT status, exit_code, error FROM runs WHERE id = 'guard'").fetchone()
    assert row["status"] == "complete"
    assert row["exit_code"] == 0
    assert row["error"] is None


def test_changed_configuration_after_interruption_is_rejected_before_scraper(tmp_path, monkeypatch):
    first = _collect_args(tmp_path, run_id="config-guard", lang="en")
    calls = []

    def fake_scraper(_command):
        calls.append(True)
        return 0

    monkeypatch.setattr(cli, "run_scraper", fake_scraper)
    monkeypatch.setattr(cli, "load_resume_completed_input_ids", lambda _output, _image: set())
    monkeypatch.setattr(cli, "ingest_records", lambda *_args, **_kwargs: pytest.fail("must not ingest"))

    assert cmd_collect(first) == 130
    assert calls == [True]

    changed = _collect_args(tmp_path, run_id="config-guard", lang="ar")
    assert cmd_collect(changed) == 2
    assert calls == [True]

    row = connect(first.db).execute("SELECT status FROM runs WHERE id = 'config-guard'").fetchone()
    assert row["status"] == "interrupted"


def test_no_resume_collection_is_rejected_without_mutation(tmp_path):
    args = _collect_args(tmp_path, no_resume=True)

    assert cmd_collect(args) == 2
    assert not (tmp_path / "sara.db").exists()
    assert not (tmp_path / "output").exists()
