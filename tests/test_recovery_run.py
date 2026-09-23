import argparse
import hashlib
import json
import sqlite3

import pytest

import sara.cli as cli
from sara.recovery import (
    PlanRejected,
    build_bin_edges,
    parse_execution_plan,
    validate_child_coordinate_precision,
)
from sara.config import BoundingBox
from sara.storage import (
    RecoverySchemaError,
    connect,
    connect_existing,
    ensure_recovery_schema,
    set_recovery_fault_hook,
    clear_recovery_fault_hook,
    verify_recovery_schema,
)

DIGEST_IMAGE = "reg.example/scrap@sha256:" + "a" * 64
BBOX = {"min_lat": 0.0, "min_lon": 0.0, "max_lat": 0.04, "max_lon": 0.04}
# 2 km source grid over this box at equator: 2 rows x 2 columns
COORDS = [(0.001, 0.001)] * 3 + [(0.03, 0.03)] * 2


def build_db(path, *, image=DIGEST_IMAGE, coords=None, queries='["restaurant"]',
             proxy_sha=None, cell_km=2.0):
    from sara.storage import connect as storage_connect

    conn = storage_connect(path)
    config = {
        "resume": True, "strict_bounds": True, "area_name": "x",
        "bbox": BBOX, "queries": json.loads(queries), "cell_km": cell_km,
        "depth": 5, "image": image, "lang": "en", "zoom": 15,
        "concurrency": 1, "browser_pool_size": 1, "pages_per_browser": 1,
        "proxy_sha256": proxy_sha,
    }
    conn.execute(
        """
        INSERT INTO runs(id, area_name, bbox_json, cell_km, depth, queries_json,
                         scraper_image, config_json, raw_path, status, started_at,
                         finished_at, exit_code, unique_seen)
        VALUES ('src-run', 'x', ?, ?, 5, ?, ?, ?, NULL, 'complete',
                '2026-09-23T00:00:00+00:00', '2026-09-23T00:20:00+00:00', 0, ?)
        """,
        (
            json.dumps(BBOX), cell_km, queries, image,
            json.dumps(config, sort_keys=True), len(coords or COORDS),
        ),
    )
    for index, (lat, lon) in enumerate(coords or COORDS):
        cur = conn.execute(
            """
            INSERT INTO businesses(canonical_key, place_id, title, latitude, longitude,
                                   first_seen_at, last_seen_at, first_run_id, last_run_id, raw_json)
            VALUES (?, ?, ?, ?, ?, 't0', 't0', 'src-run', 'src-run', '{}')
            """,
            (f"place:p{index}", f"p{index}", f"B{index}", lat, lon),
        )
        conn.execute(
            "INSERT INTO run_businesses(run_id, business_id, first_observed_at) VALUES ('src-run', ?, 't0')",
            (cur.lastrowid,),
        )
    conn.commit()
    conn.close()
    return path


def make_plan(tmp_path, *, db=None, tier_a=2, tier_b=1, recovery_cell=1.0, queries=None):
    """Generate a genuinely valid executable plan through the real planner."""
    db = db or build_db(tmp_path / "sara.db")
    area = tmp_path / "area.json"
    area.write_text(json.dumps({"name": "x", "bbox": BBOX}), encoding="utf-8")
    qfile = tmp_path / "queries.txt"
    qfile.write_text("restaurant\n", encoding="utf-8")
    counter = getattr(make_plan, "_counter", 0) + 1
    make_plan._counter = counter
    output = tmp_path / f"plan-{counter}.json"
    rc = cli.main([
        "--db", str(db), "recovery-plan",
        "--run-id", "src-run",
        "--recovery-cell-km", str(recovery_cell),
        "--tier-a-min", str(tier_a),
        "--tier-b-min", str(tier_b),
        "--policy-id", "rr-test",
        "--output", str(output),
    ])
    assert rc == 0
    data = output.read_bytes()
    return data, hashlib.sha256(data).hexdigest(), db


def run_args(db, plan_bytes, sha, tmp_path, **overrides):
    plan_file = tmp_path / "plan.json"
    plan_file.write_bytes(plan_bytes)
    values = dict(
        db=str(db),
        plan=str(plan_file),
        plan_sha256=sha,
        expected_searches=8,
        output_dir=str(tmp_path / "recovery"),
        proxy_file=None,
        dry_run=False,
    )
    values.update(overrides)
    return argparse.Namespace(**values)


def tamper(plan_bytes, mutate):
    payload = json.loads(plan_bytes.decode("utf-8"))
    mutate(payload)
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n"
    return data, hashlib.sha256(data).hexdigest()


