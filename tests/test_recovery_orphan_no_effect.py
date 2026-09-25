import pytest

import sara.cli as cli
from sara.recovery import parse_execution_plan
from sara.storage import connect

from test_recovery_run import make_plan, run_args


@pytest.mark.parametrize("orphan_name", ["results.jsonl", "results.jsonl.resume.json"])
def test_unstarted_orphan_rejection_preserves_query_snapshot(
    tmp_path, monkeypatch, capsys, orphan_name
):
    data, sha, db = make_plan(tmp_path)
    plan = parse_execution_plan(data)
    bin_record = sorted(plan.selected_bins, key=lambda b: (b.row, b.column))[0]
    bin_dir = (
        tmp_path
        / "recovery"
        / sha
        / "bins"
        / f"r{bin_record.row}-c{bin_record.column}"
    )
    bin_dir.mkdir(parents=True)

    snapshot = bin_dir / "queries.txt"
    original_snapshot = b"operator-owned-mismatch\n"
    snapshot.write_bytes(original_snapshot)

    orphan = bin_dir / orphan_name
    original_orphan = b"orphan-evidence\n"
    orphan.write_bytes(original_orphan)

    monkeypatch.setattr(cli, "_docker_inspect_container", lambda _name: None)

    def should_not_launch(_command):
        raise AssertionError("orphan rejection must occur before scraper launch")

    monkeypatch.setattr(cli, "run_scraper", should_not_launch)

    rc = cli.cmd_recovery_run(run_args(db, data, sha, tmp_path))

    assert rc == 2
    assert snapshot.read_bytes() == original_snapshot
    assert orphan.read_bytes() == original_orphan
    assert "refusing to adopt untracked scraper evidence" in capsys.readouterr().err

    conn = connect(db)
    parent = conn.execute(
        "SELECT status FROM recovery_executions WHERE plan_sha256 = ?", (sha,)
    ).fetchone()
    assert parent["status"] == "interrupted"
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM runs WHERE id LIKE 'rr-%'"
    ).fetchone()["n"] == 0
    conn.close()
