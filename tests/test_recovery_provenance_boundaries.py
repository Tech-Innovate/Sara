import sara.cli as cli
from sara.storage import connect

from test_recovery_run import run_args
from test_recovery_run_exec import _install, _records_for_bin


def _completed_execution_as_running(tmp_path, monkeypatch):
    data, sha, db, plan, _fake = _install(
        monkeypatch, tmp_path, records_for=_records_for_bin
    )
    assert cli.cmd_recovery_run(run_args(db, data, sha, tmp_path)) == 0

    conn = connect(db)
    conn.execute(
        "UPDATE recovery_executions "
        "SET status = 'running', finished_at = NULL, error = NULL, result_json = NULL "
        "WHERE plan_sha256 = ?",
        (sha,),
    )
    conn.commit()
    conn.close()
    return data, sha, db, plan


def test_malformed_mapping_bbox_is_bounded_provenance_rejection(
    tmp_path, monkeypatch, capsys
):
    data, sha, db, plan = _completed_execution_as_running(tmp_path, monkeypatch)
    capsys.readouterr()

    real_register = cli.register_recovery_execution
    first = sorted(plan.selected_bins, key=lambda b: (b.row, b.column))[0]

    def corrupt_after_registration(conn, **kwargs):
        state = real_register(conn, **kwargs)
        conn.execute(
            "UPDATE recovery_execution_bins SET bbox_json = ? "
            "WHERE plan_sha256 = ? AND row = ? AND column = ?",
            ("not-json", sha, first.row, first.column),
        )
        return state

    monkeypatch.setattr(cli, "register_recovery_execution", corrupt_after_registration)

    rc = cli.cmd_recovery_run(run_args(db, data, sha, tmp_path))
    assert rc == 2
    assert "bbox disagrees with the plan" in capsys.readouterr().err

    conn = connect(db)
    parent = conn.execute(
        "SELECT status, error FROM recovery_executions WHERE plan_sha256 = ?", (sha,)
    ).fetchone()
    assert parent["status"] == "failed"
    assert "bbox disagrees with the plan" in parent["error"]
    children = conn.execute(
        "SELECT status FROM runs WHERE id LIKE 'rr-%' ORDER BY id"
    ).fetchall()
    assert children and all(row["status"] == "complete" for row in children)
    conn.close()


def test_malformed_child_cell_km_is_bounded_provenance_rejection(
    tmp_path, monkeypatch, capsys
):
    data, sha, db, _plan = _completed_execution_as_running(tmp_path, monkeypatch)
    capsys.readouterr()

    real_register = cli.register_recovery_execution

    def corrupt_after_registration(conn, **kwargs):
        state = real_register(conn, **kwargs)
        child = conn.execute(
            "SELECT run_id FROM recovery_execution_bins "
            "WHERE plan_sha256 = ? AND run_id IS NOT NULL ORDER BY row, column LIMIT 1",
            (sha,),
        ).fetchone()
        conn.execute(
            "UPDATE runs SET cell_km = 'not-a-number' WHERE id = ?",
            (child["run_id"],),
        )
        return state

    monkeypatch.setattr(cli, "register_recovery_execution", corrupt_after_registration)

    rc = cli.cmd_recovery_run(run_args(db, data, sha, tmp_path))
    assert rc == 2
    assert "has invalid cell_km" in capsys.readouterr().err

    conn = connect(db)
    parent = conn.execute(
        "SELECT status, error FROM recovery_executions WHERE plan_sha256 = ?", (sha,)
    ).fetchone()
    assert parent["status"] == "failed"
    assert "has invalid cell_km" in parent["error"]
    children = conn.execute(
        "SELECT status FROM runs WHERE id LIKE 'rr-%' ORDER BY id"
    ).fetchall()
    assert children and all(row["status"] == "complete" for row in children)
    conn.close()


def test_keyboardinterrupt_during_registration_is_clean_pre_effect_130(
    tmp_path, monkeypatch, capsys
):
    data, sha, db, _plan, _fake = _install(
        monkeypatch, tmp_path, records_for=_records_for_bin
    )
    capsys.readouterr()

    def interrupted_registration(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "register_recovery_execution", interrupted_registration)

    rc = cli.cmd_recovery_run(run_args(db, data, sha, tmp_path))
    assert rc == 130
    assert "interrupted during registration" in capsys.readouterr().err
    assert not (tmp_path / "recovery" / sha / ".sara-recovery.lock").exists()

    conn = connect(db)
    table = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'recovery_executions'"
    ).fetchone()
    if table is not None:
        assert conn.execute("SELECT COUNT(*) AS n FROM recovery_executions").fetchone()["n"] == 0
    conn.close()