class TestStrictParser:
    def test_valid_plan_parses(self, tmp_path):
        data, sha, _ = make_plan(tmp_path)
        plan = parse_execution_plan(data)
        assert plan.source_run_id == "src-run"
        assert len(plan.selected_bins) == 2
        assert plan.targeted_searches == 8

    def test_duplicate_top_level_key_rejected(self, tmp_path):
        data, _, _ = make_plan(tmp_path)
        text = data.decode("utf-8")
        text = text.replace('"kind": "sara.recovery_plan",', '"kind": "sara.recovery_plan", "kind": "sara.recovery_plan",', 1)
        assert '"kind"' in text
        with pytest.raises(PlanRejected, match="duplicate JSON object key"):
            parse_execution_plan(text.encode("utf-8"))

    def test_duplicate_nested_key_rejected(self, tmp_path):
        data, _, _ = make_plan(tmp_path)

        def mutate(payload):
            payload["policy"]["extra_dup"] = 1
            # force duplicate via serialization trick not possible with dict;
            # instead verify summary-level duplicate detection using raw text
        # dictionaries cannot hold duplicates; use raw text injection
        text = data.decode("utf-8")
        text = text.replace('"id": "rr-test",', '"id": "rr-test", "id": "rr-test",', 1)
        with pytest.raises(PlanRejected, match="duplicate JSON object key"):
            parse_execution_plan(text.encode("utf-8"))

    def test_nan_rejected(self, tmp_path):
        data, _, _ = make_plan(tmp_path)
        text = data.decode("utf-8").replace('"tier_a_min": 2', '"tier_a_min": NaN', 1)
        with pytest.raises(PlanRejected, match="non-standard JSON constant"):
            parse_execution_plan(text.encode("utf-8"))

    @pytest.mark.parametrize("mutate,match", [
        (lambda p: p["summary"].__setitem__("estimated_recovery_searches", 99), "estimated_recovery_searches"),
        (lambda p: p["bins"][0].__setitem__("business_count", 0), "selected|tier"),
        (lambda p: p["bins"][0]["bbox"].__setitem__("max_lat", 0.123), "bbox does not match"),
        (lambda p: p["summary"].__setitem__("search_delta_vs_uniform", 0), "search_delta_vs_uniform"),
        (lambda p: p["source_grid_estimate"].__setitem__("planned_searches", 42), "source_grid_estimate"),
        (lambda p: p["recovery"]["full_uniform_estimate"].__setitem__("cells", 42), "full_uniform_estimate"),
        (lambda p: p["binning"].__setitem__("associated_businesses", 42), "associated_businesses"),
        (lambda p: (p["bins"][1].__setitem__("selected", False), p["summary"].__setitem__("selected_bins", 1), p["summary"].__setitem__("tier_b_bins", 0), p["summary"].__setitem__("unselected_bins", 3), p["summary"].__setitem__("estimated_recovery_searches", 4), p["summary"].__setitem__("search_delta_vs_uniform", -12)), "selected"),
    ])
    def test_tampered_derived_fields_rejected(self, tmp_path, mutate, match):
        data, _, _ = make_plan(tmp_path)
        tampered, _ = tamper(data, mutate)
        with pytest.raises(PlanRejected, match=match):
            parse_execution_plan(tampered)

    def test_bool_as_number_rejected(self, tmp_path):
        data, _, _ = make_plan(tmp_path)
        text = data.decode("utf-8").replace('"tier_a_min": 2', '"tier_a_min": true', 1)
        with pytest.raises(PlanRejected, match="integer"):
            parse_execution_plan(text.encode("utf-8"))

    def test_wrong_kind_rejected(self, tmp_path):
        data, _, _ = make_plan(tmp_path)

        def mutate(payload):
            payload["kind"] = "other.kind"
        tampered, _ = tamper(data, mutate)
        with pytest.raises(PlanRejected, match="kind"):
            parse_execution_plan(tampered)

    def test_extra_field_rejected(self, tmp_path):
        data, _, _ = make_plan(tmp_path)

        def mutate(payload):
            payload["surprise"] = 1
        tampered, _ = tamper(data, mutate)
        with pytest.raises(PlanRejected, match="wrong field set"):
            parse_execution_plan(tampered)


