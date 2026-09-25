import argparse
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

import sara.cli as cli
from sara.recovery import PlanRejected, parse_execution_plan, validate_digest_pinned_image
from sara.storage import (
    RecoverySchemaError,
    connect,
    connect_existing,
    set_recovery_fault_hook,
    clear_recovery_fault_hook,
)
from sara.scraper import build_docker_command, ScrapeOptions
from sara.config import AreaConfig, BoundingBox

from test_recovery_run import BBOX, DIGEST_IMAGE, build_db, make_plan, run_args, tamper


REAL_DIGEST_IMAGE = "gosom/google-maps-scraper@sha256:" + "b" * 64


class TestImageAndParserFixes:
    def test_hyphenated_digest_image_accepted(self):
        validate_digest_pinned_image(REAL_DIGEST_IMAGE)

    def test_leading_dash_rejected(self):
        with pytest.raises(PlanRejected):
            validate_digest_pinned_image("-evil@sha256:" + "b" * 64)

    def test_whitespace_rejected(self):
        with pytest.raises(PlanRejected):
            validate_digest_pinned_image("na me@sha256:" + "b" * 64)

    def test_tag_only_rejected(self):
        with pytest.raises(PlanRejected):
            validate_digest_pinned_image("gosom/google-maps-scraper:v1.18.1")

    def test_bad_digest_rejected(self):
        with pytest.raises(PlanRejected):
            validate_digest_pinned_image("gosom/google-maps-scraper@sha256:short")

    @pytest.mark.parametrize("field,value,where", [
        ("row", -1, "outside the plan grid"),
        ("column", -1, "outside the plan grid"),
        ("row", 99, "outside the plan grid"),
        ("column", 99, "outside the plan grid"),
    ])
    def test_out_of_range_row_column(self, tmp_path, field, value, where):
        data, _, _ = make_plan(tmp_path)
        tampered, _ = tamper(data, lambda p: p["bins"][0].__setitem__(field, value))
        with pytest.raises(PlanRejected, match=where):
            parse_execution_plan(tampered)

    def test_giant_integer_cell_km_rejected_cleanly(self, tmp_path):
        data, _, _ = make_plan(tmp_path)

        def mutate(payload):
            payload["source_run"]["cell_km"] = 10 ** 500
            payload["source_run"]["config"]["cell_km"] = 10 ** 500
        tampered, _ = tamper(data, mutate)
        with pytest.raises(PlanRejected):
            parse_execution_plan(tampered)

    def test_giant_integer_recovery_cell_rejected_cleanly(self, tmp_path):
        data, _, _ = make_plan(tmp_path)

        def mutate(payload):
            payload["recovery"]["cell_km"] = 10 ** 500
        tampered, _ = tamper(data, mutate)
        with pytest.raises(PlanRejected):
            parse_execution_plan(tampered)


class TestPreflightOptions:
    def _mutate_config(self, tmp_path, key, value):
        db = build_db(tmp_path / "sara.db")
        conn = connect(db)
        row = conn.execute("SELECT config_json FROM runs WHERE id='src-run'").fetchone()
        config = json.loads(row[0])
        config[key] = value
        conn.execute(
            "UPDATE runs SET config_json = ? WHERE id = 'src-run'",
            (json.dumps(config, sort_keys=True),),
        )
        conn.commit()
        conn.close()
        # regenerate plan from mutated DB
        area = tmp_path / "area2.json"
        area.write_text(json.dumps({"name": "x", "bbox": BBOX}), encoding="utf-8")
        q = tmp_path / "q2.txt"
        q.write_text("restaurant\n", encoding="utf-8")
        out = tmp_path / "plan2.json"
        assert cli.main([
            "--db", str(db), "recovery-plan", "--run-id", "src-run",
            "--recovery-cell-km", "1.0", "--tier-a-min", "2", "--tier-b-min", "1",
            "--policy-id", "p", "--output", str(out),
        ]) == 0
        return out.read_bytes(), hashlib.sha256(out.read_bytes()).hexdigest(), db

    @pytest.mark.parametrize("key,value", [
        ("concurrency", 0), ("browser_pool_size", 0), ("pages_per_browser", 0),
        ("zoom", 0), ("zoom", 22), ("lang", "  "),
    ])
    def test_invalid_scrape_options_rejected_before_writes(self, tmp_path, key, value):
        data, sha, db = self._mutate_config(tmp_path, key, value)
        assert not (tmp_path / "recovery").exists()
        assert cli.cmd_recovery_run(run_args(db, data, sha, tmp_path)) == 2
        assert not (tmp_path / "recovery").exists()

    def test_invalid_depth_rejected(self, tmp_path):
        db = build_db(tmp_path / "sara.db")
        conn = connect(db)
        for column in ("runs",):
            conn.execute(f"UPDATE {column} SET depth = 99 WHERE id = 'src-run'")
        row = conn.execute("SELECT config_json FROM runs WHERE id='src-run'").fetchone()
        config = json.loads(row[0]); config["depth"] = 99
        conn.execute("UPDATE runs SET config_json = ?", (json.dumps(config, sort_keys=True),))
        conn.commit(); conn.close()
        area = tmp_path / "a.json"; area.write_text(json.dumps({"name": "x", "bbox": BBOX}))
        q = tmp_path / "q.txt"; q.write_text("restaurant\n")
        out = tmp_path / "p.json"
        assert cli.main(["--db", str(db), "recovery-plan", "--run-id", "src-run",
                         "--recovery-cell-km", "1.0", "--tier-a-min", "2", "--tier-b-min", "1",
                         "--policy-id", "p", "--output", str(out)]) == 0
        assert cli.cmd_recovery_run(run_args(db, out.read_bytes(),
                        hashlib.sha256(out.read_bytes()).hexdigest(), tmp_path)) == 2


