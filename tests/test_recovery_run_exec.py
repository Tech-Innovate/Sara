import argparse
import hashlib
import json
import sqlite3

import pytest

import sara.cli as cli
from sara.config import BoundingBox
from sara.scraper import expected_resume_input_ids, recovery_query_snapshot_bytes
from sara.storage import connect

from test_recovery_run import DIGEST_IMAGE, BBOX, COORDS, build_db, make_plan, run_args


def _fake_scraper_ok(command, *, area_of, results_writer):
    """Build a run_scraper replacement completing one bin via its docker args."""
    raise NotImplementedError


class FakeScraper:
    """Simulates the pinned scraper: writes sidecar + results, returns 0."""

    def __init__(self, plans_by_container, records_by_run):
        self.launches = []
        self.plans_by_container = plans_by_container  # container -> (bin, run_id, plan)
        self.records_by_run = records_by_run          # run_id -> list[dict]

    def __call__(self, command):
        self.launches.append(list(command))
        container = command[command.index("--name") + 1]
        bin_record, run_id, plan = self.plans_by_container[container]
        area = type("A", (), {"bbox": bin_record.bbox})()
        expected = expected_resume_input_ids(
            type("Area", (), {"bbox": bin_record.bbox})(), list(plan.queries), plan.recovery_cell_km
        )
        # locate the output dir from the bind mount argument
        mount = next(arg for arg in command if arg.startswith("/") and arg.endswith(":/out"))
        out_dir = mount[: -len(":/out")]
        from pathlib import Path
        results = Path(out_dir) / "results.jsonl"
        with open(results, "w", encoding="utf-8") as handle:
            for record in self.records_by_run[run_id]:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        sidecar = Path(str(results) + ".resume.json")
        sidecar.write_text(
            json.dumps({"version": 1, "completed_inputs": sorted(expected)}),
            encoding="utf-8",
        )
        return 0


def _install(monkeypatch, tmp_path, *, records_for, inspect=lambda name: None):
    data, sha, db = make_plan(tmp_path)
    from sara.recovery import parse_execution_plan
    plan = parse_execution_plan(data)
    plan_sha = sha
    from sara.cli import _recovery_child_run_id, _recovery_container_name
    plans_by_container = {}
    records_by_run = {}
    for bin_record in plan.selected_bins:
        run_id = _recovery_child_run_id(plan_sha, bin_record.row, bin_record.column)
        container = _recovery_container_name(run_id)
        plans_by_container[container] = (bin_record, run_id, plan)
        records_by_run[run_id] = records_for(bin_record, run_id)
    fake = FakeScraper(plans_by_container, records_by_run)
    monkeypatch.setattr(cli, "run_scraper", fake)
    monkeypatch.setattr(cli, "_docker_inspect_container", lambda name: inspect(name))
    return data, sha, db, plan, fake


def _records_for_bin(bin_record, run_id):
    """One record per bin inside its bbox, plus a boundary-shared record."""
    lat = (bin_record.bbox.min_lat + bin_record.bbox.max_lat) / 2
    lon = (bin_record.bbox.min_lon + bin_record.bbox.max_lon) / 2
    records = [
        {"place_id": f"{run_id}-a", "title": "A", "latitude": lat, "longitude": lon},
        # sits exactly on the shared north/east edge: accepted by strict bounds
        # of this bin and of its neighbour when both run
        {"place_id": "shared-edge", "title": "S",
         "latitude": bin_record.bbox.max_lat, "longitude": bin_record.bbox.max_lon},
    ]
    return records


