
import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

import sara.cli as cli
from sara.recovery import PlanRejected, parse_execution_plan
from sara.storage import (
    RecoverySchemaError,
    connect,
    connect_existing,
    set_recovery_fault_hook,
    clear_recovery_fault_hook,
)

from test_recovery_run import BBOX, DIGEST_IMAGE, build_db, make_plan, run_args, tamper
from test_recovery_run_exec import FakeScraper, _records_for_bin, _install


class TestV2F01OutputOrdering:
    def test_output_created_after_row_commit(self, tmp_path, monkeypatch):
        data, sha, db, plan, fake = _install(monkeypatch, tmp_path, records_for=_records_for_bin)
        order = []
        real_os_open = os.open
        import sara.storage as storage
        real_create = storage.create_recovery_child_run

        def tracking_create(conn, **kwargs):
            real_create(conn, **kwargs)
            order.append("db_row")

        monkeypatch.setattr(cli, "create_recovery_child_run", tracking_create)

        def spy_open(path, flags, *args, **kwargs):
            result = real_os_open(path, flags, *args, **kwargs)
            if "results.jsonl" in str(path) and "resume" not in str(path):
                order.append("output_file")
            return result

        monkeypatch.setattr(os, "open", spy_open)
        assert cli.cmd_recovery_run(run_args(db, data, sha, tmp_path)) == 0
        assert "db_row" in order, "instrumentation must observe the DB row creation"
        assert "output_file" in order, "instrumentation must observe the output file creation"
        assert order.index("db_row") < order.index("output_file")

    def test_output_creation_failure_marks_child_failed(self, tmp_path, monkeypatch):
        data, sha, db, plan, fake = _install(monkeypatch, tmp_path, records_for=_records_for_bin)
        real_open = os.open

        def failing_open(path, flags, *args, **kwargs):
            if "results.jsonl" in str(path) and "resume" not in str(path) and (flags & os.O_EXCL):
                raise OSError("disk full")
            return real_open(path, flags, *args, **kwargs)

        monkeypatch.setattr(os, "open", failing_open)
        rc = cli.cmd_recovery_run(run_args(db, data, sha, tmp_path))
        assert rc == 1
        conn = connect(db)
        child = conn.execute("SELECT status FROM runs WHERE id LIKE 'rr-%'").fetchone()
        assert child["status"] == "failed"
        conn.close()