class TestStorageFixes:
    def test_connect_existing_refuses_to_create(self, tmp_path):
        missing = tmp_path / "nope" / "db.sqlite"
        with pytest.raises(FileNotFoundError):
            connect_existing(missing)
        assert not missing.exists()

    def test_nullability_violation_detected(self, tmp_path):
        db = build_db(tmp_path / "sara.db")
        conn = sqlite3.connect(db)
        conn.executescript("""
            CREATE TABLE recovery_executions (
                plan_sha256 TEXT PRIMARY KEY, source_run_id TEXT,
                plan_schema_version INTEGER NOT NULL, plan_kind TEXT NOT NULL,
                policy_id TEXT NOT NULL, output_root TEXT NOT NULL,
                plan_snapshot_path TEXT NOT NULL, selected_bins INTEGER NOT NULL,
                planned_searches INTEGER NOT NULL, status TEXT NOT NULL,
                started_at TEXT NOT NULL, finished_at TEXT, error TEXT, result_json TEXT,
                FOREIGN KEY(source_run_id) REFERENCES runs(id));
            CREATE TABLE recovery_execution_bins (
                plan_sha256 TEXT NOT NULL, row INTEGER NOT NULL, column INTEGER NOT NULL,
                tier TEXT NOT NULL, bbox_json TEXT NOT NULL, planned_searches INTEGER NOT NULL,
                run_id TEXT UNIQUE, container_name TEXT NOT NULL UNIQUE,
                PRIMARY KEY(plan_sha256, row, column),
                FOREIGN KEY(plan_sha256) REFERENCES recovery_executions(plan_sha256) ON DELETE CASCADE,
                FOREIGN KEY(run_id) REFERENCES runs(id));
        """)
        conn.commit(); conn.close()
        from sara.storage import ensure_recovery_schema, verify_recovery_schema
        c = connect_existing(db)
        c.execute("BEGIN IMMEDIATE")
        ensure_recovery_schema(c)
        with pytest.raises(RecoverySchemaError, match="NOT NULL"):
            verify_recovery_schema(c)
        c.rollback(); c.close()

    def test_sidecar_fallback_command_uses_digest_image(self, tmp_path, monkeypatch):
        """Force PermissionError via monkeypatch (cross-platform, hermetic)."""
        from sara import scraper
        captured = {}

        def fake_read(self, *args, **kwargs):
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr(Path, "read_text", fake_read)
        monkeypatch.setattr(scraper, "shutil_which", lambda b: "/usr/bin/docker")

        def fake_run(command, **kwargs):
            captured["command"] = list(command)

            class R:
                returncode = 0
                stdout = '{"version":1,"completed_inputs":[]}'
            return R()

        monkeypatch.setattr(scraper.subprocess, "run", fake_run)
        ids = scraper.load_resume_completed_input_ids(
            tmp_path / "results.jsonl",
            "gosom/google-maps-scraper@sha256:" + "b" * 64,
        )
        assert ids == set()
        cmd = captured["command"]
        assert any("@sha256:" in part for part in cmd)
        assert "--network" in cmd and "none" in cmd

class TestWindowsPathFake:
    def test_windows_style_bind_path_handled(self):
        """The volume-arg finder accepts Windows host paths ending in :/out."""
        from test_recovery_run_exec import FakeScraper

        parsed = FakeScraper._mount_from_command([
            "docker", "run", "--rm",
            "-v", "gmaps-playwright-cache:/opt",
            "-v", "C:\\some\\path:/queries.txt:ro",
            "-v", "C:\\some\\path\\bins\\r0-c0:/out",
            REAL_DIGEST_IMAGE,
        ])
        assert parsed == "C:\\some\\path\\bins\\r0-c0"