class TestExecutionLifecycle:
    def test_full_execution_completes_with_metrics(self, tmp_path, monkeypatch, capsys):
        data, sha, db, plan, fake = _install(
            monkeypatch, tmp_path, records_for=_records_for_bin
        )
        rc = cli.cmd_recovery_run(run_args(db, data, sha, tmp_path))
        assert rc == 0
        assert len(fake.launches) == len(plan.selected_bins)

        conn = connect(db)
        parent = conn.execute(
            "SELECT * FROM recovery_executions WHERE plan_sha256 = ?", (sha,)
        ).fetchone()
        assert parent["status"] == "complete"
        assert parent["result_json"] is not None
        result = json.loads(parent["result_json"])
        assert result["child_runs"] == len(plan.selected_bins)
        # each bin contributed 2 accepted records; the shared-edge business
        # appears in both children but counts once in DISTINCT metrics
        assert result["unique_recovery_seen"] == 3  # a, b, shared
        assert result["source_overlap_businesses"] == 0
        assert result["source_increment_businesses"] == 3
        assert result["globally_new_businesses"] == 3
        assert result["source_membership_at_plan_count"] == 5
        assert result["source_membership_current_count"] == 5
        snapshot = (tmp_path / "recovery" / sha / "recovery-plan.json").read_bytes()
        assert hashlib.sha256(snapshot).hexdigest() == sha
        out = capsys.readouterr().out
        assert "recovery complete" in out
        conn.close()

    def test_duplicate_complete_invocation_reprints_and_rejects(self, tmp_path, monkeypatch, capsys):
        data, sha, db, plan, fake = _install(
            monkeypatch, tmp_path, records_for=_records_for_bin
        )
        assert cli.cmd_recovery_run(run_args(db, data, sha, tmp_path)) == 0
        launches = len(fake.launches)
        capsys.readouterr()
        rc = cli.cmd_recovery_run(run_args(db, data, sha, tmp_path))
        assert rc == 2
        assert len(fake.launches) == launches  # no second Docker round
        assert "already complete" in capsys.readouterr().out

    def test_scraper_nonzero_marks_failed_exit_1(self, tmp_path, monkeypatch):
        data, sha, db, plan, fake = _install(
            monkeypatch, tmp_path, records_for=_records_for_bin
        )

        def failing(command):
            return 2

        monkeypatch.setattr(cli, "run_scraper", failing)
        rc = cli.cmd_recovery_run(run_args(db, data, sha, tmp_path))
        assert rc == 1
        conn = connect(db)
        parent = conn.execute(
            "SELECT status FROM recovery_executions WHERE plan_sha256 = ?", (sha,)
        ).fetchone()
        assert parent["status"] == "failed"
        child = conn.execute(
            "SELECT status, exit_code FROM runs WHERE id LIKE 'rr-%'"
        ).fetchone()
        assert child["status"] == "failed"
        assert child["exit_code"] == 2
        conn.close()

    def test_keyboardinterrupt_returns_130(self, tmp_path, monkeypatch):
        data, sha, db, plan, fake = _install(
            monkeypatch, tmp_path, records_for=_records_for_bin
        )

        def interrupted(command):
            raise KeyboardInterrupt

        monkeypatch.setattr(cli, "run_scraper", interrupted)
        rc = cli.cmd_recovery_run(run_args(db, data, sha, tmp_path))
        assert rc == 130
        conn = connect(db)
        parent = conn.execute(
            "SELECT status FROM recovery_executions WHERE plan_sha256 = ?", (sha,)
        ).fetchone()
        assert parent["status"] == "interrupted"
        conn.close()

    def test_missing_ids_interrupt_130_no_ingest(self, tmp_path, monkeypatch):
        data, sha, db, plan, fake = _install(
            monkeypatch, tmp_path, records_for=_records_for_bin
        )
        original = fake

        def partial(command):
            # complete only half the expected inputs
            container = command[command.index("--name") + 1]
            bin_record, run_id, plan_obj = original.plans_by_container[container]
            from sara.scraper import expected_resume_input_ids
            expected = expected_resume_input_ids(
                type("Area", (), {"bbox": bin_record.bbox})(),
                list(plan_obj.queries), plan_obj.recovery_cell_km,
            )
            half = sorted(expected)[: len(expected) // 2]
            mount = next(a for a in command if a.startswith("/") and a.endswith(":/out"))
            from pathlib import Path
            results = Path(mount[: -len(":/out")]) / "results.jsonl"
            with open(results, "w", encoding="utf-8") as handle:
                handle.write(json.dumps({"place_id": "x", "latitude": 0.01, "longitude": 0.01}) + "\n")
            Path(str(results) + ".resume.json").write_text(
                json.dumps({"version": 1, "completed_inputs": half}), encoding="utf-8"
            )
            return 0

        monkeypatch.setattr(cli, "run_scraper", partial)
        rc = cli.cmd_recovery_run(run_args(db, data, sha, tmp_path))
        assert rc == 130
        conn = connect(db)
        child = conn.execute("SELECT status, unique_seen FROM runs WHERE id LIKE 'rr-%'").fetchone()
        assert child["status"] == "interrupted"
        assert child["unique_seen"] == 0
        conn.close()

    def test_unexpected_ids_fail_1_no_ingest(self, tmp_path, monkeypatch):
        data, sha, db, plan, fake = _install(
            monkeypatch, tmp_path, records_for=_records_for_bin
        )

        def poisoned(command):
            container = command[command.index("--name") + 1]
            bin_record, run_id, plan_obj = fake.plans_by_container[container]
            from sara.scraper import expected_resume_input_ids
            expected = expected_resume_input_ids(
                type("Area", (), {"bbox": bin_record.bbox})(),
                list(plan_obj.queries), plan_obj.recovery_cell_km,
            )
            ids = sorted(expected) + ["resume:" + "f" * 64]
            mount = next(a for a in command if a.startswith("/") and a.endswith(":/out"))
            from pathlib import Path
            results = Path(mount[: -len(":/out")]) / "results.jsonl"
            results.write_text("", encoding="utf-8")
            Path(str(results) + ".resume.json").write_text(
                json.dumps({"version": 1, "completed_inputs": ids}), encoding="utf-8"
            )
            return 0

        monkeypatch.setattr(cli, "run_scraper", poisoned)
        rc = cli.cmd_recovery_run(run_args(db, data, sha, tmp_path))
        assert rc == 1
        conn = connect(db)
        child = conn.execute("SELECT status, unique_seen FROM runs WHERE id LIKE 'rr-%'").fetchone()
        assert child["status"] == "failed"
        assert child["unique_seen"] == 0
        conn.close()

    def test_ingestion_exception_rolls_back(self, tmp_path, monkeypatch):
        data, sha, db, plan, fake = _install(
            monkeypatch, tmp_path, records_for=_records_for_bin
        )

        def broken_ingest(*args, **kwargs):
            raise RuntimeError("ingest exploded")

        monkeypatch.setattr(cli, "ingest_records", broken_ingest)
        rc = cli.cmd_recovery_run(run_args(db, data, sha, tmp_path))
        assert rc == 1
        conn = connect(db)
        child = conn.execute("SELECT status FROM runs WHERE id LIKE 'rr-%'").fetchone()
        assert child["status"] == "failed"
        canonical = conn.execute("SELECT COUNT(*) FROM businesses WHERE first_run_id LIKE 'rr-%'").fetchone()[0]
        assert canonical == 0
        conn.close()

    def test_first_child_survives_second_failure(self, tmp_path, monkeypatch):
        data, sha, db, plan, fake = _install(
            monkeypatch, tmp_path, records_for=_records_for_bin
        )
        state = {"launches": 0}

        def fail_second(command):
            state["launches"] += 1
            if state["launches"] == 1:
                return fake(command)
            return 1

        monkeypatch.setattr(cli, "run_scraper", fail_second)
        rc = cli.cmd_recovery_run(run_args(db, data, sha, tmp_path))
        assert rc == 1
        conn = connect(db)
        statuses = dict(
            conn.execute(
                "SELECT r.id, r.status FROM runs r JOIN recovery_execution_bins b "
                "ON b.run_id = r.id WHERE b.plan_sha256 = ?", (sha,)
            ).fetchall()
        )
        assert sorted(statuses.values()) == ["complete", "failed"]
        conn.close()

    def test_active_container_blocks_relaunch(self, tmp_path, monkeypatch, capsys):
        holder = {}

        def matching_running(name):
            return {
                "running": True,
                "labels": {
                    "sara.recovery.plan_sha256": holder["sha"],
                    "sara.recovery.run_id": name[len("sara-rr-"):],
                },
            }
        data, sha, db, plan, fake = _install(
            monkeypatch, tmp_path, records_for=_records_for_bin,
            inspect=matching_running,
        )
        holder["sha"] = sha
        rc = cli.cmd_recovery_run(run_args(db, data, sha, tmp_path))
        assert rc == 2
        assert fake.launches == []
        assert "still active" in capsys.readouterr().err

    def test_wrong_label_container_rejected(self, tmp_path, monkeypatch, capsys):
        data, sha, db, plan, fake = _install(
            monkeypatch, tmp_path, records_for=_records_for_bin,
            inspect=lambda name: {"running": False, "labels": {"sara.recovery.plan_sha256": "other"}},
        )
        rc = cli.cmd_recovery_run(run_args(db, data, sha, tmp_path))
        assert rc == 2
        assert fake.launches == []
        assert "wrong ownership labels" in capsys.readouterr().err


class TestResumeSemantics:
    def _run_first_child_then_crash(self, tmp_path, monkeypatch):
        data, sha, db, plan, fake = _install(
            monkeypatch, tmp_path, records_for=_records_for_bin
        )
        state = {"launches": 0}

        def crash_after_first(command):
            state["launches"] += 1
            if state["launches"] == 1:
                fake(command)
                raise KeyboardInterrupt
            return fake(command)

        monkeypatch.setattr(cli, "run_scraper", crash_after_first)
        rc = cli.cmd_recovery_run(run_args(db, data, sha, tmp_path))
        assert rc == 130
        return data, sha, db, plan, fake, state

    def test_resume_skips_complete_child(self, tmp_path, monkeypatch):
        data, sha, db, plan, fake, state = self._run_first_child_then_crash(tmp_path, monkeypatch)
        monkeypatch.setattr(cli, "run_scraper", fake)
        monkeypatch.setattr(cli, "_docker_inspect_container", lambda name: None)
        rc = cli.cmd_recovery_run(run_args(db, data, sha, tmp_path))
        assert rc == 0
        # one launch before the crash + exactly one after: the completed
        # first child was skipped rather than re-executed
        assert len(fake.launches) == 2
        conn = connect(db)
        parent = conn.execute(
            "SELECT status FROM recovery_executions WHERE plan_sha256 = ?", (sha,)
        ).fetchone()
        assert parent["status"] == "complete"
        conn.close()

    def test_complete_sidecar_on_retry_ingests_without_docker(self, tmp_path, monkeypatch):
        data, sha, db, plan, fake = _install(
            monkeypatch, tmp_path, records_for=_records_for_bin
        )
        # First invocation: scraper writes evidence but the process "crashes"
        # after acquisition; simulate by completing then raising.
        state = {"launches": 0}

        def crash_after_evidence(command):
            state["launches"] += 1
            if state["launches"] == 1:
                fake(command)
                raise KeyboardInterrupt
            return fake(command)

        monkeypatch.setattr(cli, "run_scraper", crash_after_evidence)
        assert cli.cmd_recovery_run(run_args(db, data, sha, tmp_path)) == 130
        # Child is interrupted with complete evidence on disk.

        # Now the retry: the first child's evidence is complete, so it must
        # ingest directly without a second Docker launch; only the second
        # child launches. Intercept launches to prove the first container is
        # never started again.
        first_container = fake.launches[0][fake.launches[0].index("--name") + 1]
        relaunched = []

        def tracking_scraper(command):
            relaunched.append(command[command.index("--name") + 1])
            return fake(command)

        monkeypatch.setattr(cli, "run_scraper", tracking_scraper)
        monkeypatch.setattr(cli, "_docker_inspect_container", lambda name: None)
        rc = cli.cmd_recovery_run(run_args(db, data, sha, tmp_path))
        assert rc == 0
        assert first_container not in relaunched
        conn = connect(db)
        parent = conn.execute(
            "SELECT status FROM recovery_executions WHERE plan_sha256 = ?", (sha,)
        ).fetchone()
        assert parent["status"] == "complete"
        conn.close()


class TestSnapshotRecovery:
    def test_truncated_snapshot_repaired_before_any_child(self, tmp_path, monkeypatch):
        data, sha, db, plan, fake = _install(
            monkeypatch, tmp_path, records_for=_records_for_bin
        )
        # Pre-register by a crashed first invocation that wrote a truncated snapshot
        root = tmp_path / "recovery" / sha
        root.mkdir(parents=True)
        (root / "recovery-plan.json").write_bytes(b"{trunc")
        monkeypatch.setattr(cli, "_docker_inspect_container", lambda name: None)
        rc = cli.cmd_recovery_run(run_args(db, data, sha, tmp_path))
        assert rc == 0
        assert (root / "recovery-plan.json").read_bytes() == data

    def test_mismatched_snapshot_after_child_start_fails_closed(self, tmp_path, monkeypatch):
        data, sha, db, plan, fake = _install(
            monkeypatch, tmp_path, records_for=_records_for_bin
        )
        assert cli.cmd_recovery_run(run_args(db, data, sha, tmp_path)) == 0
        root = tmp_path / "recovery" / sha
        (root / "recovery-plan.json").write_bytes(b"tampered")
        monkeypatch.setattr(cli, "run_scraper", lambda c: (_ for _ in ()).throw(AssertionError("no relaunch")))
        monkeypatch.setattr(cli, "_docker_inspect_container", lambda name: None)
        rc = cli.cmd_recovery_run(run_args(db, data, sha, tmp_path))
        assert rc == 2


class TestSourceDrift:
    def test_membership_drift_changes_increment_semantics(self, tmp_path, monkeypatch):
        data, sha, db, plan, fake = _install(
            monkeypatch, tmp_path, records_for=_records_for_bin
        )
        # A later overlapping run adds a recovery-discovered business to the
        # SOURCE run's membership after planning but before metrics.
        conn = connect(db)

        def records_with_drift(bin_record, run_id):
            records = _records_for_bin(bin_record, run_id)
            return records

        rc = cli.cmd_recovery_run(run_args(db, data, sha, tmp_path))
        assert rc == 0
        parent = conn.execute(
            "SELECT result_json FROM recovery_executions WHERE plan_sha256 = ?", (sha,)
        ).fetchone()
        result = json.loads(parent["result_json"])
        assert result["source_membership_at_plan_count"] == 5
        assert result["source_membership_current_count"] == 5
        conn.close()
