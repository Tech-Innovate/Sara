import sara.cli as cli
from sara.storage import connect
from test_recovery_run import run_args
from test_recovery_run_exec import _install, _records_for_bin


def _finish_execution(monkeypatch, tmp_path):
    data, sha, db, plan, _fake = _install(
        monkeypatch, tmp_path, records_for=_records_for_bin
    )
    assert cli.cmd_recovery_run(run_args(db, data, sha, tmp_path)) == 0
    return data, sha, db, plan


def _assert_complete_parent(db, sha):
    conn = connect(db)
    try:
        parent = conn.execute(
            "SELECT status FROM recovery_executions WHERE plan_sha256 = ?",
            (sha,),
        ).fetchone()
        assert parent["status"] == "complete"
        children = conn.execute(
            "SELECT r.status FROM runs r JOIN recovery_execution_bins b "
            "ON b.run_id = r.id WHERE b.plan_sha256 = ? AND b.run_id IS NOT NULL",
            (sha,),
        ).fetchall()
        assert children and all(child["status"] == "complete" for child in children)
    finally:
        conn.close()


def test_complete_replay_missing_mapping_after_registration_rejected(
    tmp_path, monkeypatch, capsys
):
    data, sha, db, plan = _finish_execution(monkeypatch, tmp_path)
    capsys.readouterr()  # isolate replay reporting
    target = sorted(plan.selected_bins, key=lambda b: (b.row, b.column))[0]
    real_register = cli.register_recovery_execution

    def register_then_delete(conn, **kwargs):
        state = real_register(conn, **kwargs)
        assert state == "complete"
        conn.execute(
            "DELETE FROM recovery_execution_bins WHERE plan_sha256 = ? "
            "AND row = ? AND column = ?",
            (kwargs["plan_sha256"], target.row, target.column),
        )
        return state

    monkeypatch.setattr(cli, "register_recovery_execution", register_then_delete)
    rc = cli.cmd_recovery_run(run_args(db, data, sha, tmp_path))
    captured = capsys.readouterr()

    assert rc == 2
    assert "stored complete execution is inconsistent" in captured.err
    assert "complete parent mapping set does not match the plan bins" in captured.err
    assert "recovery-run already complete" not in captured.out
    _assert_complete_parent(db, sha)
    conn = connect(db)
    try:
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM recovery_execution_bins WHERE plan_sha256 = ?",
            (sha,),
        ).fetchone()["n"]
        assert count == len(plan.selected_bins) - 1
    finally:
        conn.close()


def test_complete_replay_extra_mapping_after_registration_rejected(
    tmp_path, monkeypatch, capsys
):
    data, sha, db, plan = _finish_execution(monkeypatch, tmp_path)
    capsys.readouterr()  # isolate replay reporting
    real_register = cli.register_recovery_execution

    def register_then_insert(conn, **kwargs):
        state = real_register(conn, **kwargs)
        assert state == "complete"
        conn.execute(
            "INSERT INTO recovery_execution_bins("
            "plan_sha256, row, column, tier, bbox_json, planned_searches, "
            "run_id, container_name) VALUES (?, 999999, 999999, 'A', '{}', 0, NULL, ?)",
            (kwargs["plan_sha256"], "unexpected-complete-replay-bin"),
        )
        return state

    monkeypatch.setattr(cli, "register_recovery_execution", register_then_insert)
    rc = cli.cmd_recovery_run(run_args(db, data, sha, tmp_path))
    captured = capsys.readouterr()

    assert rc == 2
    assert "stored complete execution is inconsistent" in captured.err
    assert "complete parent mapping set does not match the plan bins" in captured.err
    assert "recovery-run already complete" not in captured.out
    _assert_complete_parent(db, sha)
    conn = connect(db)
    try:
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM recovery_execution_bins WHERE plan_sha256 = ?",
            (sha,),
        ).fetchone()["n"]
        assert count == len(plan.selected_bins) + 1
    finally:
        conn.close()