class TestPrecisionGuard:
    def test_step_below_boundary_rejected(self):
        # latitude step ~ cell/111.32; 5e-5 km -> ~4.5e-7 deg < 1e-6
        bbox = BoundingBox(0.0, 0.0, 0.04, 0.04)
        with pytest.raises(PlanRejected, match="six-decimal"):
            validate_child_coordinate_precision(bbox, 5e-5)

    def test_step_above_boundary_accepted(self):
        bbox = BoundingBox(0.0, 0.0, 0.04, 0.04)
        validate_child_coordinate_precision(bbox, 2e-4)
        # single-origin grid (zero span in both axes) cannot collide
        tiny = BoundingBox(0.0, 0.0, 1e-9, 1e-9)
        validate_child_coordinate_precision(tiny, 5e-5)


class TestSchemaAndVerifier:
    def test_pre_feature_db_migrates(self, tmp_path):
        db = build_db(tmp_path / "sara.db")
        conn = connect_existing(db)
        conn.execute("BEGIN IMMEDIATE")
        ensure_recovery_schema(conn)
        verify_recovery_schema(conn)
        conn.commit()
        conn.close()

    def _break_schema(self, tmp_path, ddl):
        db = build_db(tmp_path / "sara.db")
        conn = sqlite3.connect(db)
        conn.executescript(ddl)
        conn.commit()
        conn.close()
        return db

    def test_incompatible_parent_table_rejected(self, tmp_path):
        db = self._break_schema(
            tmp_path,
            "CREATE TABLE recovery_executions (plan_sha256 TEXT PRIMARY KEY, junk INTEGER);",
        )
        conn = connect_existing(db)
        conn.execute("BEGIN IMMEDIATE")
        ensure_recovery_schema(conn)
        with pytest.raises(RecoverySchemaError):
            verify_recovery_schema(conn)
        conn.rollback()
        conn.close()

    def test_missing_run_id_unique_rejected(self, tmp_path):
        db = self._break_schema(
            tmp_path,
            """
            CREATE TABLE recovery_executions (
                plan_sha256 TEXT PRIMARY KEY, source_run_id TEXT NOT NULL,
                plan_schema_version INTEGER NOT NULL, plan_kind TEXT NOT NULL,
                policy_id TEXT NOT NULL, output_root TEXT NOT NULL,
                plan_snapshot_path TEXT NOT NULL, selected_bins INTEGER NOT NULL,
                planned_searches INTEGER NOT NULL, status TEXT NOT NULL,
                started_at TEXT NOT NULL, finished_at TEXT, error TEXT, result_json TEXT,
                FOREIGN KEY(source_run_id) REFERENCES runs(id));
            CREATE TABLE recovery_execution_bins (
                plan_sha256 TEXT NOT NULL, row INTEGER NOT NULL, column INTEGER NOT NULL,
                tier TEXT NOT NULL, bbox_json TEXT NOT NULL, planned_searches INTEGER NOT NULL,
                run_id TEXT, container_name TEXT NOT NULL UNIQUE,
                PRIMARY KEY(plan_sha256, row, column),
                FOREIGN KEY(plan_sha256) REFERENCES recovery_executions(plan_sha256) ON DELETE CASCADE,
                FOREIGN KEY(run_id) REFERENCES runs(id));
            """,
        )
        conn = connect_existing(db)
        conn.execute("BEGIN IMMEDIATE")
        ensure_recovery_schema(conn)
        with pytest.raises(RecoverySchemaError, match="run_id"):
            verify_recovery_schema(conn)
        conn.rollback()
        conn.close()

    def test_missing_fk_rejected(self, tmp_path):
        db = self._break_schema(
            tmp_path,
            """
            CREATE TABLE recovery_executions (
                plan_sha256 TEXT PRIMARY KEY, source_run_id TEXT NOT NULL,
                plan_schema_version INTEGER NOT NULL, plan_kind TEXT NOT NULL,
                policy_id TEXT NOT NULL, output_root TEXT NOT NULL,
                plan_snapshot_path TEXT NOT NULL, selected_bins INTEGER NOT NULL,
                planned_searches INTEGER NOT NULL, status TEXT NOT NULL,
                started_at TEXT NOT NULL, finished_at TEXT, error TEXT, result_json TEXT);
            CREATE TABLE recovery_execution_bins (
                plan_sha256 TEXT NOT NULL, row INTEGER NOT NULL, column INTEGER NOT NULL,
                tier TEXT NOT NULL, bbox_json TEXT NOT NULL, planned_searches INTEGER NOT NULL,
                run_id TEXT UNIQUE, container_name TEXT NOT NULL UNIQUE,
                PRIMARY KEY(plan_sha256, row, column));
            """,
        )
        conn = connect_existing(db)
        conn.execute("BEGIN IMMEDIATE")
        ensure_recovery_schema(conn)
        with pytest.raises(RecoverySchemaError, match="foreign key"):
            verify_recovery_schema(conn)
        conn.rollback()
        conn.close()


