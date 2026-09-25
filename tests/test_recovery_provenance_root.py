import pytest

import sara.cli as cli
from sara.recovery import PlanRejected
from sara.storage import connect

from test_recovery_run import run_args
from test_recovery_run_exec import _install, _records_for_bin


def test_child_provenance_uses_persisted_output_root(tmp_path, monkeypatch):
    data, sha, db, plan, _fake = _install(
        monkeypatch, tmp_path, records_for=_records_for_bin
    )
    assert cli.cmd_recovery_run(run_args(db, data, sha, tmp_path)) == 0

    assert not hasattr(cli, "_MAPPING_ROOT_HOLDER")
    assert not hasattr(cli, "mapping_root")

    conn = connect(db)
    mapping = conn.execute(
        "SELECT b.row, b.column, b.tier, b.bbox_json, b.planned_searches, "
        "b.run_id, b.container_name, e.output_root "
        "FROM recovery_execution_bins AS b "
        "JOIN recovery_executions AS e ON e.plan_sha256 = b.plan_sha256 "
        "WHERE b.plan_sha256 = ? ORDER BY b.row, b.column LIMIT 1",
        (sha,),
    ).fetchone()
    container_names = {
        (bin_record.row, bin_record.column): cli._recovery_container_name(
            cli._recovery_child_run_id(sha, bin_record.row, bin_record.column)
        )
        for bin_record in plan.selected_bins
    }

    child = cli._validate_child_provenance(
        conn, plan, sha, mapping, container_names
    )
    assert child["status"] == "complete"

    wrong_root = dict(mapping)
    wrong_root["output_root"] = str(tmp_path / "moved-execution-root")
    with pytest.raises(PlanRejected, match="raw_path"):
        cli._validate_child_provenance(
            conn, plan, sha, wrong_root, container_names
        )
    conn.close()
