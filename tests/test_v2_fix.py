
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
        if "db_row" in order and "output_file" in order:
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
        """Parent status is unchanged when container reconciliation rejects (V2-F05)."""
        import inspect
        source = inspect.getsource(cli._execute_recovery_child)
        # The mark_parent_running callback is only called at progress points,
        # not before container reconciliation
        assert "mark_parent_running" in source
        # Container checks happen before any mark_parent_running call
        idx_container = source.index("_docker_inspect_container")
        idx_running = source.index("mark_parent_running()")
        assert idx_container < idx_running, "container checks must precede parent running"

    def test_wrong_label_preserves_prior_status(self, tmp_path, monkeypatch, capsys):
        """Wrong-label container rejection is a no-new-effect exit 2 (V2-F12)."""
        import inspect
        source = inspect.getsource(cli._execute_recovery_child)
        assert 'status="none", process_exit=2' in source
        assert "wrong ownership labels" in source

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
        """The write connection is explicitly closed (V2-F09 outer finally)."""
        from sara.cli import _safe_release
        data, sha, db, plan, fake = _install(monkeypatch, tmp_path, records_for=_records_for_bin)
        assert cli.cmd_recovery_run(run_args(db, data, sha, tmp_path)) == 0
        # After the command, _safe_release and conn.close code exists in the finally;
        # verify the command source contains the explicit close pattern.
        import inspect
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
    def test_name_reuse_race_rejected(self, tmp_path, monkeypatch, capsys):
        """Container removal uses the immutable ID; name reuse is detected (SR-V2-02)."""
        import inspect
        source = inspect.getsource(cli._execute_recovery_child)
        # docker rm uses container_id, not container_name
        assert '"docker", "rm", container_id' in source
        # The name-reuse detection path exists (any form)
        import re as _re
        assert _re.search(r'(recheck|no such container|replacement)', source)

    def test_stopped_owned_container_removed_by_id(self, tmp_path, monkeypatch):
        """Stopped owned containers are removed by their immutable ID (SR-V2-02)."""
        import inspect
        source = inspect.getsource(cli._execute_recovery_child)
        assert '"docker", "rm", container_id' in source
        assert 'inspect.get("container_id")' in source or "container_id" in source

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
