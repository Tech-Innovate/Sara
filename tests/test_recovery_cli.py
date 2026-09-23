import argparse
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

import sara.cli as cli
from sara.cli import main
from sara.storage import connect

BBOX = {"min_lat": 0.0, "min_lon": 0.0, "max_lat": 0.05, "max_lon": 0.05}

DEFAULT_COORDS = [(0.001, 0.001)] * 15 + [(0.03, 0.03)] * 11


def build_db(path, *, run_id="base-run", status="complete",
             finished_at="2026-09-23T00:00:00+00:00", exit_code=0,
             config=None, coords=None, unique_seen=None, drift=False,
             queries='["restaurant"]', cell_km=2.0):
    conn = connect(path)
    if config is None:
        config = dict(DEFAULT_CONFIG)
    if coords is None:
        coords = DEFAULT_COORDS
    conn.execute(
        """
        INSERT INTO runs(id, area_name, bbox_json, cell_km, depth, queries_json,
                         scraper_image, config_json, raw_path, status, started_at,
                         finished_at, exit_code, unique_seen)
        VALUES (?, 'x', ?, ?, 5, ?, 'img', ?, NULL, ?, '2026-09-23T00:00:00+00:00', ?, ?, ?)
        """,
        (
            run_id, json.dumps(BBOX), cell_km, queries,
            json.dumps(config), status, finished_at, exit_code,
            len(coords) if unique_seen is None else unique_seen,
        ),
    )
    if drift:
        conn.execute(
            """
            INSERT INTO runs(id, area_name, bbox_json, cell_km, depth, queries_json,
                             scraper_image, config_json, raw_path, status, started_at,
                             finished_at, exit_code, unique_seen)
            VALUES ('later-run', 'x', ?, 1.0, 5, '["restaurant"]', 'img', NULL, NULL, 'complete',
                    '2026-09-23T01:00:00+00:00', '2026-09-23T02:00:00+00:00', 0, 0)
            """,
            (json.dumps(BBOX),),
        )
    for index, (lat, lon) in enumerate(coords):
        last_run = "later-run" if drift else run_id
        cur = conn.execute(
            """
            INSERT INTO businesses(canonical_key, place_id, title, latitude, longitude,
                                   first_seen_at, last_seen_at, first_run_id, last_run_id, raw_json)
            VALUES (?, ?, ?, ?, ?, 't0', 't0', ?, ?, '{}')
            """,
            (f"place:p{index}", f"p{index}", f"B{index}", lat, lon, run_id, last_run),
        )
        conn.execute(
            "INSERT INTO run_businesses(run_id, business_id, first_observed_at) VALUES (?, ?, 't0')",
            (run_id, cur.lastrowid),
        )
    conn.commit()
    conn.close()
    return path


def plan_args(db_path, output, **overrides):
    values = dict(
        db=str(db_path),
        run_id="base-run",
        recovery_cell_km=1.0,
        tier_a_min=15,
        tier_b_min=11,
        policy_id="pilot-test",
        output=str(output),
    )
    values.update(overrides)
    return argparse.Namespace(**values)