class TestQueryValidation:
    def test_embedded_lf_rejected(self, tmp_path):
        data, sha, db = make_plan(tmp_path)

        def mutate(payload):
            payload["source_run"]["queries"] = ["rest\naurant"]
            payload["source_run"]["queries"] = ["rest\naurant"]
        tampered, tsha = tamper(data, lambda p: (
            p["source_run"].__setitem__("queries", ["rest\naurant"]),
            p["source_run"]["config"].__setitem__("queries", ["rest\naurant"]),
        ))
        args = run_args(db, tampered, tsha, tmp_path)
        rc = cli.cmd_recovery_run(args)
        assert rc == 2

    def test_embedded_cr_rejected(self, tmp_path):
        data, sha, db = make_plan(tmp_path)
        tampered, tsha = tamper(data, lambda p: (
            p["source_run"].__setitem__("queries", ["rest\raurant"]),
            p["source_run"]["config"].__setitem__("queries", ["rest\raurant"]),
        ))
        assert cli.cmd_recovery_run(run_args(db, tampered, tsha, tmp_path)) == 2

    def test_duplicate_resume_identity_rejected(self, tmp_path):
        data, sha, db = make_plan(tmp_path)
        tampered, tsha = tamper(data, lambda p: (
            p["source_run"].__setitem__("queries", ["restaurant", "restaurant"]),
            p["source_run"]["config"].__setitem__("queries", ["restaurant", "restaurant"]),
            p["source_run"].__setitem__("query_count", 2),
        ))
        assert cli.cmd_recovery_run(run_args(db, tampered, tsha, tmp_path)) == 2


class TestAuthorityGates:
    def test_wrong_hash_rejected(self, tmp_path):
        data, sha, db = make_plan(tmp_path)
        assert cli.cmd_recovery_run(run_args(db, data, "0" * 64, tmp_path)) == 2

    def test_wrong_expected_searches_rejected(self, tmp_path):
        data, sha, db = make_plan(tmp_path)
        assert cli.cmd_recovery_run(run_args(db, data, sha, tmp_path, expected_searches=7)) == 2

    def test_tag_image_rejected(self, tmp_path):
        db = build_db(tmp_path / "sara.db", image="gosom/google-maps-scraper:v1.18.1")
        data, sha, _ = make_plan(tmp_path, db=db)
        assert cli.cmd_recovery_run(run_args(db, data, sha, tmp_path)) == 2

    def test_missing_db_rejected_not_created(self, tmp_path):
        data, sha, _ = make_plan(tmp_path)
        missing = tmp_path / "no-db" / "x.db"
        assert cli.cmd_recovery_run(run_args(missing, data, sha, tmp_path)) == 2
        assert not missing.exists()

    def test_zero_selected_plan_noop(self, tmp_path):
        data, sha, db = make_plan(tmp_path, tier_a=99, tier_b=98)
        recovery_root = tmp_path / "recovery"
        rc = cli.cmd_recovery_run(run_args(db, data, sha, tmp_path, expected_searches=0))
        assert rc == 0
        assert not recovery_root.exists()

    def test_proxy_combinations(self, tmp_path):
        proxy = tmp_path / "proxies.txt"
        proxy.write_text("socks5://1.2.3.4:1080\n", encoding="utf-8")
        proxy_sha = hashlib.sha256(proxy.read_bytes()).hexdigest()
        db = build_db(tmp_path / "sara.db", proxy_sha=proxy_sha)
        data, sha, _ = make_plan(tmp_path, db=db)
        wrong = tmp_path / "wrong.txt"
        wrong.write_text("different\n", encoding="utf-8")

        # no file supplied though plan requires one
        assert cli.cmd_recovery_run(run_args(db, data, sha, tmp_path)) == 2
        # wrong hash
        assert cli.cmd_recovery_run(run_args(db, data, sha, tmp_path, proxy_file=str(wrong))) == 2

        # proxy-free plan + supplied file -> rejected
        db2 = build_db(tmp_path / "sara2.db")
        data2, sha2, _ = make_plan(tmp_path, db=db2)
        assert cli.cmd_recovery_run(run_args(db2, data2, sha2, tmp_path, proxy_file=str(proxy))) == 2


