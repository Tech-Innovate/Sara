import json
from pathlib import Path

import sara.cli as cli
from sara.scraper import expected_resume_input_ids
from sara.storage import connect

from test_recovery_run import run_args
from test_recovery_run_exec import FakeScraper, _install, _records_for_bin


def test_late_command_validation_repairs_running_resume_child(tmp_path, monkeypatch, capsys):
    data, sha, db, _plan, fake = _install(
        monkeypatch, tmp_path, records_for=_records_for_bin
    )

    def partial_scrape(command):
        container = command[command.index("--name") + 1]
        bin_record, _run_id, plan = fake.plans_by_container[container]
        expected = sorted(
            expected_resume_input_ids(
                type("Area", (), {"bbox": bin_record.bbox})(),
                list(plan.queries),
                plan.recovery_cell_km,
            )
        )
        results = Path(FakeScraper._mount_from_command(command)) / "results.jsonl"
        results.write_text("", encoding="utf-8")
        completed = expected[: max(1, len(expected) // 2)]
        Path(str(results) + ".resume.json").write_text(
            json.dumps({"version": 1, "completed_inputs": completed}),
            encoding="utf-8",
        )
        return 0

    monkeypatch.setattr(cli, "run_scraper", partial_scrape)
    assert cli.cmd_recovery_run(run_args(db, data, sha, tmp_path)) == 130
    capsys.readouterr()

    real_validate = cli.ScrapeOptions.validate
    calls = {"n": 0}

    def fail_only_during_command_build(self):
        calls["n"] += 1
        if calls["n"] == 3:
            raise ValueError("synthetic late command validation failure")
        return real_validate(self)

    monkeypatch.setattr(cli.ScrapeOptions, "validate", fail_only_during_command_build)
    monkeypatch.setattr(
        cli,
        "run_scraper",
        lambda command: (_ for _ in ()).throw(AssertionError("scraper must not relaunch")),
    )

    rc = cli.cmd_recovery_run(run_args(db, data, sha, tmp_path))
    assert rc == 1
    assert calls["n"] == 3
    assert "child preparation failed" in capsys.readouterr().err

    conn = connect(db)
    parent = conn.execute(
        "SELECT status, error FROM recovery_executions WHERE plan_sha256 = ?", (sha,)
    ).fetchone()
    child = conn.execute(
        "SELECT status, finished_at, exit_code, error FROM runs WHERE id LIKE 'rr-%'"
    ).fetchone()
    assert parent["status"] == "failed"
    assert "synthetic late command validation failure" in parent["error"]
    assert child["status"] == "failed"
    assert child["finished_at"] is not None
    assert child["exit_code"] == 0
    assert "synthetic late command validation failure" in child["error"]
    conn.close()