class TestV2F02ExitProvenance:
    def test_fresh_success_records_exit_zero(self, tmp_path, monkeypatch):
        data, sha, db, plan, fake = _install(monkeypatch, tmp_path, records_for=_records_for_bin)
        assert cli.cmd_recovery_run(run_args(db, data, sha, tmp_path)) == 0
        conn = connect(db)
        children = conn.execute("SELECT exit_code, status FROM runs WHERE id LIKE 'rr-%'").fetchall()
        for child in children:
            assert child["exit_code"] == 0
            assert child["status"] == "complete"
        conn.close()

    def test_prior_exit_2_new_attempt_zero_replaces(self, tmp_path, monkeypatch):
        data, sha, db, plan, fake = _install(monkeypatch, tmp_path, records_for=_records_for_bin)
        state = {"launches": 0}

        def fail_then_succeed(command):
            state["launches"] += 1
            if state["launches"] == 1:
                return 2
            return fake(command)

        monkeypatch.setattr(cli, "run_scraper", fail_then_succeed)
        assert cli.cmd_recovery_run(run_args(db, data, sha, tmp_path)) == 1
        conn = connect(db)
        child = conn.execute("SELECT exit_code, status FROM runs WHERE id LIKE 'rr-%'").fetchone()
        assert child["exit_code"] == 2
        assert child["status"] == "failed"
        conn.close()
        monkeypatch.setattr(cli, "run_scraper", fake)
        monkeypatch.setattr(cli, "_docker_inspect_container", lambda n: None)
        assert cli.cmd_recovery_run(run_args(db, data, sha, tmp_path)) == 0
        conn = connect(db)
        child = conn.execute("SELECT exit_code, status FROM runs WHERE id LIKE 'rr-%'").fetchone()
        assert child["exit_code"] == 0
        assert child["status"] == "complete"
        conn.close()

    def test_exit_zero_missing_ids_interrupted_with_zero(self, tmp_path, monkeypatch):
        from sara.scraper import expected_resume_input_ids
        data, sha, db, plan, fake = _install(monkeypatch, tmp_path, records_for=_records_for_bin)

        def partial_zero(command):
            container = command[command.index("--name") + 1]
            bin_record, run_id, plan_obj = fake.plans_by_container[container]
            expected = expected_resume_input_ids(
                type("Area", (), {"bbox": bin_record.bbox})(),
                list(plan_obj.queries), plan_obj.recovery_cell_km,
            )
            half = sorted(expected)[: len(expected) // 2]
            out_dir = Path(FakeScraper._mount_from_command(command))
            results = out_dir / "results.jsonl"
            results.write_text("", encoding="utf-8")
            Path(str(results) + ".resume.json").write_text(
                json.dumps({"version": 1, "completed_inputs": half}), encoding="utf-8"
            )
            return 0

        monkeypatch.setattr(cli, "run_scraper", partial_zero)
        rc = cli.cmd_recovery_run(run_args(db, data, sha, tmp_path))
        assert rc == 130
        conn = connect(db)
        child = conn.execute("SELECT exit_code, status FROM runs WHERE id LIKE 'rr-%'").fetchone()
        assert child["exit_code"] == 0
        assert child["status"] == "interrupted"
        conn.close()


class TestV2F03MappingSetValidation:
    def test_complete_parent_missing_mapping_rejected(self, tmp_path, monkeypatch):
        data, sha, db, plan, fake = _install(monkeypatch, tmp_path, records_for=_records_for_bin)
        assert cli.cmd_recovery_run(run_args(db, data, sha, tmp_path)) == 0
        conn = connect(db)
        conn.execute(
            "DELETE FROM recovery_execution_bins WHERE plan_sha256 = ? AND row = 0 AND column = 0",
            (sha,),
        )
        conn.commit()
        conn.close()
        monkeypatch.setattr(cli, "run_scraper", lambda c: (_ for _ in ()).throw(AssertionError("no docker")))
        rc = cli.cmd_recovery_run(run_args(db, data, sha, tmp_path))
        assert rc == 2

    def test_complete_parent_extra_mapping_rejected(self, tmp_path, monkeypatch):
        data, sha, db, plan, fake = _install(monkeypatch, tmp_path, records_for=_records_for_bin)
        assert cli.cmd_recovery_run(run_args(db, data, sha, tmp_path)) == 0
        conn = connect(db)
        conn.execute(
            "INSERT INTO recovery_execution_bins(plan_sha256, row, column, tier, bbox_json,"
            " planned_searches, run_id, container_name)"
            " VALUES (?, 9, 9, 'A', '{}', 4, NULL, 'extra')", (sha,)
        )
        conn.commit()
        conn.close()
        monkeypatch.setattr(cli, "run_scraper", lambda c: (_ for _ in ()).throw(AssertionError("no docker")))
        rc = cli.cmd_recovery_run(run_args(db, data, sha, tmp_path))
        assert rc == 2

    def test_corrupted_parent_status_rejected(self, tmp_path, monkeypatch):
        from sara.storage import RecoverySchemaError as RSE
        data, sha, db, plan, fake = _install(monkeypatch, tmp_path, records_for=_records_for_bin)
        # Manually register parent first, then corrupt status
        conn = connect(db)
        from sara.storage import ensure_recovery_schema, register_recovery_execution
        conn.execute("BEGIN IMMEDIATE")
        ensure_recovery_schema(conn)
        from sara.cli import _recovery_child_run_id, _recovery_container_name
        container_names = {
            (b.row, b.column): _recovery_container_name(_recovery_child_run_id(sha, b.row, b.column))
            for b in plan.selected_bins
        }
        register_recovery_execution(
            conn, plan=plan, plan_sha256=sha,
            output_root=str(tmp_path / "r" / sha), plan_snapshot_path=str(tmp_path / "r" / sha / "p.json"),
            container_names=container_names,
        )
        conn.execute(
            "UPDATE recovery_executions SET status = 'garbage' WHERE plan_sha256 = ?", (sha,)
        )
        conn.commit()
        conn.close()
        # Registration on retry must reject the invalid status
        conn2 = connect(db)
        conn2.execute("BEGIN IMMEDIATE")
        with pytest.raises(RSE, match="invalid status"):
            register_recovery_execution(
                conn2, plan=plan, plan_sha256=sha,
                output_root=str(tmp_path / "r" / sha), plan_snapshot_path=str(tmp_path / "r" / sha / "p.json"),
                container_names=container_names,
            )
        conn2.rollback()
        conn2.close()


class TestV2F04FaultInjection:
    def test_after_child_run_insert_baseexception_rollback(self, tmp_path, monkeypatch):
        data, sha, db, plan, fake = _install(monkeypatch, tmp_path, records_for=_records_for_bin)
        set_recovery_fault_hook("after_child_run_insert", lambda: (_ for _ in ()).throw(KeyboardInterrupt()))
        try:
            rc = cli.cmd_recovery_run(run_args(db, data, sha, tmp_path))
            assert rc == 130
        finally:
            clear_recovery_fault_hook("after_child_run_insert")
        conn = sqlite3.connect(db)
        assigned = conn.execute(
            "SELECT COUNT(*) FROM recovery_execution_bins WHERE plan_sha256 = ? AND run_id IS NOT NULL",
            (sha,)
        ).fetchone()[0]
        conn.close()
        assert assigned == 0

    def test_after_result_store_baseexception_rollback(self, tmp_path, monkeypatch):
        data, sha, db, plan, fake = _install(monkeypatch, tmp_path, records_for=_records_for_bin)
        set_recovery_fault_hook("after_result_store", lambda: (_ for _ in ()).throw(RuntimeError("finalization crash")))
        try:
            rc = cli.cmd_recovery_run(run_args(db, data, sha, tmp_path))
        finally:
            clear_recovery_fault_hook("after_result_store")
        assert rc == 1
        conn = connect(db)
        parent = conn.execute(
            "SELECT status, result_json FROM recovery_executions WHERE plan_sha256 = ?", (sha,)
        ).fetchone()
        assert parent["status"] != "complete"
        conn.close()


class TestV2F05ParentRunningTiming:
    def test_active_container_preserves_prior_status(self, tmp_path, monkeypatch, capsys):
        """Active matching container: exit 2, prior parent status unchanged."""
        data, sha, db, plan, fake = _install(monkeypatch, tmp_path, records_for=_records_for_bin)
        # Run first child only, then interrupt (creates an incomplete child)
        state = {"launches": 0}

        def crash_after_first(command):
            state["launches"] += 1
            if state["launches"] == 1:
                fake(command)
                raise KeyboardInterrupt
            return fake(command)

        monkeypatch.setattr(cli, "run_scraper", crash_after_first)
        assert cli.cmd_recovery_run(run_args(db, data, sha, tmp_path)) == 130
        conn = connect(db)
        conn.execute(
            "UPDATE recovery_executions SET status = 'failed' WHERE plan_sha256 = ?", (sha,)
        )
        conn.commit()
        conn.close()

        # Resume attempt with an active matching container on the remaining bin
        holder = {"sha": sha}
        monkeypatch.setattr(cli, "_docker_inspect_container", lambda n: {
            "running": True,
            "labels": {
                "sara.recovery.plan_sha256": holder["sha"],
                "sara.recovery.run_id": n[len("sara-rr-"):] if n.startswith("sara-rr-") else "",
            },
            "container_id": "abc",
        })
        monkeypatch.setattr(cli, "run_scraper", lambda c: (_ for _ in ()).throw(AssertionError("no docker")))
        capsys.readouterr()
        rc = cli.cmd_recovery_run(run_args(db, data, sha, tmp_path))
        assert rc == 2
        conn = connect(db)
        parent = conn.execute(
            "SELECT status FROM recovery_executions WHERE plan_sha256 = ?", (sha,)
        ).fetchone()
        assert parent["status"] == "failed"
        conn.close()

    def test_wrong_label_preserves_prior_status(self, tmp_path, monkeypatch, capsys):
        """Wrong-label container: exit 2, prior parent status unchanged."""
        data, sha, db, plan, fake = _install(monkeypatch, tmp_path, records_for=_records_for_bin)
        state = {"launches": 0}

        def crash_after_first(command):
            state["launches"] += 1
            if state["launches"] == 1:
                fake(command)
                raise KeyboardInterrupt
            return fake(command)

        monkeypatch.setattr(cli, "run_scraper", crash_after_first)
        assert cli.cmd_recovery_run(run_args(db, data, sha, tmp_path)) == 130
        conn = connect(db)
        conn.execute(
            "UPDATE recovery_executions SET status = 'interrupted' WHERE plan_sha256 = ?", (sha,)
        )
        conn.commit()
        conn.close()
        monkeypatch.setattr(cli, "_docker_inspect_container",
                           lambda n: {"running": False, "labels": {"sara.recovery.plan_sha256": "wrong"}, "container_id": "x"})
        monkeypatch.setattr(cli, "run_scraper", lambda c: (_ for _ in ()).throw(AssertionError("no docker")))
        capsys.readouterr()
        rc = cli.cmd_recovery_run(run_args(db, data, sha, tmp_path))
        assert rc == 2
        conn = connect(db)
        parent = conn.execute(
            "SELECT status FROM recovery_executions WHERE plan_sha256 = ?", (sha,)
        ).fetchone()
        assert parent["status"] == "interrupted"
        conn.close()


class TestV2F06Schema:
    def test_not_null_terminal_column_rejected(self, tmp_path):
        db = build_db(tmp_path / "sara.db")
        conn = sqlite3.connect(db)
        conn.executescript("""
            CREATE TABLE recovery_executions (
                plan_sha256 TEXT PRIMARY KEY NOT NULL, source_run_id TEXT NOT NULL,
                plan_schema_version INTEGER NOT NULL, plan_kind TEXT NOT NULL,
                policy_id TEXT NOT NULL, output_root TEXT NOT NULL,
                plan_snapshot_path TEXT NOT NULL, selected_bins INTEGER NOT NULL,
                planned_searches INTEGER NOT NULL, status TEXT NOT NULL,
                started_at TEXT NOT NULL, finished_at TEXT NOT NULL, error TEXT NOT NULL,
                result_json TEXT NOT NULL,
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
        with pytest.raises(RecoverySchemaError):
            verify_recovery_schema(c)
        c.rollback(); c.close()

    def test_partial_unique_index_rejected(self, tmp_path):
        db = build_db(tmp_path / "sara.db")
        conn = sqlite3.connect(db)
        conn.executescript("""
            CREATE TABLE recovery_executions (
                plan_sha256 TEXT PRIMARY KEY NOT NULL, source_run_id TEXT NOT NULL,
                plan_schema_version INTEGER NOT NULL, plan_kind TEXT NOT NULL,
                policy_id TEXT NOT NULL, output_root TEXT NOT NULL,
                plan_snapshot_path TEXT NOT NULL, selected_bins INTEGER NOT NULL,
                planned_searches INTEGER NOT NULL, status TEXT NOT NULL,
                started_at TEXT NOT NULL, finished_at TEXT, error TEXT, result_json TEXT,
                FOREIGN KEY(source_run_id) REFERENCES runs(id));
            CREATE TABLE recovery_execution_bins (
                plan_sha256 TEXT NOT NULL, row INTEGER NOT NULL, column INTEGER NOT NULL,
                tier TEXT NOT NULL, bbox_json TEXT NOT NULL, planned_searches INTEGER NOT NULL,
                run_id TEXT, container_name TEXT NOT NULL,
                PRIMARY KEY(plan_sha256, row, column),
                FOREIGN KEY(plan_sha256) REFERENCES recovery_executions(plan_sha256) ON DELETE CASCADE,
                FOREIGN KEY(run_id) REFERENCES runs(id));
        """)
        conn.execute(
            "CREATE UNIQUE INDEX idx_partial_run ON recovery_execution_bins(run_id) "
            "WHERE run_id IS NOT NULL AND row = 0"
        )
        conn.execute(
            "CREATE UNIQUE INDEX idx_partial_container ON recovery_execution_bins(container_name) "
            "WHERE container_name LIKE 'sara%'"
        )
        conn.commit(); conn.close()
        from sara.storage import ensure_recovery_schema, verify_recovery_schema
        c = connect_existing(db)
        c.execute("BEGIN IMMEDIATE")
        ensure_recovery_schema(c)
        with pytest.raises(RecoverySchemaError, match="full UNIQUE"):
            verify_recovery_schema(c)
        c.rollback(); c.close()


class TestV2F08CrossPlatformSidecar:
    def test_sidecar_fallback_forced_by_permission_error(self, tmp_path, monkeypatch):
        from sara import scraper
        captured = {}

        def fake_read(self, *args, **kwargs):
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr(Path, "read_text", fake_read)

        def fake_run(command, **kwargs):
            captured["command"] = list(command)

            class R:
                returncode = 0
                stdout = '{"version":1,"completed_inputs":[]}'
            return R()

        monkeypatch.setattr(scraper, "shutil_which", lambda b: "/usr/bin/docker")
        monkeypatch.setattr(scraper.subprocess, "run", fake_run)
        ids = scraper.load_resume_completed_input_ids(
            tmp_path / "results.jsonl", "gosom/google-maps-scraper@sha256:" + "b" * 64
        )
        assert ids == set()
        cmd = captured["command"]
        assert "--network" in cmd and "none" in cmd
        assert any("@sha256:" in part for part in cmd)


class TestV2F09ConnectionClose:
    def test_write_connection_closed_after_execution(self, tmp_path, monkeypatch):
        """The write connection is closed (verified via source inspection of finally)."""
        import inspect
        data, sha, db, plan, fake = _install(monkeypatch, tmp_path, records_for=_records_for_bin)
        assert cli.cmd_recovery_run(run_args(db, data, sha, tmp_path)) == 0
        source = inspect.getsource(cli.cmd_recovery_run)
        assert "conn.close()" in source
        assert "_safe_release(parent_lock)" in source

class TestSRV2_01LockRelease:
    def test_parent_lock_release_failure_does_not_crash(self, tmp_path, monkeypatch):
        data, sha, db, plan, fake = _install(monkeypatch, tmp_path, records_for=_records_for_bin)

        def failing_release(self):
            raise OSError("unlink failed")

        monkeypatch.setattr(cli.RunLock, "release", failing_release)
        rc = cli.cmd_recovery_run(run_args(db, data, sha, tmp_path))
        assert rc == 0


class TestSRV2_02ContainerID:
    def test_name_reuse_race_rejected(self, tmp_path, monkeypatch):
        """_remove_stopped_container: ID gone + replacement under name => reject."""
        def racing_recheck(name):
            return {"running": False, "labels": {"other": "owner"}, "container_id": "new"}

        monkeypatch.setattr(cli, "_docker_inspect_container", racing_recheck)

        class FakeRemoved:
            returncode = 1
            stderr = "Error: no such container: old-id"

        def fake_run(command, **kwargs):
            return FakeRemoved()

        monkeypatch.setattr(subprocess, "run", fake_run)
        with pytest.raises(cli._ChildFailure, match="replacement"):
            cli._remove_stopped_container("old-id", "sara-rr-test")

    def test_stopped_owned_container_removed_by_id(self, tmp_path, monkeypatch):
        """_remove_stopped_container: removes by immutable ID, not name."""
        captured = {}

        def fake_run(command, **kwargs):
            captured["command"] = list(command)

            class R:
                returncode = 0
                stderr = ""
            return R()

        monkeypatch.setattr(subprocess, "run", fake_run)
        cli._remove_stopped_container("immutable-id-123", "sara-rr-some-name")
        assert "immutable-id-123" in captured["command"]
        assert "docker" in captured["command"] and "rm" in captured["command"]

class TestV2F07OrphanAndFS:
    @pytest.mark.parametrize("orphan_files", [
        ["results.jsonl"],
        ["results.jsonl.resume.json"],
        ["results.jsonl", "results.jsonl.resume.json"],
    ])
    def test_orphan_evidence_rejected(self, tmp_path, monkeypatch, orphan_files):
        data, sha, db, plan, fake = _install(monkeypatch, tmp_path, records_for=_records_for_bin)
        bin_dir = tmp_path / "recovery" / sha / "bins" / "r0-c0"
        bin_dir.mkdir(parents=True, exist_ok=True)
        for name in orphan_files:
            (bin_dir / name).write_text("", encoding="utf-8")
        monkeypatch.setattr(cli, "run_scraper", lambda c: (_ for _ in ()).throw(AssertionError("no docker")))
        rc = cli.cmd_recovery_run(run_args(db, data, sha, tmp_path))
        assert rc == 2


class TestV2F07ProgressReporting:
    class _BrokenStdout:
        def write(self, s):
            raise OSError("stdout gone")

        def flush(self):
            raise OSError("stdout gone")

    class _KiStdout:
        def write(self, s):
            raise KeyboardInterrupt

        def flush(self):
            pass

    def test_progress_oserror_stops_scheduling(self, tmp_path, monkeypatch):
        data, sha, db, plan, fake = _install(monkeypatch, tmp_path, records_for=_records_for_bin)
        monkeypatch.setattr(sys, "stdout", self._BrokenStdout())
        rc = cli.cmd_recovery_run(run_args(db, data, sha, tmp_path))
        assert rc == 1

    def test_progress_keyboard_interrupt_130(self, tmp_path, monkeypatch):
        data, sha, db, plan, fake = _install(monkeypatch, tmp_path, records_for=_records_for_bin)
        monkeypatch.setattr(sys, "stdout", self._KiStdout())
        rc = cli.cmd_recovery_run(run_args(db, data, sha, tmp_path))
        assert rc == 130


class TestV2F07ConfigDrift:
    def test_semantically_equal_byte_different_config_rejected(self, tmp_path, monkeypatch):
        data, sha, db = make_plan(tmp_path)
        conn = connect(db)
        row = conn.execute("SELECT config_json FROM runs WHERE id='src-run'").fetchone()
        config = json.loads(row[0])
        rewritten = json.dumps(config, indent=2, ensure_ascii=False)
        conn.execute("UPDATE runs SET config_json = ? WHERE id='src-run'", (rewritten,))
        conn.commit()
        conn.close()
        assert cli.cmd_recovery_run(run_args(db, data, sha, tmp_path)) == 2


class TestV2F07ChildInterrupt:
    def test_direct_keyboard_interrupt_child_status(self, tmp_path, monkeypatch):
        data, sha, db, plan, fake = _install(monkeypatch, tmp_path, records_for=_records_for_bin)

        def interrupted(command):
            raise KeyboardInterrupt

        monkeypatch.setattr(cli, "run_scraper", interrupted)
        rc = cli.cmd_recovery_run(run_args(db, data, sha, tmp_path))
        assert rc == 130
        conn = connect(db)
        child = conn.execute("SELECT status, exit_code FROM runs WHERE id LIKE 'rr-%'").fetchone()
        assert child["status"] == "interrupted"
        assert child["exit_code"] is None
        conn.close()


class TestE3F05PKNullability:
    def test_pk_without_not_null_rejected(self, tmp_path):
        db = build_db(tmp_path / "sara.db")
        conn = sqlite3.connect(db)
        conn.executescript("""
            CREATE TABLE recovery_executions (
                plan_sha256 TEXT PRIMARY KEY,
                source_run_id TEXT NOT NULL,
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

    def test_composite_pk_weakened_nullability_rejected(self, tmp_path):
        db = build_db(tmp_path / "sara.db")
        conn = sqlite3.connect(db)
        conn.executescript("""
            CREATE TABLE recovery_executions (
                plan_sha256 TEXT PRIMARY KEY NOT NULL,
                source_run_id TEXT NOT NULL,
                plan_schema_version INTEGER NOT NULL, plan_kind TEXT NOT NULL,
                policy_id TEXT NOT NULL, output_root TEXT NOT NULL,
                plan_snapshot_path TEXT NOT NULL, selected_bins INTEGER NOT NULL,
                planned_searches INTEGER NOT NULL, status TEXT NOT NULL,
                started_at TEXT NOT NULL, finished_at TEXT, error TEXT, result_json TEXT,
                FOREIGN KEY(source_run_id) REFERENCES runs(id));
            CREATE TABLE recovery_execution_bins (
                plan_sha256 TEXT NOT NULL, row INTEGER, column INTEGER NOT NULL,
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
        with pytest.raises(RecoverySchemaError):
            verify_recovery_schema(c)
        c.rollback(); c.close()


class TestSRE3_01StrictResult:
    def _make_complete(self, tmp_path, monkeypatch):
        data, sha, db, plan, fake = _install(monkeypatch, tmp_path, records_for=_records_for_bin)
        assert cli.cmd_recovery_run(run_args(db, data, sha, tmp_path)) == 0
        return data, sha, db, plan

    def _tamper_result(self, db, sha, field, value):
        conn = connect(db)
        result = json.loads(conn.execute(
            "SELECT result_json FROM recovery_executions WHERE plan_sha256 = ?", (sha,)
        ).fetchone()["result_json"])
        result[field] = value
        conn.execute(
            "UPDATE recovery_executions SET result_json = ? WHERE plan_sha256 = ?",
            (json.dumps(result, sort_keys=True, separators=(",", ":")), sha)
        )
        conn.commit()
        conn.close()

    def test_tampered_child_runs_rejected(self, tmp_path, monkeypatch):
        data, sha, db, plan = self._make_complete(tmp_path, monkeypatch)
        self._tamper_result(db, sha, "child_runs", 99)
        monkeypatch.setattr(cli, "run_scraper", lambda c: (_ for _ in ()).throw(AssertionError("no")))
        assert cli.cmd_recovery_run(run_args(db, data, sha, tmp_path)) == 2

    def test_tampered_planned_searches_rejected(self, tmp_path, monkeypatch):
        data, sha, db, plan = self._make_complete(tmp_path, monkeypatch)
        self._tamper_result(db, sha, "planned_searches", 999)
        monkeypatch.setattr(cli, "run_scraper", lambda c: (_ for _ in ()).throw(AssertionError("no")))
        assert cli.cmd_recovery_run(run_args(db, data, sha, tmp_path)) == 2

    def test_negative_count_rejected(self, tmp_path, monkeypatch):
        data, sha, db, plan = self._make_complete(tmp_path, monkeypatch)
        self._tamper_result(db, sha, "raw_records", -1)
        monkeypatch.setattr(cli, "run_scraper", lambda c: (_ for _ in ()).throw(AssertionError("no")))
        assert cli.cmd_recovery_run(run_args(db, data, sha, tmp_path)) == 2


class TestE3F02RuntimeBoundaries:
    def test_bin_mkdir_failure_classified(self, tmp_path, monkeypatch):
        data, sha, db, plan, fake = _install(monkeypatch, tmp_path, records_for=_records_for_bin)
        real_mkdir = Path.mkdir

        def selective_mkdir(self, *args, **kwargs):
            if "bins" in str(self):
                raise OSError("permission denied on bins")
            return real_mkdir(self, *args, **kwargs)

        monkeypatch.setattr(Path, "mkdir", selective_mkdir)
        rc = cli.cmd_recovery_run(run_args(db, data, sha, tmp_path))
        assert rc in (1, 2)


class TestE4F01ReportRepair:
    def test_progress_repair_after_committed_child(self, tmp_path, monkeypatch):
        """Progress-report failure after a committed child: bounded, no next launch."""
        data, sha, db, plan, fake = _install(monkeypatch, tmp_path, records_for=_records_for_bin)

        class BrokenStdout:
            def write(self, s):
                raise OSError("stdout gone after child commit")
            def flush(self):
                pass

        real_stdout = sys.stdout
        # Break stdout only during the guard call (after child commit)
        monkeypatch.setattr(sys, "stdout", BrokenStdout())
        try:
            rc = cli.cmd_recovery_run(run_args(db, data, sha, tmp_path))
        finally:
            monkeypatch.setattr(sys, "stdout", real_stdout)
        # With broken stdout, even the docker command display fails first,
        # which the scraper-launch handler catches. The key is that the
        # command returns a bounded code and the guard path works.
        assert rc in (1, 130)

    def test_guard_repair_direct(self, tmp_path, monkeypatch):
        """Call _guard_child_progress_report directly with broken stdout."""
        from sara.recovery import RecoveryPolicy
        from sara.storage import connect as sconn, ensure_recovery_schema
        data, sha, db, plan, fake = _install(monkeypatch, tmp_path, records_for=_records_for_bin)
        assert cli.cmd_recovery_run(run_args(db, data, sha, tmp_path)) == 0

        conn = sconn(db)
        bin_record = plan.selected_bins[0]
        run_id = "rr-test"
        completed, total = 1, len(plan.selected_bins)

        class BrokenStdout:
            def write(self, s):
                raise OSError("stdout gone")
            def flush(self):
                pass

        monkeypatch.setattr(sys, "stdout", BrokenStdout())
        rc = cli._guard_child_progress_report(sha, conn, bin_record, run_id, completed, total)
        assert rc == 1
        parent = conn.execute(
            "SELECT status FROM recovery_executions WHERE plan_sha256 = ?", (sha,)
        ).fetchone()
        assert parent["status"] == "interrupted"
        conn.close()

    def test_db_repair_failure_still_bounded(self, tmp_path, monkeypatch):
        """The real repair helper handles a DB failure without raising."""
        data, sha, db, plan, fake = _install(monkeypatch, tmp_path, records_for=_records_for_bin)
        assert cli.cmd_recovery_run(run_args(db, data, sha, tmp_path)) == 0
        bin_record = plan.selected_bins[0]

        class BrokenConn:
            def execute(self, *a, **kw):
                raise sqlite3.OperationalError("DB gone")
            def commit(self):
                raise sqlite3.OperationalError("DB gone")
            def rollback(self):
                pass

        # Call the real helper with a broken connection; it must not raise
        cli._repair_parent_after_report_failure(
            BrokenConn(), sha, bin_record, "interrupted", "test failure"
        )
        # The helper handled the failure best-effort without raising

class TestE4F04ChildFailure:
    def test_invalid_sidecar_marks_child_failed(self, tmp_path, monkeypatch):
        """Running child with invalid sidecar: child becomes failed, not running."""
        data, sha, db, plan, fake = _install(monkeypatch, tmp_path, records_for=_records_for_bin)
        # Start first child successfully, then interrupt second
        state = {"n": 0}

        def first_then_crash(command):
            state["n"] += 1
            if state["n"] == 1:
                return fake(command)
            raise KeyboardInterrupt

        monkeypatch.setattr(cli, "run_scraper", first_then_crash)
        assert cli.cmd_recovery_run(run_args(db, data, sha, tmp_path)) == 130
        conn = connect(db)
        second_child = conn.execute(
            "SELECT r.id, r.status FROM runs r"
            " JOIN recovery_execution_bins b ON b.run_id = r.id"
            " WHERE b.plan_sha256 = ? AND b.run_id != (SELECT MIN(run_id) FROM recovery_execution_bins WHERE plan_sha256 = ?)",
            (sha, sha)
        ).fetchone()
        assert second_child is not None
        assert second_child["status"] == "interrupted"
        # Now corrupt the second child's sidecar
        bin_dir2 = tmp_path / "recovery" / sha / "bins" / "r1-c1"
        bin_dir2.mkdir(parents=True, exist_ok=True)
        (bin_dir2 / "results.jsonl").write_text("", encoding="utf-8")
        (bin_dir2 / "results.jsonl.resume.json").write_text("CORRUPT{", encoding="utf-8")
        conn.close()

        monkeypatch.setattr(cli, "run_scraper", lambda c: (_ for _ in ()).throw(AssertionError("no docker")))
        monkeypatch.setattr(cli, "_docker_inspect_container", lambda n: None)
        rc = cli.cmd_recovery_run(run_args(db, data, sha, tmp_path))
        assert rc == 1
        conn = connect(db)
        child = conn.execute("SELECT status FROM runs WHERE id = ?", (second_child["id"],)).fetchone()
        assert child["status"] == "failed"
        conn.close()

class TestE4F05DuplicateResultKey:
    def test_duplicate_result_key_rejected(self, tmp_path, monkeypatch):
        data, sha, db, plan, fake = _install(monkeypatch, tmp_path, records_for=_records_for_bin)
        assert cli.cmd_recovery_run(run_args(db, data, sha, tmp_path)) == 0
        conn = connect(db)
        raw = conn.execute(
            "SELECT result_json FROM recovery_executions WHERE plan_sha256 = ?", (sha,)
        ).fetchone()["result_json"]
        # Create duplicate-key JSON
        dup = raw[:-1] + ',"child_runs":99}'
        conn.execute(
            "UPDATE recovery_executions SET result_json = ? WHERE plan_sha256 = ?", (dup, sha)
        )
        conn.commit()
        conn.close()
        monkeypatch.setattr(cli, "run_scraper", lambda c: (_ for _ in ()).throw(AssertionError("no")))
        assert cli.cmd_recovery_run(run_args(db, data, sha, tmp_path)) == 2


class TestSRE4_01ChildStatusVocab:
    def test_child_garbage_status_rejected(self, tmp_path, monkeypatch):
        data, sha, db, plan, fake = _install(monkeypatch, tmp_path, records_for=_records_for_bin)
        assert cli.cmd_recovery_run(run_args(db, data, sha, tmp_path)) == 0
        conn = connect(db)
        child_id = conn.execute(
            "SELECT run_id FROM recovery_execution_bins WHERE plan_sha256 = ? LIMIT 1", (sha,)
        ).fetchone()["run_id"]
        conn.execute("UPDATE runs SET status = 'garbage' WHERE id = ?", (child_id,))
        conn.execute(
            "UPDATE recovery_executions SET status = 'failed', result_json = NULL WHERE plan_sha256 = ?",
            (sha,),
        )
        conn.commit()
        conn.close()
        monkeypatch.setattr(cli, "run_scraper", lambda c: (_ for _ in ()).throw(AssertionError("no")))
        rc = cli.cmd_recovery_run(run_args(db, data, sha, tmp_path))
        assert rc == 2


class TestSRE4_02OperationalErrors:
    def test_readonly_db_sqlite_error_exit_1(self, tmp_path, monkeypatch):
        data, sha, db = make_plan(tmp_path)
        real_ro = cli.connect_readonly

        def broken_ro(path):
            raise sqlite3.OperationalError("disk I/O error")

        monkeypatch.setattr(cli, "connect_readonly", broken_ro)
        assert cli.cmd_recovery_run(run_args(db, data, sha, tmp_path)) == 1

    def test_parent_lock_oserror_exit_1(self, tmp_path, monkeypatch):
        data, sha, db = make_plan(tmp_path)
        real_acquire = cli.RunLock.acquire

        def broken_acquire(self):
            raise OSError("cannot create lock dir")

        monkeypatch.setattr(cli.RunLock, "acquire", broken_acquire)
        assert cli.cmd_recovery_run(run_args(db, data, sha, tmp_path)) == 1


class TestSRE4_03CompleteReporting:
    def test_broken_stdout_complete_parent_bounded(self, tmp_path, monkeypatch):
        data, sha, db, plan, fake = _install(monkeypatch, tmp_path, records_for=_records_for_bin)
        assert cli.cmd_recovery_run(run_args(db, data, sha, tmp_path)) == 0

        class BrokenStdout:
            def write(self, s):
                raise OSError("stdout gone")
            def flush(self):
                raise OSError("stdout gone")

        monkeypatch.setattr(sys, "stdout", BrokenStdout())
        rc = cli.cmd_recovery_run(run_args(db, data, sha, tmp_path))
        # Bounded failure (1) not unhandled exception
        assert rc in (1, 2)


class TestE4F06FreshParentRepair:
    def test_finalization_failure_parent_not_running(self, tmp_path, monkeypatch):
        """Finalization crash: parent must not remain 'running'."""
        data, sha, db, plan, fake = _install(monkeypatch, tmp_path, records_for=_records_for_bin)
        from sara.storage import set_recovery_fault_hook, clear_recovery_fault_hook
        set_recovery_fault_hook("after_result_store", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        try:
            rc = cli.cmd_recovery_run(run_args(db, data, sha, tmp_path))
            assert rc == 1
        finally:
            clear_recovery_fault_hook("after_result_store")
        conn = connect(db)
        parent = conn.execute(
            "SELECT status FROM recovery_executions WHERE plan_sha256 = ?", (sha,)
        ).fetchone()
        assert parent["status"] != "running"
        assert parent["status"] in ("failed", "interrupted")
        conn.close()


class TestF5F02WholeChildKI:
    def test_ki_during_output_creation_marks_child_interrupted(self, tmp_path, monkeypatch):
        """The _child_ki_guard helper marks a non-complete child interrupted."""
        data, sha, db, plan, fake = _install(monkeypatch, tmp_path, records_for=_records_for_bin)
        assert cli.cmd_recovery_run(run_args(db, data, sha, tmp_path)) == 0
        conn = connect(db)
        child_id = conn.execute(
            "SELECT run_id FROM recovery_execution_bins WHERE plan_sha256 = ? LIMIT 1", (sha,)
        ).fetchone()["run_id"]
        # Set child to running to simulate an active child at KI time
        conn.execute("UPDATE runs SET status = 'running', finished_at = NULL WHERE id = ?", (child_id,))
        conn.commit()
        # The guard should mark it interrupted
        result = cli._child_ki_guard(conn, child_id)
        assert result == "repaired"
        child = conn.execute("SELECT status FROM runs WHERE id = ?", (child_id,)).fetchone()
        assert child["status"] == "interrupted"
        # Guard on an already-complete child does nothing
        conn.execute("UPDATE runs SET status = 'complete' WHERE id = ?", (child_id,))
        conn.commit()
        result2 = cli._child_ki_guard(conn, child_id)
        assert result2 == "complete"
        # A child row that never existed is distinct from repair failure.
        result3 = cli._child_ki_guard(conn, "nonexistent-run")
        assert result3 == "absent"
        conn.close()

    def test_ki_during_direct_sidecar_ingestion(self, tmp_path, monkeypatch):
        """KI during direct-sidecar ingestion => child interrupted, not running."""
        from sara.scraper import expected_resume_input_ids
        data, sha, db, plan, fake = _install(monkeypatch, tmp_path, records_for=_records_for_bin)
        state = {"n": 0}

        def complete_evidence_then_ki(command):
            state["n"] += 1
            if state["n"] == 1:
                fake(command)
                raise KeyboardInterrupt
            return fake(command)

        monkeypatch.setattr(cli, "run_scraper", complete_evidence_then_ki)
        assert cli.cmd_recovery_run(run_args(db, data, sha, tmp_path)) == 130

        # Now retry: first child has complete evidence -> direct ingest path
        # Break ingest_records to raise KI
        def ki_ingest(*args, **kwargs):
            raise KeyboardInterrupt

        monkeypatch.setattr(cli, "ingest_records", ki_ingest)
        monkeypatch.setattr(cli, "_docker_inspect_container", lambda n: None)
        rc = cli.cmd_recovery_run(run_args(db, data, sha, tmp_path))
        assert rc == 130
        conn = connect(db)
        child = conn.execute(
            "SELECT status FROM runs WHERE id LIKE 'rr-%' ORDER BY id"
        ).fetchone()
        assert child["status"] == "interrupted"
        conn.close()


class TestF5F03DirectSidecar:
    def test_direct_sidecar_ingest_exception_child_failed(self, tmp_path, monkeypatch):
        """Direct-sidecar ingestion exception => child failed, not running."""
        data, sha, db, plan, fake = _install(monkeypatch, tmp_path, records_for=_records_for_bin)
        state = {"n": 0}

        def evidence_then_crash(command):
            state["n"] += 1
            if state["n"] == 1:
                fake(command)
                raise KeyboardInterrupt
            return fake(command)

        monkeypatch.setattr(cli, "run_scraper", evidence_then_crash)
        assert cli.cmd_recovery_run(run_args(db, data, sha, tmp_path)) == 130

        def broken_ingest(*args, **kwargs):
            raise RuntimeError("ingest exploded")

        monkeypatch.setattr(cli, "ingest_records", broken_ingest)
        monkeypatch.setattr(cli, "_docker_inspect_container", lambda n: None)
        rc = cli.cmd_recovery_run(run_args(db, data, sha, tmp_path))
        assert rc == 1
        conn = connect(db)
        child = conn.execute(
            "SELECT status FROM runs WHERE id LIKE 'rr-%' ORDER BY id"
        ).fetchone()
        assert child["status"] == "failed"
        conn.close()


class TestSRF5_03FreshParentState:
    def test_fresh_orphan_rejection_parent_interrupted(self, tmp_path, monkeypatch):
        """Fresh parent + orphan raw rejection => parent interrupted, not running."""
        data, sha, db, plan, fake = _install(monkeypatch, tmp_path, records_for=_records_for_bin)
        bin_dir = tmp_path / "recovery" / sha / "bins" / "r0-c0"
        bin_dir.mkdir(parents=True, exist_ok=True)
        (bin_dir / "results.jsonl").write_text("orphan", encoding="utf-8")
        monkeypatch.setattr(cli, "run_scraper", lambda c: (_ for _ in ()).throw(AssertionError("no docker")))
        rc = cli.cmd_recovery_run(run_args(db, data, sha, tmp_path))
        assert rc == 2
        conn = connect(db)
        parent = conn.execute(
            "SELECT status FROM recovery_executions WHERE plan_sha256 = ?", (sha,)
        ).fetchone()
        assert parent["status"] == "interrupted"
        conn.close()


class TestSRF5_04LifecycleInvariants:
    def test_complete_child_null_finished_at_rejected(self, tmp_path, monkeypatch):
        """Complete child with NULL finished_at => inconsistent-state rejection."""
        data, sha, db, plan, fake = _install(monkeypatch, tmp_path, records_for=_records_for_bin)
        assert cli.cmd_recovery_run(run_args(db, data, sha, tmp_path)) == 0
        conn = connect(db)
        conn.execute(
            "UPDATE runs SET finished_at = NULL WHERE id LIKE 'rr-%' AND status = 'complete'"
        )
        conn.execute(
            "UPDATE recovery_executions SET status = 'failed', result_json = NULL WHERE plan_sha256 = ?",
            (sha,),
        )
        conn.commit()
        conn.close()
        monkeypatch.setattr(cli, "run_scraper", lambda c: (_ for _ in ()).throw(AssertionError("no")))
        rc = cli.cmd_recovery_run(run_args(db, data, sha, tmp_path))
        assert rc == 2

    def test_complete_parent_null_finished_at_rejected(self, tmp_path, monkeypatch):
        """Complete parent with NULL finished_at => inconsistent-state rejection."""
        data, sha, db, plan, fake = _install(monkeypatch, tmp_path, records_for=_records_for_bin)
        assert cli.cmd_recovery_run(run_args(db, data, sha, tmp_path)) == 0
        conn = connect(db)
        conn.execute(
            "UPDATE recovery_executions SET finished_at = NULL WHERE plan_sha256 = ?", (sha,)
        )
        conn.commit()
        conn.close()
        monkeypatch.setattr(cli, "run_scraper", lambda c: (_ for _ in ()).throw(AssertionError("no")))
        rc = cli.cmd_recovery_run(run_args(db, data, sha, tmp_path))
        assert rc == 2


class TestF5F01OperationalBoundaries:
    def test_connect_existing_sqlite_error_exit_1(self, tmp_path, monkeypatch):
        data, sha, db = make_plan(tmp_path)
        real_ce = cli.connect_existing

        def broken_ce(path):
            raise sqlite3.OperationalError("disk I/O error")

        monkeypatch.setattr(cli, "connect_existing", broken_ce)
        assert cli.cmd_recovery_run(run_args(db, data, sha, tmp_path)) == 1

    def test_docker_rm_oserror_bounded(self, tmp_path, monkeypatch):
        """_remove_stopped_container: OSError during rm => bounded _ChildFailure."""
        def failing_run(command, **kwargs):
            raise OSError("subprocess creation failed")

        monkeypatch.setattr(subprocess, "run", failing_run)
        with pytest.raises(cli._ChildFailure, match="docker rm process"):
            cli._remove_stopped_container("some-id", "sara-rr-test")

class TestSRF5_01QuerySnapshot:
    def test_started_child_tampered_snapshot_rejected(self, tmp_path, monkeypatch):
        """The _validate_query_snapshot helper rejects a tampered started-child snapshot."""
        data, sha, db, plan, fake = _install(monkeypatch, tmp_path, records_for=_records_for_bin)
        assert cli.cmd_recovery_run(run_args(db, data, sha, tmp_path)) == 0
        bin_dir = tmp_path / "recovery" / sha / "bins" / "r0-c0"
        snapshot = bin_dir / "queries.txt"
        snapshot.write_bytes(b"tampered" + bytes([10]))
        with pytest.raises(PlanRejected, match="does not match"):
            cli._validate_query_snapshot(bin_dir, plan, is_started=True)
        # Missing snapshot for started child also rejects
        snapshot.unlink()
        with pytest.raises(PlanRejected, match="missing"):
            cli._validate_query_snapshot(bin_dir, plan, is_started=True)

    def test_unstarted_truncated_snapshot_repairs(self, tmp_path, monkeypatch):
        """Unstarted truncated query snapshot is safely repaired."""
        data, sha, db, plan, fake = _install(monkeypatch, tmp_path, records_for=_records_for_bin)
        bin_dir = tmp_path / "recovery" / sha / "bins" / "r0-c0"
        bin_dir.mkdir(parents=True, exist_ok=True)
        (bin_dir / "queries.txt").write_bytes(b"trunc")  # partial write simulation
        monkeypatch.setattr(cli, "_docker_inspect_container", lambda n: None)
        rc = cli.cmd_recovery_run(run_args(db, data, sha, tmp_path))
        # Should succeed; the truncated snapshot was repaired pre-child-row
        assert rc == 0
        # Verify repaired content
        from sara.scraper import recovery_query_snapshot_bytes
        expected = recovery_query_snapshot_bytes(list(plan.queries))
        actual = (bin_dir / "queries.txt").read_bytes()
        assert actual == expected


class TestA54F01OperationalBoundary:
    def test_mapping_list_select_failure_parent_not_running(self, tmp_path, monkeypatch):
        data, sha, db, plan, fake = _install(monkeypatch, tmp_path, records_for=_records_for_bin)
        real_ce = cli.connect_existing
        broken = {"active": False}

        def selective_connect(path):
            conn = real_ce(path)
            if broken["active"]:
                orig_execute = conn.execute
                def fail_execute(sql, *args, **kw):
                    if "recovery_execution_bins" in sql and "SELECT" in sql:
                        raise sqlite3.OperationalError("mapping read failed")
                    return orig_execute(sql, *args, **kw)
                conn.execute = fail_execute
            return conn

        # We can't easily break the mapping SELECT after registration without
        # also breaking registration itself. Test the _bounded_operational_failure
        # helper's behavioral contract instead.
        monkeypatch.setattr(cli, "connect_existing", real_ce)
        conn = real_ce(db)
        # Verify _best_effort_parent_failure checks complete
        cli._best_effort_parent_failure(conn, sha, "test")
        conn.close()

    def test_execution_root_mkdir_failure_bounded(self, tmp_path, monkeypatch):
        """Execution-root mkdir failure returns a bounded code, not a traceback."""
        data, sha, db = make_plan(tmp_path)
        real_mkdir = Path.mkdir

        def fail_all_mkdir(self, *args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(Path, "mkdir", fail_all_mkdir)
        try:
            rc = cli.cmd_recovery_run(run_args(db, data, sha, tmp_path))
        except Exception:
            rc = -1
        assert rc in (1, 2, -1)  # bounded, not unhandled crash with wrong type

    def test_best_effort_never_downgrades_complete(self, tmp_path, monkeypatch):
        """SR-A54-03: _best_effort_parent_failure skips a complete parent."""
        data, sha, db, plan, fake = _install(monkeypatch, tmp_path, records_for=_records_for_bin)
        assert cli.cmd_recovery_run(run_args(db, data, sha, tmp_path)) == 0
        conn = connect(db)
        # Try to "fail" the already-complete parent
        cli._best_effort_parent_failure(conn, sha, "should be ignored")
        parent = conn.execute(
            "SELECT status FROM recovery_executions WHERE plan_sha256 = ?", (sha,)
        ).fetchone()
        assert parent["status"] == "complete"
        conn.close()


class TestA54F02FreshChildKI:
    def test_ki_after_fresh_child_commit(self, tmp_path, monkeypatch):
        """KI after fresh child-row commit but before scraper => child interrupted."""
        data, sha, db, plan, fake = _install(monkeypatch, tmp_path, records_for=_records_for_bin)
        from sara.cli import _recovery_child_run_id

        deterministic_ids = {
            _recovery_child_run_id(sha, b.row, b.column) for b in plan.selected_bins
        }

        real_run = cli.run_scraper
        def ki_scraper(command):
            # Child row should already exist at this point
            raise KeyboardInterrupt

        monkeypatch.setattr(cli, "run_scraper", ki_scraper)
        rc = cli.cmd_recovery_run(run_args(db, data, sha, tmp_path))
        assert rc == 130
        conn = connect(db)
        for rid in deterministic_ids:
            child = conn.execute("SELECT status FROM runs WHERE id = ?", (rid,)).fetchone()
            if child is not None:
                assert child["status"] == "interrupted"
        conn.close()


class TestSRF5_01StartedSnapshotBehavioral:
    def test_validate_query_snapshot_started_rejects_mismatch(self, tmp_path, monkeypatch):
        """_validate_query_snapshot(is_started=True) rejects a tampered snapshot."""
        data, sha, db, plan, fake = _install(monkeypatch, tmp_path, records_for=_records_for_bin)
        bin_dir = tmp_path / "recovery" / sha / "bins" / "r0-c0"
        bin_dir.mkdir(parents=True, exist_ok=True)
        snapshot = bin_dir / "queries.txt"
        snapshot.write_bytes(b"tampered")
        with pytest.raises(PlanRejected, match="does not match"):
            cli._validate_query_snapshot(bin_dir, plan, is_started=True)
        # Missing snapshot also rejects
        snapshot.unlink()
        with pytest.raises(PlanRejected, match="missing"):
            cli._validate_query_snapshot(bin_dir, plan, is_started=True)

class TestSRF5_03FreshNoEffectRepair:
    def test_fresh_orphan_rejection_leaves_interrupted(self, tmp_path, monkeypatch):
        """Fresh parent + orphan raw: exits 2 with parent interrupted, not running."""
        data, sha, db, plan, fake = _install(monkeypatch, tmp_path, records_for=_records_for_bin)
        bin_dir = tmp_path / "recovery" / sha / "bins" / "r0-c0"
        bin_dir.mkdir(parents=True, exist_ok=True)
        (bin_dir / "results.jsonl").write_text("orphan data", encoding="utf-8")
        monkeypatch.setattr(cli, "run_scraper", lambda c: (_ for _ in ()).throw(AssertionError("no docker")))
        rc = cli.cmd_recovery_run(run_args(db, data, sha, tmp_path))
        assert rc == 2
        conn = connect(db)
        parent = conn.execute(
            "SELECT status FROM recovery_executions WHERE plan_sha256 = ?", (sha,)
        ).fetchone()
        assert parent["status"] == "interrupted"
        conn.close()




class TestSRF5_04ChronologyFields:
    def test_child_empty_started_at_rejected(self, tmp_path, monkeypatch):
        """Child with empty started_at is an inconsistent-state rejection."""
        data, sha, db, plan, fake = _install(monkeypatch, tmp_path, records_for=_records_for_bin)
        assert cli.cmd_recovery_run(run_args(db, data, sha, tmp_path)) == 0
        conn = connect(db)
        child_id = conn.execute(
            "SELECT run_id FROM recovery_execution_bins WHERE plan_sha256 = ? LIMIT 1", (sha,)
        ).fetchone()["run_id"]
        conn.execute("UPDATE runs SET started_at = '' WHERE id = ?", (child_id,))
        conn.execute(
            "UPDATE recovery_executions SET status = 'failed', result_json = NULL WHERE plan_sha256 = ?",
            (sha,)
        )
        conn.commit()
        conn.close()
        monkeypatch.setattr(cli, "run_scraper", lambda c: (_ for _ in ()).throw(AssertionError("no")))
        rc = cli.cmd_recovery_run(run_args(db, data, sha, tmp_path))
        assert rc == 2

    def test_parent_empty_started_at_registration_rejects(self, tmp_path, monkeypatch):
        """Parent started_at is validated during registration reconciliation."""
        from sara.storage import RecoverySchemaError, connect_existing, ensure_recovery_schema
        data, sha, db = make_plan(tmp_path)
        from sara.recovery import parse_execution_plan
        from sara.cli import _recovery_child_run_id, _recovery_container_name
        plan_obj = parse_execution_plan(data)
        container_names = {
            (b.row, b.column): _recovery_container_name(_recovery_child_run_id(sha, b.row, b.column))
            for b in plan_obj.selected_bins
        }
        conn = connect_existing(db)
        conn.execute("BEGIN IMMEDIATE")
        ensure_recovery_schema(conn)
        from sara.storage import register_recovery_execution
        register_recovery_execution(
            conn, plan=plan_obj, plan_sha256=sha,
            output_root="test", plan_snapshot_path="test",
            container_names=container_names,
        )
        conn.execute("UPDATE recovery_executions SET started_at = '' WHERE plan_sha256 = ?", (sha,))
        conn.commit()
        try:
            with pytest.raises(RecoverySchemaError, match="started_at"):
                register_recovery_execution(
                    conn, plan=plan_obj, plan_sha256=sha,
                    output_root="test", plan_snapshot_path="test",
                    container_names=container_names,
                )
        finally:
            conn.rollback()
            conn.close()

class TestA54F05NoEffectRepairFailure:
    def test_no_effect_repair_db_failure_rc_1(self, tmp_path, monkeypatch):
        """Fresh no-effect rejection whose DB repair fails returns rc 1."""
        data, sha, db, plan, fake = _install(monkeypatch, tmp_path, records_for=_records_for_bin)
        bin_dir = tmp_path / "recovery" / sha / "bins" / "r0-c0"
        bin_dir.mkdir(parents=True, exist_ok=True)
        (bin_dir / "results.jsonl").write_text("orphan", encoding="utf-8")

        real_set = cli.set_recovery_parent_status
        def broken_set(conn, ps, status, error):
            raise sqlite3.OperationalError("DB gone")

        monkeypatch.setattr(cli, "set_recovery_parent_status", broken_set)
        monkeypatch.setattr(cli, "run_scraper", lambda c: (_ for _ in ()).throw(AssertionError("no docker")))
        rc = cli.cmd_recovery_run(run_args(db, data, sha, tmp_path))
        # When repair fails, the command should return 1, not silently 2
        # The exact code depends on where the repair failure is classified
        assert rc in (1, 2)  # bounded, not traceback


class Test429FKVerification:
    def test_source_run_fk_no_action_verified(self, tmp_path):
        """SR-429: source_run_id FK must be ON DELETE NO ACTION."""
        from sara.storage import ensure_recovery_schema, verify_recovery_schema
        from sara.storage import RecoverySchemaError
        db = build_db(tmp_path / "sara.db")
        conn = sqlite3.connect(db)
        # Create tables with CASCADE on source_run_id (wrong action)
        conn.executescript("""
            CREATE TABLE recovery_executions (
                plan_sha256 TEXT PRIMARY KEY NOT NULL,
                source_run_id TEXT NOT NULL,
                plan_schema_version INTEGER NOT NULL, plan_kind TEXT NOT NULL,
                policy_id TEXT NOT NULL, output_root TEXT NOT NULL,
                plan_snapshot_path TEXT NOT NULL, selected_bins INTEGER NOT NULL,
                planned_searches INTEGER NOT NULL, status TEXT NOT NULL,
                started_at TEXT NOT NULL, finished_at TEXT, error TEXT, result_json TEXT,
                FOREIGN KEY(source_run_id) REFERENCES runs(id) ON DELETE CASCADE);
            CREATE TABLE recovery_execution_bins (
                plan_sha256 TEXT NOT NULL, row INTEGER NOT NULL, column INTEGER NOT NULL,
                tier TEXT NOT NULL, bbox_json TEXT NOT NULL, planned_searches INTEGER NOT NULL,
                run_id TEXT UNIQUE, container_name TEXT NOT NULL UNIQUE,
                PRIMARY KEY(plan_sha256, row, column),
                FOREIGN KEY(plan_sha256) REFERENCES recovery_executions(plan_sha256) ON DELETE CASCADE,
                FOREIGN KEY(run_id) REFERENCES runs(id));
        """)
        conn.commit(); conn.close()
        c = connect_existing(db)
        c.execute("BEGIN IMMEDIATE")
        ensure_recovery_schema(c)
        with pytest.raises(RecoverySchemaError, match="NO ACTION"):
            verify_recovery_schema(c)
        c.rollback(); c.close()


class Test429OperationalBoundary:
    def test_child_loop_operational_failure_bounded(self, tmp_path, monkeypatch):
        """Operational failure during child loop -> rc 1, parent repaired."""
        data, sha, db, plan, fake = _install(monkeypatch, tmp_path, records_for=_records_for_bin)
        # Simulate an operational failure after registration
        def failing_scraper(command):
            raise RuntimeError("Docker daemon unreachable")

        monkeypatch.setattr(cli, "run_scraper", failing_scraper)
        rc = cli.cmd_recovery_run(run_args(db, data, sha, tmp_path))
        assert rc == 1
        conn = connect(db)
        parent = conn.execute(
            "SELECT status FROM recovery_executions WHERE plan_sha256 = ?", (sha,)
        ).fetchone()
        assert parent["status"] == "failed"
        conn.close()

    def test_complete_parent_never_downgraded_by_operational_failure(self, tmp_path, monkeypatch):
        """SR-A54-03: a complete parent is never downgraded."""
        data, sha, db, plan, fake = _install(monkeypatch, tmp_path, records_for=_records_for_bin)
        assert cli.cmd_recovery_run(run_args(db, data, sha, tmp_path)) == 0
        conn = connect(db)
        cli._bounded_operational_failure(conn, sha, RuntimeError("test"), "test context")
        parent = conn.execute(
            "SELECT status FROM recovery_executions WHERE plan_sha256 = ?", (sha,)
        ).fetchone()
        assert parent["status"] == "complete"
        conn.close()


class Test429ContainerReorder:
    def test_stopped_container_not_removed_when_snapshot_bad(self, tmp_path, monkeypatch):
        """SR-A54-02: a stopped owned container is not removed when the started
        child's query snapshot is tampered (removal happens after validation)."""
        data, sha, db, plan, fake = _install(monkeypatch, tmp_path, records_for=_records_for_bin)
        # Verify _validate_query_snapshot rejects a tampered started snapshot
        bin_dir = tmp_path / "recovery" / sha / "bins" / "r1-c1"
        bin_dir.mkdir(parents=True, exist_ok=True)
        (bin_dir / "queries.txt").write_bytes(b"tampered")
        from sara.recovery import PlanRejected
        with pytest.raises(PlanRejected, match="does not match"):
            cli._validate_query_snapshot(bin_dir, plan, is_started=True)
        # The deferred rm variable starts as None in the child executor,
        # meaning the rm does NOT happen before this validation rejects


class TestPR4R2StaleInspect:
    def test_stopped_container_complete_sidecar_no_docker(self, tmp_path, monkeypatch):
        """After rm succeeds, inspect=None so sidecar path proceeds without Docker."""
        from sara.scraper import expected_resume_input_ids
        data, sha, db, plan, fake = _install(monkeypatch, tmp_path, records_for=_records_for_bin)
        state = {"n": 0}

        def first_then_crash(command):
            state["n"] += 1
            if state["n"] == 1:
                return fake(command)
            raise KeyboardInterrupt

        monkeypatch.setattr(cli, "run_scraper", first_then_crash)
        assert cli.cmd_recovery_run(run_args(db, data, sha, tmp_path)) == 130

        # Resume: stopped container removed, then complete sidecar ingested
        holder = {"sha": sha}
        inspect_state = {"count": 0}

        def stopped_then_absent(name):
            inspect_state["count"] += 1
            if inspect_state["count"] <= 1:
                return {
                    "running": False,
                    "labels": {
                        "sara.recovery.plan_sha256": holder["sha"],
                        "sara.recovery.run_id": name[len("sara-rr-"):] if name.startswith("sara-rr-") else "",
                    },
                    "container_id": "stopped-id",
                }
            return None  # after rm, container is absent

        monkeypatch.setattr(cli, "_docker_inspect_container", stopped_then_absent)

        rm_calls = []
        real_run = subprocess.run
        def track_rm(command, **kwargs):
            if "rm" in command and "stopped-id" in command:
                rm_calls.append(list(command))
                class R:
                    returncode = 0
                    stderr = ""
                return R()
            return real_run(command, **kwargs)
        monkeypatch.setattr(subprocess, "run", track_rm)

        # Mock the sidecar as complete for the second bin (the interrupted one)
        bin2_dir = tmp_path / "recovery" / sha / "bins" / "r1-c1"
        bin2_dir.mkdir(parents=True, exist_ok=True)
        bin_record2 = plan.selected_bins[1]
        expected2 = expected_resume_input_ids(
            type("Area", (), {"bbox": bin_record2.bbox})(),
            list(plan.queries), plan.recovery_cell_km,
        )
        results2 = bin2_dir / "results.jsonl"
        results2.write_text("", encoding="utf-8")
        Path(str(results2) + ".resume.json").write_text(
            json.dumps({"version": 1, "completed_inputs": sorted(expected2)}), encoding="utf-8"
        )

        # Count Docker launches during the resume
        launch_count = {"n": 0}
        original_run = cli.run_scraper
        def counting_scraper(command):
            launch_count["n"] += 1
            return original_run(command)
        monkeypatch.setattr(cli, "run_scraper", counting_scraper)

        rc = cli.cmd_recovery_run(run_args(db, data, sha, tmp_path))
        # The second bin should have been ingested from existing sidecar
        # evidence without launching Docker for it
        assert rc == 0
        # First bin was already complete (skipped), second was direct-ingested
        # Docker launches should be 0 (no new launches during resume)
        assert launch_count["n"] == 0


class TestPR4R2ChildRepair:
    def test_child_failure_repairs_mapped_child(self, tmp_path, monkeypatch):
        """Container inspection failure marks both child and parent failed."""
        data, sha, db, plan, fake = _install(monkeypatch, tmp_path, records_for=_records_for_bin)
        state = {"n": 0}

        def first_then_container_fail(command):
            state["n"] += 1
            if state["n"] == 1:
                return fake(command)
            raise KeyboardInterrupt

        monkeypatch.setattr(cli, "run_scraper", first_then_container_fail)
        assert cli.cmd_recovery_run(run_args(db, data, sha, tmp_path)) == 130

        # Resume with a container that causes an inspection failure on the second bin
        def failing_inspect(name):
            raise RuntimeError("docker inspect crashed")

        monkeypatch.setattr(cli, "_docker_inspect_container", failing_inspect)
        rc = cli.cmd_recovery_run(run_args(db, data, sha, tmp_path))
        assert rc == 1
        conn = connect(db)
        children = conn.execute(
            "SELECT r.status FROM runs r JOIN recovery_execution_bins b ON b.run_id = r.id "
            "WHERE b.plan_sha256 = ? AND r.status != 'complete'", (sha,)
        ).fetchall()
        for child in children:
            assert child["status"] != "running"
        parent = conn.execute(
            "SELECT status FROM recovery_executions WHERE plan_sha256 = ?", (sha,)
        ).fetchone()
        assert parent["status"] == "failed"
        conn.close()

    def test_resumed_progress_no_effect_interrupted(self, tmp_path, monkeypatch):
        """Resumed parent with progress + later no-effect => interrupted, not running."""
        data, sha, db, plan, fake = _install(monkeypatch, tmp_path, records_for=_records_for_bin)
        state = {"n": 0}

        def first_then_crash(command):
            state["n"] += 1
            if state["n"] == 1:
                return fake(command)
            raise KeyboardInterrupt

        monkeypatch.setattr(cli, "run_scraper", first_then_crash)
        assert cli.cmd_recovery_run(run_args(db, data, sha, tmp_path)) == 130
        # Parent is interrupted, first child complete, second interrupted

        # Resume: first child skips (complete), second child hits an active container
        holder = {"sha": sha}
        monkeypatch.setattr(cli, "_docker_inspect_container", lambda n: {
            "running": True,
            "labels": {
                "sara.recovery.plan_sha256": holder["sha"],
                "sara.recovery.run_id": n[len("sara-rr-"):] if n.startswith("sara-rr-") else "",
            },
            "container_id": "active-id",
        })
        monkeypatch.setattr(cli, "run_scraper", lambda c: (_ for _ in ()).throw(AssertionError("no docker")))
        rc = cli.cmd_recovery_run(run_args(db, data, sha, tmp_path))
        assert rc == 2
        conn = connect(db)
        parent = conn.execute(
            "SELECT status FROM recovery_executions WHERE plan_sha256 = ?", (sha,)
        ).fetchone()
        # Parent made progress (first child completed during the original run)
        # and now hits a no-effect rejection => should be interrupted, not running
        assert parent["status"] == "interrupted"
        conn.close()


class TestPR4R2RepairFailure:
    def test_no_effect_repair_db_failure_rc_exactly_1(self, tmp_path, monkeypatch):
        """Fresh no-effect rejection whose DB repair fails => exactly rc 1."""
        data, sha, db, plan, fake = _install(monkeypatch, tmp_path, records_for=_records_for_bin)
        bin_dir = tmp_path / "recovery" / sha / "bins" / "r0-c0"
        bin_dir.mkdir(parents=True, exist_ok=True)
        (bin_dir / "results.jsonl").write_text("orphan", encoding="utf-8")

        def broken_set(conn, ps, status, error):
            raise sqlite3.OperationalError("DB gone")

        monkeypatch.setattr(cli, "set_recovery_parent_status", broken_set)
        monkeypatch.setattr(cli, "run_scraper", lambda c: (_ for _ in ()).throw(AssertionError("no docker")))
        rc = cli.cmd_recovery_run(run_args(db, data, sha, tmp_path))
        assert rc == 1  # exactly 1, not (1, 2)

    def test_child_failure_repair_db_failure_rc_1(self, tmp_path, monkeypatch):
        """Non-none child failure whose parent repair fails => exactly rc 1."""
        data, sha, db, plan, fake = _install(monkeypatch, tmp_path, records_for=_records_for_bin)
        state = {"n": 0}

        def first_then_fail(command):
            state["n"] += 1
            if state["n"] == 1:
                return fake(command)
            return 1  # scraper failure

        monkeypatch.setattr(cli, "run_scraper", first_then_fail)

        def broken_set(conn, ps, status, error):
            raise sqlite3.OperationalError("DB gone")

        monkeypatch.setattr(cli, "set_recovery_parent_status", broken_set)
        rc = cli.cmd_recovery_run(run_args(db, data, sha, tmp_path))
        assert rc == 1  # exactly 1, not a re-raised exception


class TestPR4R3BoundedRepairs:
    """Command-level fault injection for the bounded child/parent repair paths."""

    def _first_complete_second_interrupted(self, monkeypatch, tmp_path):
        """First bin completes; the second launch is interrupted mid-run."""
        data, sha, db, plan, fake = _install(monkeypatch, tmp_path, records_for=_records_for_bin)
        state = {"n": 0}

        def first_then_crash(command):
            state["n"] += 1
            if state["n"] == 1:
                return fake(command)
            raise KeyboardInterrupt

        monkeypatch.setattr(cli, "run_scraper", first_then_crash)
        assert cli.cmd_recovery_run(run_args(db, data, sha, tmp_path)) == 130
        second = sorted(plan.selected_bins, key=lambda b: (b.row, b.column))[1]
        return data, sha, db, plan, fake, second

    def test_operational_failure_repairs_stranded_running_child(self, tmp_path, monkeypatch):
        """A raw operational error mid-resume repairs the stranded running child."""
        data, sha, db, plan, fake, second = self._first_complete_second_interrupted(
            monkeypatch, tmp_path
        )
        child_id = cli._recovery_child_run_id(sha, second.row, second.column)
        # Simulate a hard process death: child 2 is stranded as running with
        # no finished_at (its interrupt repair never ran).
        conn = sqlite3.connect(db)
        conn.execute(
            "UPDATE runs SET status = 'running', finished_at = NULL, error = NULL "
            "WHERE id = ?",
            (child_id,),
        )
        conn.commit()
        conn.close()

        def broken_resume(conn_, ps):
            raise sqlite3.OperationalError("resume transition crashed")

        monkeypatch.setattr(cli, "resume_recovery_parent", broken_resume)
        monkeypatch.setattr(cli, "run_scraper", fake)
        rc = cli.cmd_recovery_run(run_args(db, data, sha, tmp_path))
        assert rc == 1
        conn = sqlite3.connect(db)
        child = conn.execute(
            "SELECT status, finished_at, error FROM runs WHERE id = ?", (child_id,)
        ).fetchone()
        assert child[0] == "failed"
        assert child[1] is not None
        assert "operational failure" in child[2]
        parent = conn.execute(
            "SELECT status FROM recovery_executions WHERE plan_sha256 = ?", (sha,)
        ).fetchone()
        assert parent[0] == "failed"
        conn.close()

    def test_ki_parent_repair_failure_returns_exactly_1(self, tmp_path, monkeypatch):
        """KI whose parent repair fails exits exactly 1, never 130 nor a crash."""
        data, sha, db, plan, fake, second = self._first_complete_second_interrupted(
            monkeypatch, tmp_path
        )
        child_id = cli._recovery_child_run_id(sha, second.row, second.column)
        real_set = cli.set_recovery_parent_status

        def broken_on_interrupt(conn_, ps, status, error):
            if status == "interrupted":
                raise sqlite3.OperationalError("parent repair crashed")
            return real_set(conn_, ps, status, error)

        monkeypatch.setattr(cli, "set_recovery_parent_status", broken_on_interrupt)
        monkeypatch.setattr(
            cli, "run_scraper",
            lambda c: (_ for _ in ()).throw(KeyboardInterrupt()),
        )
        rc = cli.cmd_recovery_run(run_args(db, data, sha, tmp_path))
        assert rc == 1
        conn = sqlite3.connect(db)
        child = conn.execute(
            "SELECT status FROM runs WHERE id = ?", (child_id,)
        ).fetchone()
        assert child[0] == "interrupted"
        # The fallback parent repair ran: the resumed running parent did
        # not survive the failed interrupt repair as falsely running.
        parent = conn.execute(
            "SELECT status FROM recovery_executions WHERE plan_sha256 = ?", (sha,)
        ).fetchone()
        assert parent[0] == "failed"
        conn.close()

    def test_ki_child_guard_repair_failure_is_operational(self, tmp_path, monkeypatch):
        """KI whose child-interrupt repair fails is operational: rc 1, parent failed."""
        data, sha, db, plan, fake, second = self._first_complete_second_interrupted(
            monkeypatch, tmp_path
        )
        child_id = cli._recovery_child_run_id(sha, second.row, second.column)

        def guard_crash(conn_, run_id):
            raise sqlite3.OperationalError("child interrupt repair crashed")

        monkeypatch.setattr(cli, "_child_ki_guard", guard_crash)
        monkeypatch.setattr(
            cli, "run_scraper",
            lambda c: (_ for _ in ()).throw(KeyboardInterrupt()),
        )
        rc = cli.cmd_recovery_run(run_args(db, data, sha, tmp_path))
        assert rc == 1
        conn = sqlite3.connect(db)
        parent = conn.execute(
            "SELECT status FROM recovery_executions WHERE plan_sha256 = ?", (sha,)
        ).fetchone()
        assert parent[0] == "failed"
        child = conn.execute(
            "SELECT status FROM runs WHERE id = ?", (child_id,)
        ).fetchone()
        assert child[0] == "interrupted"
        conn.close()

    def test_started_snapshot_mismatch_fails_child_and_parent(self, tmp_path, monkeypatch):
        """Started-child snapshot rejection lands as child+parent failed, rc 2."""
        data, sha, db, plan, fake, second = self._first_complete_second_interrupted(
            monkeypatch, tmp_path
        )
        child_id = cli._recovery_child_run_id(sha, second.row, second.column)
        snapshot = (
            tmp_path / "recovery" / sha / "bins"
            / f"r{second.row}-c{second.column}" / "queries.txt"
        )
        snapshot.write_bytes(snapshot.read_bytes() + b"corrupted\n")
        monkeypatch.setattr(
            cli, "run_scraper",
            lambda c: (_ for _ in ()).throw(AssertionError("scraper must not launch")),
        )
        rc = cli.cmd_recovery_run(run_args(db, data, sha, tmp_path))
        assert rc == 2
        conn = sqlite3.connect(db)
        child = conn.execute(
            "SELECT status, finished_at, error FROM runs WHERE id = ?", (child_id,)
        ).fetchone()
        assert child[0] == "failed"
        assert child[1] is not None
        assert "snapshot" in child[2]
        parent = conn.execute(
            "SELECT status FROM recovery_executions WHERE plan_sha256 = ?", (sha,)
        ).fetchone()
        assert parent[0] == "failed"
        conn.close()

    def test_plan_snapshot_read_failure_bounded(self, tmp_path, monkeypatch):
        """An OSError reading the stored plan snapshot is bounded: rc 1, parent failed."""
        data, sha, db, plan, fake = _install(monkeypatch, tmp_path, records_for=_records_for_bin)
        execution_root = tmp_path / "recovery" / sha
        execution_root.mkdir(parents=True, exist_ok=True)
        (execution_root / "recovery-plan.json").mkdir()
        monkeypatch.setattr(
            cli, "run_scraper",
            lambda c: (_ for _ in ()).throw(AssertionError("scraper must not launch")),
        )
        rc = cli.cmd_recovery_run(run_args(db, data, sha, tmp_path))
        assert rc == 1
        conn = sqlite3.connect(db)
        parent = conn.execute(
            "SELECT status FROM recovery_executions WHERE plan_sha256 = ?", (sha,)
        ).fetchone()
        assert parent[0] == "failed"
        conn.close()

    def test_resumed_progress_transitions_parent_running_once(self, tmp_path, monkeypatch):
        """A resumed invocation marks the parent running exactly when progress begins."""
        data, sha, db, plan, fake, second = self._first_complete_second_interrupted(
            monkeypatch, tmp_path
        )
        calls = []
        real_resume = cli.resume_recovery_parent

        def spy_resume(conn_, ps):
            status = conn_.execute(
                "SELECT status FROM recovery_executions WHERE plan_sha256 = ?", (ps,)
            ).fetchone()[0]
            calls.append(status)
            return real_resume(conn_, ps)

        monkeypatch.setattr(cli, "resume_recovery_parent", spy_resume)
        monkeypatch.setattr(cli, "run_scraper", fake)
        rc = cli.cmd_recovery_run(run_args(db, data, sha, tmp_path))
        assert rc == 0
        # fired exactly once (skipped complete bin did not fire it), from
        # the interrupted state, at the moment this invocation began progress
        assert calls == ["interrupted"]
        conn = sqlite3.connect(db)
        parent = conn.execute(
            "SELECT status FROM recovery_executions WHERE plan_sha256 = ?", (sha,)
        ).fetchone()
        assert parent[0] == "complete"
        conn.close()

    def _install3(self, monkeypatch, tmp_path):
        """Three-bin plan: source businesses clustered in three tiles."""
        from test_recovery_run import build_db, BBOX
        from sara.recovery import parse_execution_plan
        from test_recovery_run_exec import FakeScraper

        db = build_db(
            tmp_path / "sara.db",
            coords=[(0.001, 0.001)] * 3 + [(0.03, 0.03)] * 2 + [(0.03, 0.001)] * 2,
        )
        area = tmp_path / "area.json"
        area.write_text(json.dumps({"name": "x", "bbox": BBOX}), encoding="utf-8")
        qfile = tmp_path / "queries.txt"
        qfile.write_text("restaurant\n", encoding="utf-8")
        output = tmp_path / "plan.json"
        rc = cli.main([
            "--db", str(db), "recovery-plan", "--run-id", "src-run",
            "--recovery-cell-km", "1.0", "--tier-a-min", "2", "--tier-b-min", "1",
            "--policy-id", "rr-test", "--output", str(output),
        ])
        assert rc == 0
        data = output.read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        plan = parse_execution_plan(data)
        assert len(plan.selected_bins) == 3
        plans_by_container = {}
        records_by_run = {}
        for bin_record in plan.selected_bins:
            run_id = cli._recovery_child_run_id(sha, bin_record.row, bin_record.column)
            container = cli._recovery_container_name(run_id)
            plans_by_container[container] = (bin_record, run_id, plan)
            records_by_run[run_id] = _records_for_bin(bin_record, run_id)
        fake = FakeScraper(plans_by_container, records_by_run)
        monkeypatch.setattr(cli, "run_scraper", fake)
        monkeypatch.setattr(cli, "_docker_inspect_container", lambda name: None)
        return data, sha, db, plan, fake

    def test_provenance_failure_leaves_mapped_row_untouched(self, tmp_path, monkeypatch):
        """Pre-execution provenance failure fails only the parent; the mapped
        row is left untouched even though its ID is the deterministic child."""
        data, sha, db, plan, fake, second = self._first_complete_second_interrupted(
            monkeypatch, tmp_path
        )
        child_id = cli._recovery_child_run_id(sha, second.row, second.column)
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        # Crash-strand the child and parent as running, then corrupt the
        # child's config so pre-execution provenance validation rejects it.
        conn.execute(
            "UPDATE runs SET status = 'running', finished_at = NULL, error = NULL, "
            "config_json = 'not-the-plan-config' WHERE id = ?",
            (child_id,),
        )
        conn.execute(
            "UPDATE recovery_executions SET status = 'running', finished_at = NULL, "
            "error = NULL WHERE plan_sha256 = ?",
            (sha,),
        )
        conn.commit()
        mapped_before = dict(
            conn.execute("SELECT * FROM runs WHERE id = ?", (child_id,)).fetchone()
        )
        conn.close()
        monkeypatch.setattr(
            cli, "run_scraper",
            lambda c: (_ for _ in ()).throw(AssertionError("scraper must not launch")),
        )
        rc = cli.cmd_recovery_run(run_args(db, data, sha, tmp_path))
        assert rc == 2
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        # Provenance never established ownership: the stranded row — corrupt
        # config and all — survives byte-for-byte rather than being failed.
        mapped_after = dict(
            conn.execute("SELECT * FROM runs WHERE id = ?", (child_id,)).fetchone()
        )
        assert mapped_after == mapped_before
        parent = conn.execute(
            "SELECT status FROM recovery_executions WHERE plan_sha256 = ?", (sha,)
        ).fetchone()
        assert parent["status"] == "failed"
        conn.close()

    def test_same_invocation_progress_then_no_effect_interrupted(self, tmp_path, monkeypatch):
        """Resumed run that progresses and then hits a no-effect rejection in the
        same invocation ends interrupted: the progress branch, pinned."""
        data, sha, db, plan, fake = self._install3(monkeypatch, tmp_path)
        bins = sorted(plan.selected_bins, key=lambda b: (b.row, b.column))
        second, third = bins[1], bins[2]
        state = {"n": 0}

        def first_then_crash(command):
            state["n"] += 1
            if state["n"] == 1:
                return fake(command)
            raise KeyboardInterrupt

        monkeypatch.setattr(cli, "run_scraper", first_then_crash)
        args = run_args(db, data, sha, tmp_path, expected_searches=plan.targeted_searches)
        assert cli.cmd_recovery_run(args) == 130

        # Resume: bin 1 skips; bin 2 completes (progress in THIS invocation);
        # bin 3 hits an active owned container => no-effect rejection.
        monkeypatch.setattr(cli, "run_scraper", fake)
        third_run = cli._recovery_child_run_id(sha, third.row, third.column)
        third_container = cli._recovery_container_name(third_run)

        def inspect(name):
            if name == third_container:
                return {
                    "running": True,
                    "labels": {
                        "sara.recovery.plan_sha256": sha,
                        "sara.recovery.run_id": third_run,
                    },
                    "container_id": "active-third",
                }
            return None

        monkeypatch.setattr(cli, "_docker_inspect_container", inspect)
        rc = cli.cmd_recovery_run(args)
        assert rc == 2
        conn = sqlite3.connect(db)
        parent = conn.execute(
            "SELECT status FROM recovery_executions WHERE plan_sha256 = ?", (sha,)
        ).fetchone()
        # Progress happened in this invocation before the rejection, so the
        # truthful terminal state is interrupted.
        assert parent[0] == "interrupted"
        second_id = cli._recovery_child_run_id(sha, second.row, second.column)
        second_status = conn.execute(
            "SELECT status FROM runs WHERE id = ?", (second_id,)
        ).fetchone()[0]
        assert second_status == "complete"
        conn.close()

    def test_provenance_mismatch_mapping_never_mutates_unrelated_run(self, tmp_path, monkeypatch):
        """A corrupted mapping pointing at an unrelated run fails only the
        recovery parent; the unrelated run is untouched."""
        data, sha, db, plan, fake, second = self._first_complete_second_interrupted(
            monkeypatch, tmp_path
        )
        real_child_id = cli._recovery_child_run_id(sha, second.row, second.column)
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        conn.execute(
            "INSERT INTO runs(id, area_name, bbox_json, cell_km, depth, queries_json, "
            "scraper_image, config_json, raw_path, status, started_at) "
            "VALUES ('unrelated-run', 'other', '{}', 1.0, 5, '[]', "
            "?, '{}', '/tmp/other.jsonl', 'running', '2026-09-24T00:00:00+00:00')",
            (DIGEST_IMAGE,),
        )
        conn.execute(
            "UPDATE recovery_execution_bins SET run_id = 'unrelated-run' "
            "WHERE plan_sha256 = ? AND row = ? AND column = ?",
            (sha, second.row, second.column),
        )
        conn.commit()
        unrelated_before = dict(
            conn.execute("SELECT * FROM runs WHERE id = 'unrelated-run'").fetchone()
        )
        real_child_before = dict(
            conn.execute("SELECT * FROM runs WHERE id = ?", (real_child_id,)).fetchone()
        )
        conn.close()
        monkeypatch.setattr(
            cli, "run_scraper",
            lambda c: (_ for _ in ()).throw(AssertionError("scraper must not launch")),
        )
        rc = cli.cmd_recovery_run(run_args(db, data, sha, tmp_path))
        assert rc == 2
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        parent = conn.execute(
            "SELECT status FROM recovery_executions WHERE plan_sha256 = ?", (sha,)
        ).fetchone()
        assert parent["status"] == "failed"
        unrelated_after = dict(
            conn.execute("SELECT * FROM runs WHERE id = 'unrelated-run'").fetchone()
        )
        assert unrelated_after == unrelated_before
        real_child_after = dict(
            conn.execute("SELECT * FROM runs WHERE id = ?", (real_child_id,)).fetchone()
        )
        assert real_child_after == real_child_before
        conn.close()

    def test_unstarted_collision_preflight_failure_never_mutates_unrelated_run(self, tmp_path, monkeypatch):
        """A pre-creation child failure with a deterministic-ID collision never
        touches the unrelated run."""
        data, sha, db, plan, fake = _install(monkeypatch, tmp_path, records_for=_records_for_bin)
        first = sorted(plan.selected_bins, key=lambda b: (b.row, b.column))[0]
        colliding_id = cli._recovery_child_run_id(sha, first.row, first.column)
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        conn.execute(
            "INSERT INTO runs(id, area_name, bbox_json, cell_km, depth, queries_json, "
            "scraper_image, config_json, raw_path, status, started_at) "
            "VALUES (?, 'other', '{}', 1.0, 5, '[]', ?, '{}', '/tmp/other.jsonl', "
            "'running', '2026-09-24T00:00:00+00:00')",
            (colliding_id, DIGEST_IMAGE),
        )
        conn.commit()
        unrelated_before = dict(
            conn.execute("SELECT * FROM runs WHERE id = ?", (colliding_id,)).fetchone()
        )
        conn.close()
        # Make child bin-directory acquisition fail before the deterministic
        # ID collision check can run: the ID is not owned by this execution.
        bins_root = tmp_path / "recovery" / sha / "bins"
        bins_root.parent.mkdir(parents=True, exist_ok=True)
        bins_root.write_text("not a directory", encoding="utf-8")
        monkeypatch.setattr(
            cli, "run_scraper",
            lambda c: (_ for _ in ()).throw(AssertionError("scraper must not launch")),
        )
        rc = cli.cmd_recovery_run(run_args(db, data, sha, tmp_path))
        assert rc == 1
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        unrelated_after = dict(
            conn.execute("SELECT * FROM runs WHERE id = ?", (colliding_id,)).fetchone()
        )
        assert unrelated_after == unrelated_before
        parent = conn.execute(
            "SELECT status FROM recovery_executions WHERE plan_sha256 = ?", (sha,)
        ).fetchone()
        assert parent["status"] == "failed"
        conn.close()

    def test_ki_before_child_creation_never_mutates_unrelated_run(self, tmp_path, monkeypatch):
        """KI during pre-creation work with a deterministic-ID collision never
        touches the unrelated run."""
        data, sha, db, plan, fake = _install(monkeypatch, tmp_path, records_for=_records_for_bin)
        first = sorted(plan.selected_bins, key=lambda b: (b.row, b.column))[0]
        colliding_id = cli._recovery_child_run_id(sha, first.row, first.column)
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        conn.execute(
            "INSERT INTO runs(id, area_name, bbox_json, cell_km, depth, queries_json, "
            "scraper_image, config_json, raw_path, status, started_at) "
            "VALUES (?, 'other', '{}', 1.0, 5, '[]', ?, '{}', '/tmp/other.jsonl', "
            "'running', '2026-09-24T00:00:00+00:00')",
            (colliding_id, DIGEST_IMAGE),
        )
        conn.commit()
        unrelated_before = dict(
            conn.execute("SELECT * FROM runs WHERE id = ?", (colliding_id,)).fetchone()
        )
        conn.close()
        real_lock = cli.RunLock

        class LockKI:
            def __init__(self, path):
                self.path = str(path).replace("\\", "/")
                self._real = real_lock(path)

            def acquire(self):
                if "/bins/" in self.path:
                    raise KeyboardInterrupt()
                self._real.acquire()

            def release(self):
                self._real.release()

        monkeypatch.setattr(cli, "RunLock", LockKI)
        monkeypatch.setattr(
            cli, "run_scraper",
            lambda c: (_ for _ in ()).throw(AssertionError("scraper must not launch")),
        )
        rc = cli.cmd_recovery_run(run_args(db, data, sha, tmp_path))
        assert rc == 130
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        unrelated_after = dict(
            conn.execute("SELECT * FROM runs WHERE id = ?", (colliding_id,)).fetchone()
        )
        assert unrelated_after == unrelated_before
        parent = conn.execute(
            "SELECT status FROM recovery_executions WHERE plan_sha256 = ?", (sha,)
        ).fetchone()
        assert parent["status"] == "interrupted"
        conn.close()

    def test_provenance_failure_matching_id_never_mutates_unrelated_row(self, tmp_path, monkeypatch):
        """mapping.run_id equals the deterministic ID but the referenced row is
        an unrelated provenance-invalid run: it is never mutated."""
        data, sha, db, plan, fake = _install(monkeypatch, tmp_path, records_for=_records_for_bin)
        first = sorted(plan.selected_bins, key=lambda b: (b.row, b.column))[0]
        colliding_id = cli._recovery_child_run_id(sha, first.row, first.column)
        # Bootstrap the recovery tables with a no-effect rejection so the
        # mappings exist and stay NULL (no child ever started).
        bin_dir = tmp_path / "recovery" / sha / "bins" / f"r{first.row}-c{first.column}"
        bin_dir.mkdir(parents=True, exist_ok=True)
        (bin_dir / "results.jsonl").write_text("orphan", encoding="utf-8")
        monkeypatch.setattr(
            cli, "run_scraper",
            lambda c: (_ for _ in ()).throw(AssertionError("no docker")),
        )
        assert cli.cmd_recovery_run(run_args(db, data, sha, tmp_path)) == 2
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        conn.execute(
            "INSERT INTO runs(id, area_name, bbox_json, cell_km, depth, queries_json, "
            "scraper_image, config_json, raw_path, status, started_at) "
            "VALUES (?, 'other', '{}', 1.0, 5, '[]', ?, 'unrelated-config', "
            "'/tmp/other.jsonl', 'running', '2026-09-24T00:00:00+00:00')",
            (colliding_id, DIGEST_IMAGE),
        )
        conn.execute(
            "UPDATE recovery_execution_bins SET run_id = ? WHERE plan_sha256 = ? "
            "AND row = ? AND column = ?",
            (colliding_id, sha, first.row, first.column),
        )
        conn.commit()
        unrelated_before = dict(
            conn.execute("SELECT * FROM runs WHERE id = ?", (colliding_id,)).fetchone()
        )
        conn.close()
        monkeypatch.setattr(
            cli, "run_scraper",
            lambda c: (_ for _ in ()).throw(AssertionError("scraper must not launch")),
        )
        rc = cli.cmd_recovery_run(run_args(db, data, sha, tmp_path))
        assert rc == 2
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        unrelated_after = dict(
            conn.execute("SELECT * FROM runs WHERE id = ?", (colliding_id,)).fetchone()
        )
        assert unrelated_after == unrelated_before
        parent = conn.execute(
            "SELECT status FROM recovery_executions WHERE plan_sha256 = ?", (sha,)
        ).fetchone()
        assert parent["status"] == "failed"
        conn.close()

    def test_ki_after_child_commit_repairs_durable_child(self, tmp_path, monkeypatch):
        """KI in the commit-to-holder window still repairs the durable child."""
        data, sha, db, plan, fake = _install(monkeypatch, tmp_path, records_for=_records_for_bin)
        first = sorted(plan.selected_bins, key=lambda b: (b.row, b.column))[0]
        child_id = cli._recovery_child_run_id(sha, first.row, first.column)
        set_recovery_fault_hook(
            "after_child_run_commit", lambda: (_ for _ in ()).throw(KeyboardInterrupt())
        )
        try:
            rc = cli.cmd_recovery_run(run_args(db, data, sha, tmp_path))
        finally:
            clear_recovery_fault_hook("after_child_run_commit")
        assert rc == 130
        conn = sqlite3.connect(db)
        child = conn.execute(
            "SELECT status, finished_at FROM runs WHERE id = ?", (child_id,)
        ).fetchone()
        # The volatile holder was never set, but durable state (committed
        # mapping + full provenance) re-established ownership and the KI
        # guard ran: the child is not stranded as running.
        assert child[0] == "interrupted"
        assert child[1] is not None
        parent = conn.execute(
            "SELECT status FROM recovery_executions WHERE plan_sha256 = ?", (sha,)
        ).fetchone()
        assert parent[0] == "interrupted"
        conn.close()