class TestSourceBinding:
    def test_missing_source_run_rejected(self, tmp_path):
        data, sha, db = make_plan(tmp_path)
        conn = connect(db)
        conn.execute("DELETE FROM run_businesses")
        conn.execute("DELETE FROM businesses")
        conn.execute("DELETE FROM runs")
        conn.commit()
        conn.close()
        assert cli.cmd_recovery_run(run_args(db, data, sha, tmp_path)) == 2

    def test_toctou_between_preflight_and_registration(self, tmp_path):
        data, sha, db = make_plan(tmp_path)
        real_connect = cli.connect_readonly

        def mutate_then_preflight(path):
            conn = real_connect(path)
            original = cli.connect_existing

            def swapped(p):
                # mutate the source row right when the write connection opens
                conn2 = sqlite3.connect(p)
                conn2.execute("UPDATE runs SET area_name = 'tampered' WHERE id = 'src-run'")
                conn2.commit()
                conn2.close()
                return original(p)
            cli.connect_existing = swapped
            return conn

        cli.connect_readonly = mutate_then_preflight
        try:
            assert cli.cmd_recovery_run(run_args(db, data, sha, tmp_path)) == 2
        finally:
            cli.connect_readonly = real_connect
            cli.connect_existing = __import__("sara.storage", fromlist=["connect_existing"]).connect_existing
        conn = sqlite3.connect(db)
        rows = conn.execute("SELECT COUNT(*) FROM recovery_executions").fetchone()[0] \
            if conn.execute("SELECT name FROM sqlite_master WHERE name='recovery_executions'").fetchone() else 0
        conn.close()
        assert rows == 0


class TestDryRun:
    def test_dry_run_no_side_effects(self, tmp_path, monkeypatch):
        data, sha, db = make_plan(tmp_path)
        called = []
        monkeypatch.setattr(cli, "run_scraper", lambda c: called.append(c) or 1)
        monkeypatch.setattr(cli, "connect_existing", lambda p: (_ for _ in ()).throw(AssertionError("write connector used")))
        monkeypatch.setattr(cli, "_docker_inspect_container", lambda n: (_ for _ in ()).throw(AssertionError("docker used")))
        snapshot = {"sha": hashlib.sha256(db.read_bytes()).hexdigest()}

        rc = cli.cmd_recovery_run(run_args(db, data, sha, tmp_path, dry_run=True))
        assert rc == 0
        assert not called
        assert not (tmp_path / "recovery").exists()
        assert hashlib.sha256(db.read_bytes()).hexdigest() == snapshot["sha"]
        # pre-feature DB: recovery tables were never created by dry-run
        conn = sqlite3.connect(db)
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name IN ('recovery_executions','recovery_execution_bins')"
        ).fetchone()[0] == 0
        conn.close()

    def test_dry_run_deterministic(self, tmp_path, capsys):
        data, sha, db = make_plan(tmp_path)
        capsys.readouterr()  # discard planner output
        first = cli.cmd_recovery_run(run_args(db, data, sha, tmp_path, dry_run=True))
        out1 = capsys.readouterr().out
        second = cli.cmd_recovery_run(run_args(db, data, sha, tmp_path, dry_run=True))
        out2 = capsys.readouterr().out
        assert first == second == 0
        assert out1 == out2
        assert "execution_history=not_checked" in out1


class TestRegistrationAtomicity:
    def test_fault_after_parent_insert_rolls_back(self, tmp_path):
        data, sha, db = make_plan(tmp_path)
        set_recovery_fault_hook("after_parent_insert", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        try:
            rc = cli.cmd_recovery_run(run_args(db, data, sha, tmp_path))
            assert rc == 1
        finally:
            clear_recovery_fault_hook("after_parent_insert")
        conn = sqlite3.connect(db)
        def count(table):
            exists = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE name = ?", (table,)
            ).fetchone()[0]
            return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] if exists else 0
        assert count("recovery_executions") == 0
        assert count("recovery_execution_bins") == 0
        conn.close()

    def test_fault_after_mapping_inserts_rolls_back(self, tmp_path):
        data, sha, db = make_plan(tmp_path)
        set_recovery_fault_hook("after_mapping_inserts", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        try:
            rc = cli.cmd_recovery_run(run_args(db, data, sha, tmp_path))
            assert rc == 1
        finally:
            clear_recovery_fault_hook("after_mapping_inserts")
        conn = sqlite3.connect(db)
        def count(table):
            exists = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE name = ?", (table,)
            ).fetchone()[0]
            return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] if exists else 0
        assert count("recovery_executions") == 0
        assert count("recovery_execution_bins") == 0
        conn.close()