def dump_logical_state(db_path) -> str:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    state = []
    for table in ("runs", "businesses", "run_businesses"):
        rows = [dict(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY 1, 2")]
        state.append((table, rows))
    conn.close()
    return repr(state)


def test_missing_db_is_rejected_and_not_created(tmp_path, capsys):
    db = tmp_path / "no such dir" / "missing.sara.db"
    rc = cli.cmd_recovery_plan(plan_args(db, tmp_path / "plan.json"))
    assert rc == 2
    assert not db.exists()
    assert "does not exist" in capsys.readouterr().err


def test_missing_run_is_rejected(tmp_path, capsys):
    db = build_db(tmp_path / "sara.db", run_id="base-run")
    rc = cli.cmd_recovery_plan(plan_args(db, tmp_path / "plan.json", run_id="other-run"))
    assert rc == 2
    assert "does not exist" in capsys.readouterr().err


def test_noncomplete_run_is_rejected(tmp_path, capsys):
    db = build_db(tmp_path / "sara.db", status="running", finished_at=None)
    rc = cli.cmd_recovery_plan(plan_args(db, tmp_path / "plan.json"))
    assert rc == 2
    assert "not complete" in capsys.readouterr().err


def test_complete_run_with_null_finished_at_is_rejected(tmp_path, capsys):
    db = build_db(tmp_path / "sara.db", finished_at=None)
    rc = cli.cmd_recovery_plan(plan_args(db, tmp_path / "plan.json"))
    assert rc == 2
    assert "finished_at" in capsys.readouterr().err


@pytest.mark.parametrize("config", [
    {"resume": False, "strict_bounds": True},
    {"resume": True, "strict_bounds": False},
    {"resume": True},          # strict_bounds missing
    {"strict_bounds": True},   # resume missing
])
def test_non_resume_or_non_strict_source_is_rejected(tmp_path, capsys, config):
    db = build_db(tmp_path / "sara.db", config=config)
    rc = cli.cmd_recovery_plan(plan_args(db, tmp_path / "plan.json"))
    assert rc == 2


def test_malformed_queries_are_rejected(tmp_path):
    db = build_db(tmp_path / "sara-a.db", queries="[]")
    assert cli.cmd_recovery_plan(plan_args(db, tmp_path / "plan.json")) == 2
    db = build_db(tmp_path / "sara-b.db", queries='{"not": "a list"}')
    assert cli.cmd_recovery_plan(plan_args(db, tmp_path / "plan2.json")) == 2


def test_missing_config_is_rejected(tmp_path):
    conn = connect(build_db(tmp_path / "sara.db"))
    conn.execute("UPDATE runs SET config_json = NULL")
    conn.commit()
    conn.close()
    assert cli.cmd_recovery_plan(plan_args(tmp_path / "sara.db", tmp_path / "plan.json")) == 2


def test_malformed_bbox_is_rejected(tmp_path):
    conn = connect(build_db(tmp_path / "sara.db"))
    conn.execute("UPDATE runs SET bbox_json = '{\"min_lat\": 5}'")
    conn.commit()
    conn.close()
    assert cli.cmd_recovery_plan(plan_args(tmp_path / "sara.db", tmp_path / "plan.json")) == 2


def test_membership_count_mismatch_is_rejected(tmp_path, capsys):
    db = build_db(tmp_path / "sara.db", unique_seen=99)
    rc = cli.cmd_recovery_plan(plan_args(db, tmp_path / "plan.json"))
    assert rc == 2
    assert "unique_seen" in capsys.readouterr().err


@pytest.mark.parametrize("coords", [
    [(0.9, 0.9)],                # outside bbox
    [(None, 0.001)],             # missing latitude
    [(0.001, None)],             # missing longitude
    [(float("inf"), 0.001)],     # nonfinite
])
def test_bad_associated_coordinates_are_rejected(tmp_path, coords):
    db = build_db(tmp_path / "sara.db", coords=coords, unique_seen=1)
    assert cli.cmd_recovery_plan(plan_args(db, tmp_path / "plan.json")) == 2


def test_later_run_coordinate_drift_is_rejected(tmp_path, capsys):
    db = build_db(tmp_path / "sara.db", coords=[(0.001, 0.001)], drift=True, unique_seen=1)
    rc = cli.cmd_recovery_plan(plan_args(db, tmp_path / "plan.json"))
    assert rc == 2
    assert "later run" in capsys.readouterr().err


def test_recovery_cell_must_be_finer(tmp_path, capsys):
    db = build_db(tmp_path / "sara.db")
    rc = cli.cmd_recovery_plan(plan_args(db, tmp_path / "plan.json", recovery_cell_km=2.0))
    assert rc == 2
    assert "strictly finer" in capsys.readouterr().err


def test_invalid_thresholds_are_rejected(tmp_path):
    db = build_db(tmp_path / "sara.db")
    assert cli.cmd_recovery_plan(plan_args(db, tmp_path / "p1.json", tier_a_min=11)) == 2
    assert cli.cmd_recovery_plan(plan_args(db, tmp_path / "p2.json", tier_b_min=0)) == 2
    assert cli.cmd_recovery_plan(plan_args(db, tmp_path / "p3.json", tier_a_min=5, tier_b_min=11)) == 2


def test_valid_source_produces_plan_without_mutating_db(tmp_path, capsys):
    db = build_db(tmp_path / "sara.db")
    before = dump_logical_state(db)
    output = tmp_path / "recovery plan.json"
    rc = cli.cmd_recovery_plan(plan_args(db, output))
    assert rc == 0
    assert output.exists()
    assert dump_logical_state(db) == before
    out_err = capsys.readouterr()
    assert "sha256=" in out_err.out
    assert "search_delta_vs_uniform" in out_err.out


def test_nonzero_exit_code_complete_run_is_accepted(tmp_path):
    # A separately verified re-ingest can finalize complete with nonzero
    # scraper exit-code provenance; recovery-plan must not require exit 0.
    db = build_db(tmp_path / "sara.db", exit_code=1)
    assert cli.cmd_recovery_plan(plan_args(db, tmp_path / "plan.json")) == 0


def test_existing_output_is_never_overwritten(tmp_path):
    db = build_db(tmp_path / "sara.db")
    output = tmp_path / "plan.json"
    output.write_text("sentinel", encoding="utf-8")
    rc = cli.cmd_recovery_plan(plan_args(db, output))
    assert rc == 2
    assert output.read_text(encoding="utf-8") == "sentinel"


def test_same_snapshot_same_args_byte_identical_output(tmp_path):
    db = build_db(tmp_path / "sara.db")
    first = tmp_path / "plan-a.json"
    second = tmp_path / "plan-b.json"
    assert cli.cmd_recovery_plan(plan_args(db, first)) == 0
    assert cli.cmd_recovery_plan(plan_args(db, second)) == 0
    assert first.read_bytes() == second.read_bytes()


def test_payload_contains_no_absolute_paths_or_runtime_metadata(tmp_path):
    db = build_db(tmp_path / "sara db with spaces.sara.db")
    output = tmp_path / "nested" / "plan.json"
    output.parent.mkdir()
    assert cli.cmd_recovery_plan(plan_args(db, output)) == 0
    text = output.read_text(encoding="utf-8")
    assert str(db.resolve()) not in text
    assert str(output.resolve()) not in text
    for banned in ("timestamp", "hostname", "\"pid\"", "uuid"):
        assert banned not in text.lower()


def test_printed_hash_matches_file_bytes(tmp_path, capsys):
    db = build_db(tmp_path / "sara.db")
    output = tmp_path / "plan.json"
    assert cli.cmd_recovery_plan(plan_args(db, output)) == 0
    captured = capsys.readouterr().out
    printed = [line for line in captured.splitlines() if line.startswith("sha256=")][0]
    digest = printed.split("=", 1)[1].strip()
    assert digest == hashlib.sha256(output.read_bytes()).hexdigest()


def test_uri_sensitive_db_paths_are_handled(tmp_path):
    name = "weird #100% db.sara.db"
    db = build_db(tmp_path / name)
    assert cli.cmd_recovery_plan(plan_args(db, tmp_path / "plan.json")) == 0


def test_scraper_and_docker_paths_are_never_reached(tmp_path, monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("planner must never invoke scraper/docker/ingest paths")

    monkeypatch.setattr(cli, "run_scraper", forbidden)
    monkeypatch.setattr(cli, "build_docker_command", forbidden)
    monkeypatch.setattr(cli, "ingest_records", forbidden)
    monkeypatch.setattr(cli, "write_query_snapshot", forbidden)
    db = build_db(tmp_path / "sara.db")
    assert cli.cmd_recovery_plan(plan_args(db, tmp_path / "plan.json")) == 0


def test_readonly_connection_rejects_missing_db_without_creating(tmp_path):
    from sara.storage import connect_readonly

    missing = tmp_path / "gone.db"
    with pytest.raises(FileNotFoundError):
        connect_readonly(missing)
    assert not missing.exists()


def test_readonly_connection_blocks_writes(tmp_path):
    from sara.storage import connect_readonly

    db = build_db(tmp_path / "sara.db")
    conn = connect_readonly(db)
    try:
        with pytest.raises(sqlite3.Error):
            conn.execute("DELETE FROM businesses")
    finally:
        conn.close()


def test_main_dispatch_exit_codes(tmp_path):
    db = build_db(tmp_path / "sara.db")
    output = tmp_path / "plan.json"
    rc = main([
        "--db", str(db), "recovery-plan",
        "--run-id", "base-run",
        "--recovery-cell-km", "1.0",
        "--tier-a-min", "15",
        "--tier-b-min", "11",
        "--policy-id", "pilot-test",
        "--output", str(output),
    ])
    assert rc == 0
    assert output.exists()
    rc = main([
        "--db", str(db), "recovery-plan",
        "--run-id", "missing-run",
        "--recovery-cell-km", "1.0",
        "--tier-a-min", "15",
        "--tier-b-min", "11",
        "--policy-id", "pilot-test",
        "--output", str(tmp_path / "plan2.json"),
    ])
    assert rc == 2


DEFAULT_CONFIG = {
    "resume": True,
    "strict_bounds": True,
    "area_name": "x",
    "bbox": BBOX,
    "queries": ["restaurant"],
    "cell_km": 2.0,
    "depth": 5,
    "image": "img",
    "lang": "en",
    "zoom": 15,
}


def _rewrite_config(db_path, mutate):
    conn = connect(db_path)
    config = json.loads(conn.execute("SELECT config_json FROM runs").fetchone()[0])
    mutate(config)
    conn.execute("UPDATE runs SET config_json = ?", (json.dumps(config),))
    conn.commit()
    conn.close()


def test_output_opened_in_binary_mode_when_platform_supports_it(tmp_path, monkeypatch):
    import os as os_mod

    db = build_db(tmp_path / "sara.db")
    captured = {}
    real_open = os_mod.open

    def spy_open(path, flags, mode=0o777, *args, **kwargs):
        captured["flags"] = flags
        return real_open(path, flags, mode, *args, **kwargs)

    monkeypatch.setattr(cli.os, "open", spy_open)
    assert cli.cmd_recovery_plan(plan_args(db, tmp_path / "plan.json")) == 0
    if hasattr(os_mod, "O_BINARY"):
        assert captured["flags"] & os_mod.O_BINARY


def test_write_failure_cleans_up_partial_output(tmp_path, monkeypatch):
    db = build_db(tmp_path / "sara.db")
    output = tmp_path / "plan.json"

    def failing_write(_fd, _view):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(cli.os, "write", failing_write)
    rc = cli.cmd_recovery_plan(plan_args(db, output))
    assert rc == 1
    assert not output.exists()


def test_zero_progress_write_is_an_error_not_a_loop(tmp_path, monkeypatch):
    db = build_db(tmp_path / "sara.db")
    output = tmp_path / "plan.json"
    monkeypatch.setattr(cli.os, "write", lambda _fd, _view: 0)
    rc = cli.cmd_recovery_plan(plan_args(db, output))
    assert rc == 1
    assert not output.exists()


def test_close_failure_cleans_up_partial_output(tmp_path, monkeypatch):
    import os as os_mod

    db = build_db(tmp_path / "sara.db")
    output = tmp_path / "plan.json"
    real_close = os_mod.close

    def failing_close(fd):
        real_close(fd)
        raise OSError("simulated close failure")

    monkeypatch.setattr(cli.os, "close", failing_close)
    rc = cli.cmd_recovery_plan(plan_args(db, output))
    assert rc == 1
    assert not output.exists()


def test_connection_open_failure_returns_one(tmp_path, monkeypatch, capsys):
    db = build_db(tmp_path / "sara.db")

    def broken(_path):
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(cli, "connect_readonly", broken)
    rc = cli.cmd_recovery_plan(plan_args(db, tmp_path / "plan.json"))
    assert rc == 1
    assert "failed to open database" in capsys.readouterr().err


@pytest.mark.parametrize("column", ["bbox_json", "queries_json", "config_json"])
def test_blob_typed_columns_are_rejected(tmp_path, column):
    db = build_db(tmp_path / "sara.db")
    conn = connect(db)
    conn.execute(f"UPDATE runs SET {column} = ?", (b'{"x": 1}',))
    conn.commit()
    conn.close()
    assert cli.cmd_recovery_plan(plan_args(db, tmp_path / "plan.json")) == 2


@pytest.mark.parametrize("changes", [
    {"cell_km": 9.9},
    {"depth": 3},
    {"image": "other-image"},
    {"area_name": "elsewhere"},
    {"queries": ["cafe"]},
    {"bbox": {"min_lat": 0.0, "min_lon": 0.0, "max_lat": 9.0, "max_lon": 9.0}},
])
def test_config_column_disagreement_is_rejected(tmp_path, changes):
    db = build_db(tmp_path / "sara.db")
    _rewrite_config(db, lambda config: config.update(changes))
    assert cli.cmd_recovery_plan(plan_args(db, tmp_path / "plan.json")) == 2


def test_config_missing_duplicated_field_is_rejected(tmp_path):
    db = build_db(tmp_path / "sara.db")

    def drop(config):
        del config["cell_km"]

    _rewrite_config(db, drop)
    assert cli.cmd_recovery_plan(plan_args(db, tmp_path / "plan.json")) == 2


def test_source_config_preserved_in_artifact(tmp_path):
    db = build_db(tmp_path / "sara.db")
    output = tmp_path / "plan.json"
    assert cli.cmd_recovery_plan(plan_args(db, output)) == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    config = payload["source_run"]["config"]
    assert config["lang"] == "en"
    assert config["zoom"] == 15
    assert config["cell_km"] == 2.0
    assert config["bbox"] == BBOX
    assert config["queries"] == ["restaurant"]


def test_completion_claim_constant_used_not_duplicated():
    source = Path(cli.__file__).read_text(encoding="utf-8")
    assert "recorded_complete_not_reverified" not in source
    assert "COMPLETION_CLAIM" in source


# ---- v3 review regressions (V2-F01..F04, V2-SR01) ----


def test_unknown_config_fields_omitted_from_artifact(tmp_path):
    db = build_db(tmp_path / "sara.db")
    _rewrite_config(
        db,
        lambda config: config.update(
            {"secret_token": "hunter2", "proxy_url": "http://user:pass@host:8080"}
        ),
    )
    output = tmp_path / "plan.json"
    assert cli.cmd_recovery_plan(plan_args(db, output)) == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    config = payload["source_run"]["config"]
    assert "secret_token" not in config
    assert "proxy_url" not in config
    assert set(config) <= set(
        payload["source_run"]["config"]
    )
    assert payload["source_run"]["config_sha256"]
    known = {
        "area_name", "bbox", "queries", "cell_km", "depth", "concurrency",
        "browser_pool_size", "pages_per_browser", "lang", "zoom", "resume",
        "image", "proxy_sha256", "strict_bounds",
    }
    assert set(config) <= known


def test_huge_json_integer_in_config_is_rejected_not_traceback(tmp_path):
    db = build_db(tmp_path / "sara.db")
    _rewrite_config(db, lambda config: config.update({"cell_km": 10 ** 400}))
    rc = cli.cmd_recovery_plan(plan_args(db, tmp_path / "plan.json"))
    assert rc == 2


def test_huge_json_integer_in_bbox_config_is_rejected(tmp_path):
    db = build_db(tmp_path / "sara.db")
    _rewrite_config(
        db,
        lambda config: config.update(
            {"bbox": {"min_lat": 10 ** 400, "min_lon": 0.0, "max_lat": 0.05, "max_lon": 0.05}}
        ),
    )
    assert cli.cmd_recovery_plan(plan_args(db, tmp_path / "plan.json")) == 2


def test_nan_config_constant_is_rejected(tmp_path):
    db = build_db(tmp_path / "sara.db")
    conn = connect(db)
    raw = conn.execute("SELECT config_json FROM runs").fetchone()[0]
    poisoned = raw.replace('"en"', "NaN", 1)
    assert poisoned != raw
    conn.execute("UPDATE runs SET config_json = ?", (poisoned,))
    conn.commit()
    conn.close()
    assert cli.cmd_recovery_plan(plan_args(db, tmp_path / "plan.json")) == 2


def test_nan_bbox_constant_is_rejected(tmp_path):
    db = build_db(tmp_path / "sara.db")
    conn = connect(db)
    poisoned = json.dumps(BBOX).replace("0.05", "NaN", 1)
    conn.execute("UPDATE runs SET bbox_json = ?", (poisoned,))
    conn.commit()
    conn.close()
    assert cli.cmd_recovery_plan(plan_args(db, tmp_path / "plan.json")) == 2


@pytest.mark.parametrize("column,value", [
    ("started_at", b"t"),
    ("finished_at", b"t"),
    ("exit_code", b"1"),
    ("exit_code", "zero"),
    ("started_at", ""),
])
def test_malformed_source_scalars_are_rejected(tmp_path, column, value):
    db = build_db(tmp_path / "sara.db")
    conn = connect(db)
    conn.execute(f"UPDATE runs SET {column} = ?", (value,))
    conn.commit()
    conn.close()
    assert cli.cmd_recovery_plan(plan_args(db, tmp_path / "plan.json")) == 2


def test_plan_output_contains_no_nonstandard_json_constants(tmp_path):
    db = build_db(tmp_path / "sara.db")
    output = tmp_path / "plan.json"
    assert cli.cmd_recovery_plan(plan_args(db, output)) == 0

    def reject(token):
        raise AssertionError(f"non-standard JSON constant in output: {token}")

    json.loads(output.read_text(encoding="utf-8"), parse_constant=reject)
