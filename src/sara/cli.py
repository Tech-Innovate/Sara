from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import socket
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path

from .config import AreaConfig, BoundingBox, load_area, load_queries, write_query_snapshot
from .grid import estimate_grid
from .scraper import (
    DEFAULT_IMAGE,
    ScrapeOptions,
    build_docker_command,
    command_for_display,
    expected_resume_input_ids,
    load_resume_completed_input_ids,
    recovery_query_snapshot_bytes,
    run_scraper,
    validate_resume_query_identities,
    validate_recovery_queries,
)
from .recovery import (
    COMPLETION_CLAIM,
    PlanRejected,
    RecoveryPolicy,
    build_recovery_plan,
    parse_execution_plan,
    project_source_config,
    serialize_recovery_plan,
    validate_child_coordinate_precision,
    validate_digest_pinned_image,
)
from .storage import (
    RecoverySchemaError,
    SourceRunRejected,
    bind_plan_to_source,
    connect,
    connect_existing,
    connect_readonly,
    create_recovery_child_run,
    ensure_recovery_schema,
    finalize_recovery_execution,
    ingest_records,
    iter_jsonl,
    load_recovery_source_run,
    register_recovery_execution,
    resume_recovery_parent,
    set_recovery_parent_status,
    utc_now,
    verify_recovery_schema,
)

_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sara", description="Local coverage-oriented Google Maps collector")
    parser.add_argument("--db", default="data/sara.db", help="SQLite database path")
    sub = parser.add_subparsers(dest="command", required=True)

    plan = sub.add_parser("plan", help="Estimate grid size and search count")
    plan.add_argument("--area", required=True)
    plan.add_argument("--queries", required=True)
    plan.add_argument("--cell-km", type=float, default=1.0)

    collect = sub.add_parser("collect", help="Run the pinned local scraper and ingest JSONL")
    collect.add_argument("--area", required=True)
    collect.add_argument("--run-id", help="Reuse a failed/interrupted run ID so upstream resume state is preserved")
    collect.add_argument("--queries", required=True)
    collect.add_argument("--cell-km", type=float, default=1.0)
    collect.add_argument("--depth", type=_positive_int, default=5)
    collect.add_argument("--concurrency", type=_positive_int, default=4)
    collect.add_argument("--browser-pool-size", type=_positive_int, default=1)
    collect.add_argument("--pages-per-browser", type=_positive_int, default=4)
    collect.add_argument("--lang", default="en")
    collect.add_argument("--zoom", type=int, default=15)
    collect.add_argument("--image", default=DEFAULT_IMAGE)
    collect.add_argument("--proxy-file")
    collect.add_argument("--output-dir", default="output")
    collect.add_argument("--no-resume", action="store_true")
    collect.add_argument(
        "--include-out-of-bounds",
        action="store_true",
        help="retain results outside the configured bounding box in canonical storage",
    )
    collect.add_argument("--dry-run", action="store_true")

    ingest = sub.add_parser("ingest", help="Re-ingest the recorded JSONL file for an existing run")
    ingest.add_argument("--run-id", required=True)
    ingest.add_argument("--file", required=True)

    stats = sub.add_parser("stats", help="Show run and canonical business counts")
    stats.add_argument("--limit", type=_positive_int, default=20)

    recovery_plan = sub.add_parser(
        "recovery-plan",
        help="Plan finer-grid recovery from a completed run without executing anything",
    )
    recovery_plan.add_argument("--run-id", required=True)
    recovery_plan.add_argument("--recovery-cell-km", type=float, required=True)
    recovery_plan.add_argument("--tier-a-min", type=int, required=True)
    recovery_plan.add_argument("--tier-b-min", type=int, required=True)
    recovery_plan.add_argument("--policy-id", required=True)
    recovery_plan.add_argument("--output", required=True)

    recovery_run = sub.add_parser(
        "recovery-run",
        help="Execute one frozen recovery plan as independent resumable child runs",
    )
    recovery_run.add_argument("--plan", required=True)
    recovery_run.add_argument("--plan-sha256", required=True)
    recovery_run.add_argument("--expected-searches", type=int, required=True)
    recovery_run.add_argument("--output-dir", required=True)
    recovery_run.add_argument("--proxy-file")
    recovery_run.add_argument("--dry-run", action="store_true")
    return parser


def _reject_json_constant(token: str):
    # NaN/Infinity/-Infinity are accepted by Python's json.loads by default
    # but are not standard JSON; source state containing them is rejected.
    raise ValueError(f"non-standard JSON constant {token!r} in recorded run state")


def _validate_run_id(run_id: str) -> str:
    if not _RUN_ID_RE.fullmatch(run_id):
        raise ValueError("run_id must be 1-64 characters using only letters, digits, '.', '_' or '-'")
    return run_id


def _file_sha256(path: Path | None) -> str | None:
    if path is None:
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run_config_json(*, area, queries: list[str], options: ScrapeOptions, strict_bounds: bool) -> str:
    payload = {
        "area_name": area.name,
        "bbox": area.bbox.__dict__,
        "queries": queries,
        "cell_km": options.cell_km,
        "depth": options.depth,
        "concurrency": options.concurrency,
        "browser_pool_size": options.browser_pool_size,
        "pages_per_browser": options.pages_per_browser,
        "lang": options.lang,
        "zoom": options.zoom,
        "resume": options.resume,
        "image": options.image,
        "proxy_sha256": _file_sha256(options.proxy_file),
        "strict_bounds": strict_bounds,
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _ensure_run(
    conn,
    *,
    run_id: str,
    area,
    queries,
    cell_km: float,
    depth: int,
    image: str,
    raw_path: str,
    config_json: str,
    status: str,
):
    bbox_json = json.dumps(area.bbox.__dict__, sort_keys=True)
    queries_json = json.dumps(queries, ensure_ascii=False)
    existing = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    if existing is None:
        conn.execute(
            """
            INSERT INTO runs(
                id, area_name, bbox_json, cell_km, depth, queries_json, scraper_image,
                config_json, raw_path, status, started_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id, area.name, bbox_json, cell_km, depth, queries_json, image,
                config_json, raw_path, status, utc_now(),
            ),
        )
    else:
        expected = (area.name, bbox_json, cell_km, depth, queries_json, image, config_json, raw_path)
        actual = tuple(
            existing[key]
            for key in (
                "area_name", "bbox_json", "cell_km", "depth", "queries_json",
                "scraper_image", "config_json", "raw_path",
            )
        )
        if actual != expected:
            raise ValueError(f"run_id {run_id} exists with different crawl configuration")
        conn.execute(
            "UPDATE runs SET status = ?, finished_at = NULL, exit_code = NULL, error = NULL WHERE id = ?",
            (status, run_id),
        )
    conn.commit()


def _mark_run(conn, run_id: str, *, status: str, exit_code: int | None, error: str | None) -> None:
    conn.execute(
        "UPDATE runs SET status = ?, finished_at = ?, exit_code = ?, error = ? WHERE id = ?",
        (status, utc_now(), exit_code, error, run_id),
    )
    conn.commit()


def _pid_alive_windows(pid: int) -> bool:
    import ctypes
    from ctypes import wintypes

    synchronize = 0x00100000
    wait_object_0 = 0x00000000
    wait_timeout = 0x00000102
    error_invalid_parameter = 87

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.OpenProcess(synchronize, False, pid)
    if not handle:
        return ctypes.get_last_error() != error_invalid_parameter

    try:
        result = kernel32.WaitForSingleObject(handle, 0)
        if result == wait_object_0:
            return False
        if result == wait_timeout:
            return True
        return True
    finally:
        kernel32.CloseHandle(handle)


def _pid_alive(pid: int, *, platform: str | None = None) -> bool:
    if pid <= 0:
        return False
    platform = os.name if platform is None else platform
    if platform == "nt":
        return _pid_alive_windows(pid)
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


class RunLock:
    def __init__(self, path: Path):
        self.path = path
        self.token = uuid.uuid4().hex
        self.acquired = False

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "token": self.token,
            "created_at": utc_now(),
        }
        encoded = json.dumps(payload, sort_keys=True).encode("utf-8")

        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                try:
                    existing = json.loads(self.path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    raise RuntimeError(f"run lock exists and cannot be verified: {self.path}") from exc
                if existing.get("host") == socket.gethostname() and not _pid_alive(int(existing.get("pid", 0))):
                    self.path.unlink(missing_ok=True)
                    continue
                raise RuntimeError(
                    f"run is already locked by pid={existing.get('pid')} host={existing.get('host')}"
                )
            else:
                try:
                    os.write(fd, encoded)
                finally:
                    os.close(fd)
                self.acquired = True
                return
        raise RuntimeError(f"could not acquire run lock: {self.path}")

    def release(self) -> None:
        if not self.acquired:
            return
        try:
            existing = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if existing.get("token") == self.token:
            self.path.unlink(missing_ok=True)
        self.acquired = False


def _print_stats(stats) -> None:
    print(
        " ".join(
            (
                f"raw={stats.raw_records}",
                f"accepted={stats.accepted_records}",
                f"out_of_bounds={stats.out_of_bounds_records}",
                f"unlocated={stats.unlocated_records}",
                f"unidentified={stats.unidentified_records}",
                f"unique_seen={stats.unique_seen}",
                f"new_businesses={stats.new_businesses}",
            )
        )
    )


def _report_stats(stats, *, operation: str) -> int:
    """Report committed results without letting presentation mutate lifecycle state."""
    try:
        _print_stats(stats)
    except KeyboardInterrupt:
        print(f"{operation} completed; reporting interrupted", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"{operation} completed but reporting failed: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_plan(args) -> int:
    area = load_area(args.area)
    queries = load_queries(args.queries)
    estimate = estimate_grid(area.bbox, args.cell_km, len(queries))
    print(json.dumps({
        "area": area.name,
        "queries": len(queries),
        "cell_km": args.cell_km,
        "rows": estimate.rows,
        "columns": estimate.columns,
        "cells": estimate.cells,
        "planned_searches": estimate.searches,
    }, indent=2))
    return 0


def cmd_collect(args) -> int:
    area = load_area(args.area)
    queries = load_queries(args.queries)
    run_id = _validate_run_id(args.run_id or uuid.uuid4().hex[:12])
    output_dir = (Path(args.output_dir) / run_id).resolve()
    output_file = output_dir / "results.jsonl"
    query_snapshot = output_dir / "queries.txt"
    options = ScrapeOptions(
        cell_km=args.cell_km,
        depth=args.depth,
        concurrency=args.concurrency,
        browser_pool_size=args.browser_pool_size,
        pages_per_browser=args.pages_per_browser,
        lang=args.lang,
        zoom=args.zoom,
        resume=not args.no_resume,
        image=args.image,
        proxy_file=Path(args.proxy_file).resolve() if args.proxy_file else None,
    )
    options.validate()
    if not options.resume:
        print(
            "collection requires resume mode because upstream exit code 0 does not prove crawl completion",
            file=sys.stderr,
        )
        return 2

    strict_bounds = not args.include_out_of_bounds
    config_json = _run_config_json(area=area, queries=queries, options=options, strict_bounds=strict_bounds)
    estimate = estimate_grid(area.bbox, args.cell_km, len(queries))
    print(f"run_id={run_id} cells={estimate.cells} planned_searches={estimate.searches}")
    if estimate.cells == 0:
        print("grid produced 0 cells; check bounding box and cell size", file=sys.stderr)
        return 2

    if args.dry_run:
        command = build_docker_command(
            area=area,
            queries_file=query_snapshot,
            output_file=output_file,
            options=options,
            prepare_paths=False,
        )
        print(command_for_display(command))
        return 0

    expected_inputs = expected_resume_input_ids(area, queries, options.cell_km)
    if len(expected_inputs) != estimate.searches:
        raise RuntimeError(
            f"internal grid mismatch: planner expects {estimate.searches} searches but completion model expects {len(expected_inputs)}"
        )

    lock = RunLock(output_dir / ".sara.lock")
    try:
        lock.acquire()
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    try:
        conn = connect(args.db)
        existing = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if existing is not None and existing["status"] == "complete":
            print(f"run_id {run_id} is already complete; choose a new run ID", file=sys.stderr)
            return 2

        resume_state = Path(str(output_file) + ".resume.json")
        if existing is None and (output_file.exists() or resume_state.exists()):
            print(
                f"run_id {run_id} has pre-existing scraper output but no database run; choose a new run ID",
                file=sys.stderr,
            )
            return 2

        try:
            _ensure_run(
                conn,
                run_id=run_id,
                area=area,
                queries=queries,
                cell_km=args.cell_km,
                depth=args.depth,
                image=args.image,
                raw_path=str(output_file),
                config_json=config_json,
                status="running",
            )
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2

        scraper_exit_code: int | None = None
        try:
            write_query_snapshot(query_snapshot, queries)
            command = build_docker_command(
                area=area,
                queries_file=query_snapshot,
                output_file=output_file,
                options=options,
            )
            print(command_for_display(command))
            scraper_exit_code = run_scraper(command)
            if scraper_exit_code != 0:
                error = f"scraper failed with exit code {scraper_exit_code}"
                _mark_run(conn, run_id, status="failed", exit_code=scraper_exit_code, error=error)
                print(error, file=sys.stderr)
                return scraper_exit_code

            try:
                completed_inputs = load_resume_completed_input_ids(output_file, options.image)
                comparison = expected_inputs.compare_completed(completed_inputs)
            except Exception as exc:
                error = f"completion verification failed after scraper exit 0: {exc}"
                _mark_run(conn, run_id, status="failed", exit_code=scraper_exit_code, error=error)
                print(error, file=sys.stderr)
                return 1

            if comparison.unexpected:
                error = (
                    "resume completion state does not match this run: "
                    f"{comparison.unexpected} unexpected completed input(s)"
                )
                _mark_run(conn, run_id, status="failed", exit_code=scraper_exit_code, error=error)
                print(error, file=sys.stderr)
                return 1

            if comparison.missing:
                error = (
                    "scraper exited before all planned searches completed: "
                    f"completed {comparison.matched}/{len(expected_inputs)}"
                )
                _mark_run(conn, run_id, status="interrupted", exit_code=scraper_exit_code, error=error)
                print(error, file=sys.stderr)
                return 130

            stats = ingest_records(
                conn,
                run_id,
                iter_jsonl(output_file),
                bbox=area.bbox if strict_bounds else None,
                finalize_run=("complete", scraper_exit_code, None),
            )
        except KeyboardInterrupt:
            status_row = conn.execute("SELECT status FROM runs WHERE id = ?", (run_id,)).fetchone()
            if status_row is not None and status_row["status"] == "complete":
                print("collection completed; reporting interrupted", file=sys.stderr)
                return 130
            recorded_exit = scraper_exit_code if scraper_exit_code is not None else 130
            _mark_run(conn, run_id, status="interrupted", exit_code=recorded_exit, error="interrupted")
            print("collection interrupted", file=sys.stderr)
            return 130
        except Exception as exc:
            status_row = conn.execute("SELECT status FROM runs WHERE id = ?", (run_id,)).fetchone()
            if status_row is not None and status_row["status"] == "complete":
                print(f"collection completed but post-commit handling failed: {exc}", file=sys.stderr)
                return 1
            _mark_run(conn, run_id, status="failed", exit_code=scraper_exit_code, error=str(exc))
            print(f"collection failed: {exc}", file=sys.stderr)
            return 1

        return _report_stats(stats, operation="collection")
    finally:
        lock.release()


def cmd_ingest(args) -> int:
    run_id = _validate_run_id(args.run_id)
    conn = connect(args.db)
    row = conn.execute(
        """
        SELECT id, area_name, cell_km, queries_json, scraper_image, raw_path,
               bbox_json, config_json, status, exit_code
        FROM runs WHERE id = ?
        """,
        (run_id,),
    ).fetchone()
    if row is None:
        print("run_id does not exist; use collect to create tracked runs", file=sys.stderr)
        return 2
    if not row["raw_path"]:
        print("run has no recorded raw_path", file=sys.stderr)
        return 2

    supplied = Path(args.file).resolve()
    expected = Path(row["raw_path"]).resolve()
    if supplied != expected:
        print(f"ingest file must match the run raw_path: {expected}", file=sys.stderr)
        return 2

    bbox_data = json.loads(row["bbox_json"])
    bbox = BoundingBox(**bbox_data)
    bbox.validate()
    config = json.loads(row["config_json"]) if row["config_json"] else {}
    strict_bounds = bool(config.get("strict_bounds", True))
    if not bool(config.get("resume", False)):
        print("ingest requires a run with verifiable resume completion state", file=sys.stderr)
        return 2

    queries = json.loads(row["queries_json"])
    if not isinstance(queries, list) or not queries or not all(isinstance(query, str) for query in queries):
        print("run has invalid recorded query configuration", file=sys.stderr)
        return 2
    area = AreaConfig(str(row["area_name"]), bbox)

    lock = RunLock(expected.parent / ".sara.lock")
    try:
        lock.acquire()
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    try:
        try:
            expected_inputs = expected_resume_input_ids(area, queries, float(row["cell_km"]))
            completed_inputs = load_resume_completed_input_ids(expected, str(row["scraper_image"]))
            comparison = expected_inputs.compare_completed(completed_inputs)
        except (OSError, RuntimeError, ValueError) as exc:
            print(f"ingest completion verification failed: {exc}", file=sys.stderr)
            return 2

        if comparison.unexpected or comparison.missing:
            print(
                "ingest requires verified complete crawl evidence: "
                f"completed {comparison.matched}/{len(expected_inputs)}, "
                f"unexpected={comparison.unexpected}",
                file=sys.stderr,
            )
            return 2

        try:
            stats = ingest_records(
                conn,
                run_id,
                iter_jsonl(supplied),
                bbox=bbox if strict_bounds else None,
                finalize_run=("complete", row["exit_code"], None) if row["status"] != "complete" else None,
            )
        except Exception as exc:
            print(f"ingest failed: {exc}", file=sys.stderr)
            return 1

        return _report_stats(stats, operation="ingest")
    finally:
        lock.release()


def cmd_stats(args) -> int:
    conn = connect(args.db)
    total = conn.execute("SELECT COUNT(*) AS n FROM businesses").fetchone()["n"]
    print(f"canonical_businesses={total}")
    rows = conn.execute(
        """
        SELECT id, area_name, cell_km, depth, status, raw_records, accepted_records,
               out_of_bounds_records, unlocated_records, unidentified_records,
               unique_seen, new_businesses, started_at, finished_at, error
        FROM runs ORDER BY started_at DESC LIMIT ?
        """,
        (args.limit,),
    ).fetchall()
    for row in rows:
        print(dict(row))
    return 0


def _best_effort_cleanup(output_path: Path, created: bool) -> None:
    """Remove a partially written output this invocation created.

    Best effort: if removal itself fails the partial file may remain, so the
    caller reports the write failure and the exclusive-create guard refuses
    to overwrite the leftover on a later attempt.
    """
    if not created:
        return
    try:
        output_path.unlink()
    except OSError as exc:
        print(f"warning: could not remove partial output {output_path}: {exc}", file=sys.stderr)


def _report_recovery_plan(output_path: Path, data: bytes, summary) -> int:
    """Report a finished plan without letting presentation failure misstate it.

    The plan file is already complete when this runs. A reporting failure
    must not delete, rewrite, or retroactively fail the finished artifact:
    the operator gets a bounded lifecycle message and a non-success status
    while the valid plan stays on disk.
    """
    digest = hashlib.sha256(data).hexdigest()
    try:
        print(f"recovery_plan written: {output_path}")
        print(f"sha256={digest}")
        print(
            " ".join(
                (
                    f"selected_bins={summary['selected_bins']}",
                    f"estimated_recovery_searches={summary['estimated_recovery_searches']}",
                    f"full_uniform_recovery_searches={summary['full_uniform_recovery_searches']}",
                    f"search_delta_vs_uniform={summary['search_delta_vs_uniform']}",
                )
            )
        )
    except KeyboardInterrupt:
        try:
            print("recovery plan written; reporting interrupted", file=sys.stderr)
        except Exception:
            pass
        return 130
    except Exception as exc:
        try:
            print(f"recovery plan written but reporting failed: {exc}", file=sys.stderr)
        except Exception:
            pass
        return 1
    return 0


def cmd_recovery_plan(args) -> int:
    run_id = _validate_run_id(args.run_id)
    policy = RecoveryPolicy(
        policy_id=args.policy_id,
        tier_a_min=args.tier_a_min,
        tier_b_min=args.tier_b_min,
        recovery_cell_km=args.recovery_cell_km,
    )
    try:
        policy.validate()
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    output_path = Path(args.output)
    if output_path.exists():
        print(f"output path already exists; refusing to overwrite: {output_path}", file=sys.stderr)
        return 2

    def reject(message: str) -> int:
        print(message, file=sys.stderr)
        return 2

    try:
        conn = connect_readonly(args.db)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except sqlite3.Error as exc:
        print(f"recovery plan failed to open database: {exc}", file=sys.stderr)
        return 1

    try:
        # One read snapshot covers the source row and business membership.
        conn.execute("BEGIN")
        try:
            record = load_recovery_source_run(conn, run_id)
        except ValueError as exc:
            return reject(str(exc))
        row = record["row"]
        bbox = record["bbox"]
        queries = record["queries"]
        config = record["config"]
        config_raw = record["config_raw"]
        source_cell_km = record["source_cell_km"]
        if args.recovery_cell_km >= source_cell_km:
            return reject("recovery_cell_km must be strictly finer than the source cell size")

        member_count = conn.execute(
            "SELECT COUNT(*) AS n FROM run_businesses WHERE run_id = ?", (run_id,)
        ).fetchone()["n"]
        if member_count != row["unique_seen"]:
            return reject(
                f"run membership count ({member_count}) does not match recorded "
                f"unique_seen ({row['unique_seen']}); canonical state has changed"
            )

        coordinates: list[tuple[float, float]] = []
        for business in conn.execute(
            """
            SELECT b.id, b.latitude, b.longitude, b.last_run_id
            FROM run_businesses rb
            JOIN businesses b ON b.id = rb.business_id
            WHERE rb.run_id = ?
            ORDER BY b.id
            """,
            (run_id,),
        ):
            if business["last_run_id"] != run_id:
                return reject(
                    f"business {business['id']} was last observed by a later run "
                    f"({business['last_run_id']!r}); current coordinates are not "
                    "safe evidence for this source run"
                )
            latitude = business["latitude"]
            longitude = business["longitude"]
            if latitude is None or longitude is None:
                return reject(f"business {business['id']} has no recorded coordinates")
            if not (
                isinstance(latitude, (int, float))
                and isinstance(longitude, (int, float))
                and math.isfinite(latitude)
                and math.isfinite(longitude)
            ):
                return reject(f"business {business['id']} has nonfinite coordinates")
            coordinates.append((float(latitude), float(longitude)))

        source_run = {
            "id": row["id"],
            "area_name": row["area_name"],
            "bbox": {
                "min_lat": bbox.min_lat,
                "min_lon": bbox.min_lon,
                "max_lat": bbox.max_lat,
                "max_lon": bbox.max_lon,
            },
            "cell_km": source_cell_km,
            "depth": row["depth"],
            "queries": queries,
            "query_count": len(queries),
            "scraper_image": row["scraper_image"],
            "status": row["status"],
            "exit_code": row["exit_code"],
            "strict_bounds": True,
            "resume": True,
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "config_sha256": hashlib.sha256(config_raw.encode("utf-8")).hexdigest(),
            "config": project_source_config(config),
            "completion_claim": COMPLETION_CLAIM,
        }

        try:
            plan = build_recovery_plan(
                policy=policy,
                source_run=source_run,
                source_bbox=bbox,
                source_cell_km=float(source_cell_km),
                query_count=len(queries),
                coordinates=coordinates,
            )
        except ValueError as exc:
            return reject(str(exc))
    except sqlite3.Error as exc:
        print(f"recovery plan failed: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()

    try:
        data = serialize_recovery_plan(plan).encode("utf-8")
    except (TypeError, ValueError) as exc:
        return reject(f"plan payload failed strict JSON serialization: {exc}")
    created = False
    try:
        # O_BINARY is required on Windows so LF bytes are not translated to
        # CRLF; it is a no-op flag where the platform does not define it.
        fd = os.open(
            output_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
            0o600,
        )
        created = True
        try:
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("plan write made no progress")
                view = view[written:]
        finally:
            os.close(fd)
    except FileExistsError:
        return reject(f"output path already exists; refusing to overwrite: {output_path}")
    except OSError as exc:
        _best_effort_cleanup(output_path, created)
        print(f"recovery plan failed to write output: {exc}", file=sys.stderr)
        return 1
    except BaseException:
        # Interruption: best-effort removal of the partial file this
        # invocation created before propagating.
        _best_effort_cleanup(output_path, created)
        raise

    return _report_recovery_plan(output_path, data, plan.summary)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "plan":
            return cmd_plan(args)
        if args.command == "collect":
            return cmd_collect(args)
        if args.command == "ingest":
            return cmd_ingest(args)
        if args.command == "stats":
            return cmd_stats(args)
        if args.command == "recovery-plan":
            return cmd_recovery_plan(args)
        if args.command == "recovery-run":
            return cmd_recovery_run(args)
    except (FileNotFoundError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    raise AssertionError(args.command)



# ---------------------------------------------------------------------------
# recovery-run: plan-consuming execution layer
# ---------------------------------------------------------------------------

_RECOVERY_LABEL_PLAN = "sara.recovery.plan_sha256"
_RECOVERY_LABEL_RUN = "sara.recovery.run_id"


class _ChildFailure(Exception):
    def __init__(self, *, status: str, process_exit: int, error: str):
        super().__init__(error)
        self.status = status
        self.process_exit = process_exit
        self.error = error


def _recovery_child_run_id(plan_sha256: str, row: int, column: int) -> str:
    digest = hashlib.sha256()
    digest.update(plan_sha256.encode("utf-8"))
    digest.update(b"\x00")
    digest.update(str(row).encode("utf-8"))
    digest.update(b"\x00")
    digest.update(str(column).encode("utf-8"))
    return f"rr-{digest.hexdigest()[:40]}"


def _recovery_container_name(run_id: str) -> str:
    return f"sara-rr-{run_id}"


def _docker_inspect_container(name: str) -> dict | None:
    """Inspect a deterministic container name; fail closed on ambiguity."""
    completed = subprocess.run(
        ["docker", "inspect", name], capture_output=True, text=True, check=False
    )
    if completed.returncode != 0:
        stderr = completed.stderr or ""
        if "no such object" in stderr.lower():
            return None
        raise RuntimeError(
            f"docker inspect failed for {name}: {stderr.strip() or completed.returncode}"
        )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"docker inspect returned ambiguous output for {name}") from exc
    if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
        raise RuntimeError(f"docker inspect returned ambiguous output for {name}")
    info = payload[0]
    labels = (info.get("Config") or {}).get("Labels") or {}
    state = info.get("State") or {}
    return {
        "running": state.get("Running") is True,
        "labels": dict(labels),
        "container_id": info.get("Id"),
    }


def _recovery_child_config_json(plan, bin_record, proxy_sha256, plan_sha256) -> str:
    config = plan.config
    payload = {
        "area_name": f"{plan.area_name}-rr-r{bin_record.row}-c{bin_record.column}",
        "bbox": {
            "min_lat": bin_record.bbox.min_lat,
            "min_lon": bin_record.bbox.min_lon,
            "max_lat": bin_record.bbox.max_lat,
            "max_lon": bin_record.bbox.max_lon,
        },
        "queries": list(plan.queries),
        "cell_km": plan.recovery_cell_km,
        "depth": plan.depth,
        "concurrency": config["concurrency"],
        "browser_pool_size": config["browser_pool_size"],
        "pages_per_browser": config["pages_per_browser"],
        "lang": config["lang"],
        "zoom": config["zoom"],
        "resume": True,
        "image": plan.scraper_image,
        "proxy_sha256": proxy_sha256,
        "strict_bounds": True,
        "recovery": {
            "plan_sha256": plan_sha256,
            "plan_schema_version": 1,
            "source_run_id": plan.source_run_id,
            "policy_id": plan.policy.policy_id,
            "row": bin_record.row,
            "column": bin_record.column,
            "tier": bin_record.tier,
            "planned_searches": bin_record.planned_searches,
        },
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _write_plan_snapshot(snapshot_path: Path, data: bytes) -> None:
    created = False
    try:
        fd = os.open(
            snapshot_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
            0o600,
        )
        created = True
        try:
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("snapshot write made no progress")
                view = view[written:]
        finally:
            os.close(fd)
    except FileExistsError:
        raise
    except OSError as exc:
        if created:
            try:
                snapshot_path.unlink()
            except OSError:
                pass
        raise
    except BaseException:
        if created:
            try:
                snapshot_path.unlink()
            except OSError:
                pass
        raise


def _guard_child_progress_report(plan_sha256, conn, bin_record, run_id, completed, total) -> int:
    """Report one committed child; a reporting failure stops scheduling."""
    try:
        print(
            f"recovery child complete: r{bin_record.row}-c{bin_record.column} "
            f"run_id={run_id} ({completed}/{total})"
        )
        return 0
    except KeyboardInterrupt:
        _repair_parent_after_report_failure(conn, plan_sha256, bin_record, "interrupted", "reporting interrupted")
        return 130
    except Exception as exc:
        _repair_parent_after_report_failure(conn, plan_sha256, bin_record, "interrupted", f"reporting failed ({exc})")
        return 1


def _safe_release(lock) -> None:
    """Release a RunLock without letting unlink failure overwrite committed state."""
    try:
        lock.release()
    except Exception as exc:
        _best_effort_stderr(f"warning: lock release failed for {lock.path}: {exc}")


def _best_effort_stderr(message: str) -> None:
    """Print to stderr; a broken stderr cannot reclassify a completed effect."""
    try:
        print(message, file=sys.stderr)
    except Exception:
        pass


def _best_effort_parent_failure(conn, plan_sha256, message):
    """Best-effort transition of an active parent to failed; never downgrade complete."""
    try:
        # SR-A54-03: check current status before repair.
        row = conn.execute(
            "SELECT status FROM recovery_executions WHERE plan_sha256 = ?",
            (plan_sha256,),
        ).fetchone()
        if row is not None and row["status"] == "complete":
            return  # never downgrade a committed complete parent
        conn.execute("BEGIN IMMEDIATE")
        try:
            set_recovery_parent_status(conn, plan_sha256, "failed", message)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    except Exception as exc:
        _best_effort_stderr(f"warning: parent failure repair failed: {exc}")


def _validate_query_snapshot(bin_dir, plan, *, is_started):
    """SR-F5-01/SR-F5-02: enforce exact query snapshot for every incomplete child.

    Unstarted: missing/partial/mismatching is safely repaired from the plan.
    Started: missing/mismatching is fail-closed (never rewrite historical evidence).
    """
    from .scraper import recovery_query_snapshot_bytes
    query_snapshot = bin_dir / "queries.txt"
    expected = recovery_query_snapshot_bytes(list(plan.queries))
    if is_started:
        if not query_snapshot.is_file():
            raise PlanRejected(
                f"started child query snapshot is missing: {query_snapshot}"
            )
        actual = query_snapshot.read_bytes()
        if actual != expected:
            raise PlanRejected(
                f"started child query snapshot does not match the frozen plan: {query_snapshot}"
            )
    else:
        if query_snapshot.exists():
            actual = query_snapshot.read_bytes()
            if actual != expected:
                query_snapshot.unlink()
        if not query_snapshot.exists():
            fd = os.open(
                query_snapshot,
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0),
                0o644,
            )
            try:
                view = memoryview(expected)
                while view:
                    written = os.write(fd, view)
                    if written <= 0:
                        raise OSError("query snapshot write made no progress")
                    view = view[written:]
            finally:
                os.close(fd)


def _bounded_operational_failure(conn, plan_sha256, exc, context):
    """A54-F01: one bounded handler for post-registration operational failures.

    Rolls back any active transaction, best-effort marks the parent failed
    (unless already complete — SR-A54-03), and returns rc=1. Does NOT
    swallow _ChildFailure, PlanRejected, KeyboardInterrupt, or AssertionError.
    """
    try:
        conn.rollback()
    except Exception:
        pass
    # SR-A54-03: never downgrade a committed complete parent.
    try:
        row = conn.execute(
            "SELECT status FROM recovery_executions WHERE plan_sha256 = ?",
            (plan_sha256,),
        ).fetchone()
        if row is None or row["status"] != "complete":
            _best_effort_parent_failure(conn, plan_sha256, f"{context}: {exc}")
    except Exception:
        pass
    _best_effort_stderr(f"recovery-run {context}: {exc}")
    return 1


def _repair_parent_after_report_failure(conn, plan_sha256, bin_record, status, detail):
    """Rollback-safe parent repair after a committed child report failure.

    Never alters the completed child. If the DB repair itself fails, emits
    best-effort stderr and returns without raising (E4-F01).
    """
    message = (
        f"child r{bin_record.row}-c{bin_record.column} completed; "
        f"{detail}; no further bins scheduled"
    )
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            set_recovery_parent_status(conn, plan_sha256, status, message)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    except Exception as db_exc:
        _best_effort_stderr(
            f"warning: parent status repair failed after child completion: {db_exc}"
        )


def _guard_final_report(plan_sha256, report) -> int:
    try:
        report()
        return 0
    except KeyboardInterrupt:
        _best_effort_stderr("recovery execution complete; reporting interrupted")
        return 130
    except Exception as exc:
        _best_effort_stderr(f"recovery execution complete but reporting failed: {exc}")
        return 1


def cmd_recovery_run(args) -> int:
    def reject(message: str) -> int:
        print(message, file=sys.stderr)
        return 2

    plan_path = Path(args.plan)
    try:
        data = plan_path.read_bytes()
    except OSError as exc:
        return reject(f"cannot read plan file: {exc}")
    plan_sha256 = hashlib.sha256(data).hexdigest()

    supplied_sha = str(args.plan_sha256 or "").strip().lower()
    if supplied_sha != plan_sha256:
        return reject(
            "plan SHA-256 mismatch: the exact plan-byte execution key does not match --plan-sha256"
        )

    try:
        plan = parse_execution_plan(data)
    except PlanRejected as exc:
        return reject(f"plan rejected: {exc}")

    if args.expected_searches != plan.targeted_searches:
        return reject(
            f"--expected-searches ({args.expected_searches}) does not equal the plan's "
            f"recomputed targeted searches ({plan.targeted_searches})"
        )

    try:
        validate_recovery_queries(list(plan.queries))
        for bin_record in plan.selected_bins:
            validate_child_coordinate_precision(bin_record.bbox, plan.recovery_cell_km)
        validate_digest_pinned_image(plan.scraper_image)
    except (PlanRejected, ValueError) as exc:
        return reject(f"plan rejected: {exc}")


    proxy_path: Path | None = None
    plan_proxy_sha = plan.config.get("proxy_sha256")
    if plan_proxy_sha is None:
        if args.proxy_file:
            return reject("plan records no proxy but --proxy-file was supplied")
    else:
        if not args.proxy_file:
            return reject("plan requires a proxy file but --proxy-file was not supplied")
        proxy_path = Path(args.proxy_file)
        try:
            proxy_bytes = proxy_path.read_bytes()
        except OSError as exc:
            return reject(f"cannot read proxy file: {exc}")
        if hashlib.sha256(proxy_bytes).hexdigest() != plan_proxy_sha:
            return reject("proxy file SHA-256 does not match the plan's recorded proxy hash")

    # RRI-F11: the exact plan-derived scrape contract must validate before
    # any write connector, output directory, or lock is touched.
    try:
        _preflight_options = ScrapeOptions(
            cell_km=plan.recovery_cell_km, depth=plan.depth,
            concurrency=plan.config["concurrency"],
            browser_pool_size=plan.config["browser_pool_size"],
            pages_per_browser=plan.config["pages_per_browser"],
            lang=plan.config["lang"], zoom=plan.config["zoom"],
            resume=True, image=plan.scraper_image, proxy_file=proxy_path,
        )
        if plan.depth < 1 or plan.depth > 10:
            raise ValueError("depth must be between 1 and 10")
        _preflight_options.validate()
    except (ValueError, TypeError) as exc:
        return reject(f"plan rejected: plan-derived scraper configuration is invalid: {exc}")

    try:
        conn = connect_readonly(args.db)
    except FileNotFoundError as exc:
        return reject(str(exc))
    except sqlite3.Error as exc:
        print(f"recovery-run failed to open database read-only: {exc}", file=sys.stderr)
        return 1
    try:
        conn.execute("BEGIN")
        bind_plan_to_source(conn, plan)
    except (SourceRunRejected, ValueError) as exc:
        return reject(str(exc))
    except sqlite3.Error as exc:
        print(f"recovery-run failed during source binding: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()

    output_root = Path(args.output_dir).resolve()
    execution_root = output_root / plan_sha256

    container_names = {
        (b.row, b.column): _recovery_container_name(_recovery_child_run_id(plan_sha256, b.row, b.column))
        for b in plan.selected_bins
    }

    if args.dry_run:
        print(f"plan_sha256={plan_sha256}")
        print(f"selected_bins={len(plan.selected_bins)} planned_searches={plan.targeted_searches}")
        print("execution_history=not_checked")
        for bin_record in sorted(plan.selected_bins, key=lambda b: (b.row, b.column)):
            run_id = _recovery_child_run_id(plan_sha256, bin_record.row, bin_record.column)
            bin_dir = execution_root / "bins" / f"r{bin_record.row}-c{bin_record.column}"
            area = AreaConfig(f"{plan.area_name}-rr-r{bin_record.row}-c{bin_record.column}", bin_record.bbox)
            options = ScrapeOptions(
                cell_km=plan.recovery_cell_km, depth=plan.depth,
                concurrency=plan.config["concurrency"],
                browser_pool_size=plan.config["browser_pool_size"],
                pages_per_browser=plan.config["pages_per_browser"],
                lang=plan.config["lang"], zoom=plan.config["zoom"],
                resume=True, image=plan.scraper_image, proxy_file=proxy_path,
            )
            command = build_docker_command(
                area=area, queries_file=bin_dir / "queries.txt",
                output_file=bin_dir / "results.jsonl", options=options,
                prepare_paths=False,
                container_name=container_names[(bin_record.row, bin_record.column)],
                labels={
                    _RECOVERY_LABEL_PLAN: plan_sha256,
                    _RECOVERY_LABEL_RUN: run_id,
                },
            )
            print(
                f"bin r{bin_record.row}-c{bin_record.column} tier={bin_record.tier} "
                f"searches={bin_record.planned_searches} run_id={run_id} "
                f"container={container_names[(bin_record.row, bin_record.column)]}"
            )
            print(f"  {command_for_display(command)}")
        return 0

    if not plan.selected_bins:
        print("no selected recovery bins; nothing to execute")
        return 0

    parent_lock = RunLock(execution_root / ".sara-recovery.lock")
    try:
        parent_lock.acquire()
    except RuntimeError as exc:
        return reject(f"recovery execution is already active: {exc}")
    except OSError as exc:
        print(f"recovery-run failed to create parent lock: {exc}", file=sys.stderr)
        return 1

    try:
        conn = None
        try:
            conn = connect_existing(args.db)
        except FileNotFoundError as exc:
            return reject(str(exc))
        except sqlite3.Error as exc:
            print(f"recovery-run failed to open database: {exc}", file=sys.stderr)
            return 1
        try:
            try:
                conn.execute("BEGIN IMMEDIATE")
            except sqlite3.Error as begin_exc:
                print(f"recovery-run registration BEGIN failed: {begin_exc}", file=sys.stderr)
                return 1
            try:
                bind_plan_to_source(conn, plan)
                ensure_recovery_schema(conn)
                verify_recovery_schema(conn)
                snapshot_path = execution_root / "recovery-plan.json"
                state = register_recovery_execution(
                    conn, plan=plan, plan_sha256=plan_sha256,
                    output_root=str(execution_root), plan_snapshot_path=str(snapshot_path),
                    container_names=container_names,
                )
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        except (SourceRunRejected, RecoverySchemaError, PlanRejected) as exc:
            return reject(str(exc))
        except Exception as exc:  # registration must fail closed, never escape
            print(f"recovery-run registration failed: {exc}", file=sys.stderr)
            return 1

        try:
            mappings_list = list(conn.execute(
                "SELECT row, column, tier, bbox_json, planned_searches, run_id, container_name "
                "FROM recovery_execution_bins WHERE plan_sha256 = ? ORDER BY row, column",
                (plan_sha256,),
            ))
        except (sqlite3.Error, OSError) as exc:
            return _bounded_operational_failure(conn, plan_sha256, exc, "mapping list read")
        _MAPPING_ROOT_HOLDER["root"] = str(execution_root)
        if state == "complete":
            try:
                stored_result = _validate_complete_parent(
                    conn, plan, plan_sha256, mappings_list, container_names
                )
            except PlanRejected as exc:
                return reject(f"stored complete execution is inconsistent: {exc}")
            try:
                print("recovery-run already complete")
                print(stored_result)
            except KeyboardInterrupt:
                _best_effort_stderr("recovery-run already complete; reporting interrupted")
                return 130
            except Exception as report_exc:
                _best_effort_stderr(
                    f"recovery-run already complete but reporting failed: {report_exc}"
                )
                return 1
            return 2
        # SR-I02: prior status is preserved through snapshot/mapping
        # validation and no-new-effect checks; the parent moves to running
        # only when this invocation actually performs child progress.

        try:
            execution_root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return _bounded_operational_failure(conn, plan_sha256, exc, "execution root creation")
        snapshot_path = execution_root / "recovery-plan.json"
        try:
            snapshot_matches = (
                snapshot_path.exists()
                and hashlib.sha256(snapshot_path.read_bytes()).hexdigest() == plan_sha256
            )
        except OSError as exc:
            return _bounded_operational_failure(conn, plan_sha256, exc, "plan snapshot read")
        if not snapshot_matches:
            try:
                started = conn.execute(
                    "SELECT COUNT(*) AS n FROM recovery_execution_bins "
                    "WHERE plan_sha256 = ? AND run_id IS NOT NULL",
                    (plan_sha256,),
                ).fetchone()["n"]
            except sqlite3.Error as exc:
                return _bounded_operational_failure(conn, plan_sha256, exc, "started-child count")
            if snapshot_path.exists() and started:
                return reject(
                    "existing plan snapshot does not match the authorized plan bytes "
                    "and at least one child has started; failing closed"
                )
            try:
                if snapshot_path.exists():
                    snapshot_path.unlink()
                _write_plan_snapshot(snapshot_path, data)
            except OSError as exc:
                _best_effort_parent_failure(conn, plan_sha256, f"plan snapshot write failed: {exc}")
                _best_effort_stderr(f"recovery-run failed to write plan snapshot: {exc}")
                return 1

        mappings = {
            (m["row"], m["column"]): m
            for m in mappings_list
        }
        ordered_bins = sorted(plan.selected_bins, key=lambda b: (b.row, b.column))
        total = len(ordered_bins)
        completed_count = 0
        progress_started = False
        for bin_record in ordered_bins:
            mapping = mappings[(bin_record.row, bin_record.column)]
            # Ownership of a child row is established only by provenance
            # (mapped children) or by the executor committing the created
            # deterministic child; failure handlers must never mutate a
            # merely derivable ID.
            owned_child = {"run_id": None}
            if mapping["run_id"] is not None:
                # RRI-F03/SR-I04: full provenance before trusting status.
                try:
                    child_row = _validate_child_provenance(
                        conn, plan, plan_sha256, mapping, container_names
                    )
                except PlanRejected as exc:
                    # The stored execution state is unusable. Record the
                    # lifecycle outcome instead of a bare rejection so a
                    # crash-stranded running parent/child cannot survive.
                    # The mapped run is eligible for child repair only when
                    # its identity equals the plan-derived child ID: an
                    # untrusted mapping must never mutate the row it
                    # happens to point at.
                    try:
                        conn.execute("BEGIN IMMEDIATE")
                        try:
                            if mapping["run_id"] == _recovery_child_run_id(
                                plan_sha256, bin_record.row, bin_record.column
                            ):
                                conn.execute(
                                    "UPDATE runs SET status = 'failed', "
                                    "finished_at = ?, error = ? "
                                    "WHERE id = ? AND status != 'complete'",
                                    (utc_now(), str(exc), mapping["run_id"]),
                                )
                            set_recovery_parent_status(
                                conn, plan_sha256, "failed", str(exc)
                            )
                            conn.commit()
                        except BaseException:
                            conn.rollback()
                            raise
                    except Exception:
                        return 1
                    _best_effort_stderr(f"recovery execution state is inconsistent: {exc}")
                    return 2
                # Provenance passed: the mapped child is owned.
                owned_child["run_id"] = mapping["run_id"]
                if child_row["status"] == "complete":
                    completed_count += 1
                    continue
            def mark_parent_running():
                nonlocal progress_started
                if not progress_started:
                    progress_started = True
                    if state == "resumed":
                        conn.execute("BEGIN IMMEDIATE")
                        try:
                            resume_recovery_parent(conn, plan_sha256)
                            conn.commit()
                        except BaseException:
                            conn.rollback()
                            raise
            try:
                outcome = _execute_recovery_child(
                    conn, plan, plan_sha256, execution_root, bin_record,
                    mapping, proxy_path, container_names[(bin_record.row, bin_record.column)],
                    mark_parent_running=mark_parent_running,
                    owned_child=owned_child,
                )
            except _ChildFailure as failure:
                if failure.status != "none":
                    # Repair the owned child, not just the parent. An
                    # unstarted bin whose child row was never created owns
                    # nothing: the derivable ID may name an unrelated run
                    # and must not be mutated.
                    child_run_id = owned_child["run_id"]
                    try:
                        conn.execute("BEGIN IMMEDIATE")
                        try:
                            if child_run_id is not None:
                                conn.execute(
                                    "UPDATE runs SET status = ?, finished_at = ?, error = ? "
                                    "WHERE id = ? AND status != 'complete'",
                                    (failure.status, utc_now(), failure.error, child_run_id),
                                )
                            set_recovery_parent_status(conn, plan_sha256, failure.status, failure.error)
                            conn.commit()
                        except BaseException:
                            conn.rollback()
                            raise
                    except Exception as repair_exc:
                        _best_effort_stderr(
                            f"recovery-run child/parent repair failed: {repair_exc}"
                        )
                        return 1
                else:
                    # Fix4: no-effect rejection. Fresh parent: interrupted.
                    # Resumed parent with prior progress: interrupted.
                    # Resumed parent with NO prior progress: preserve state.
                    # Repair failure: rc 1, never silent rc 2 with running.
                    try:
                        conn.execute("BEGIN IMMEDIATE")
                        try:
                            if state == "registered" or progress_started:
                                set_recovery_parent_status(
                                    conn, plan_sha256, "interrupted",
                                    f"no-new-effect rejection: {failure.error}",
                                )
                                conn.commit()
                            else:
                                conn.rollback()  # preserve prior status
                        except BaseException:
                            conn.rollback()
                            raise
                    except Exception:
                        return 1  # repair failed: rc 1, never silent rc 2
                _best_effort_stderr(failure.error)
                return failure.process_exit
            except KeyboardInterrupt:
                # Bounded KI repair. A failed child-interrupt repair is an
                # operational failure (bounded parent repair, exit 1); a
                # failed parent repair falls back to best-effort parent
                # failure so a resumed running parent never survives the
                # interrupt as falsely running, then exits 1. Only a fully
                # repaired interrupt exits 130.
                ki_run_id = owned_child["run_id"]
                if ki_run_id is not None:
                    try:
                        _child_ki_guard(conn, ki_run_id)
                    except Exception as child_repair_exc:
                        return _bounded_operational_failure(
                            conn, plan_sha256, child_repair_exc, "child interrupt repair"
                        )
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    try:
                        set_recovery_parent_status(
                            conn, plan_sha256, "interrupted", "recovery-run interrupted"
                        )
                        conn.commit()
                    except BaseException:
                        conn.rollback()
                        raise
                except Exception as parent_repair_exc:
                    _best_effort_parent_failure(
                        conn, plan_sha256,
                        f"recovery-run interrupt parent repair failed: {parent_repair_exc}",
                    )
                    return 1
                _best_effort_stderr("recovery-run interrupted")
                return 130
            except PlanRejected as reject_exc:
                # Snapshot rejection repairs the owned child and the parent.
                child_run_id = owned_child["run_id"]
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    try:
                        if child_run_id is not None:
                            conn.execute(
                                "UPDATE runs SET status = 'failed', finished_at = ?, "
                                "error = ? WHERE id = ? AND status != 'complete'",
                                (utc_now(), str(reject_exc), child_run_id),
                            )
                        set_recovery_parent_status(
                            conn, plan_sha256, "failed", str(reject_exc)
                        )
                        conn.commit()
                    except BaseException:
                        conn.rollback()
                        raise
                except Exception:
                    return 1
                _best_effort_stderr(str(reject_exc))
                return 2
            except (sqlite3.Error, OSError, RuntimeError, RecoverySchemaError) as op_exc:
                # An operational error mid-child must not strand an owned
                # active child as running; repair it before the parent. An
                # unstarted bin owns nothing until its child row commits,
                # so a merely derivable ID is never mutated.
                child_run_id = owned_child["run_id"]
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    try:
                        if child_run_id is not None:
                            conn.execute(
                                "UPDATE runs SET status = 'failed', finished_at = ?, "
                                "error = ? WHERE id = ? AND status != 'complete'",
                                (utc_now(), f"operational failure: {op_exc}", child_run_id),
                            )
                        conn.commit()
                    except BaseException:
                        conn.rollback()
                        raise
                except Exception:
                    pass  # child repair best-effort; parent repair below
                return _bounded_operational_failure(
                    conn, plan_sha256, op_exc, "child orchestration"
                )
            if outcome == "complete":
                completed_count += 1
                report_rc = _guard_child_progress_report(
                    plan_sha256, conn, bin_record,
                    mapping["run_id"] or _recovery_child_run_id(
                        plan_sha256, bin_record.row, bin_record.column
                    ),
                    completed_count, total,
                )
                if report_rc != 0:
                    return report_rc

        try:
            conn.execute("BEGIN IMMEDIATE")
            metrics = finalize_recovery_execution(
                conn, plan_sha256, plan.source_run_id, plan.associated_businesses
            )
            conn.commit()
        except BaseException as finalize_exc:
            conn.rollback()
            if isinstance(finalize_exc, KeyboardInterrupt):
                raise

            _best_effort_parent_failure(
                conn, plan_sha256, f"finalization failed: {finalize_exc}"
            )
            _best_effort_stderr(f"recovery-run finalization failed: {finalize_exc}")
            return 1

        try:
            result_json = conn.execute(
                "SELECT result_json FROM recovery_executions WHERE plan_sha256 = ?",
                (plan_sha256,),
            ).fetchone()["result_json"]
        except (sqlite3.Error, TypeError) as fetch_exc:
            # A54-F01: parent is already committed complete; bounded failure.
            _best_effort_stderr(f"recovery-run failed to fetch final result: {fetch_exc}")
            return 1
        return _guard_final_report(
            plan_sha256,
            lambda: (
                print(f"recovery complete: {json.dumps(metrics, ensure_ascii=False, sort_keys=True)}"),
                print(f"result_json={result_json}"),
            ),
        )
    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass
        _safe_release(parent_lock)


_RESULT_V1_INT_FIELDS = (
    "child_runs", "planned_searches", "raw_records", "accepted_records",
    "out_of_bounds_records", "unlocated_records", "unidentified_records",
    "unique_recovery_seen", "source_overlap_businesses",
    "source_increment_businesses", "globally_new_businesses",
    "source_membership_at_plan_count", "source_membership_current_count",
)


def _validate_child_provenance(conn, plan, plan_sha256, mapping, container_names):
    """Full deterministic provenance check for one mapped child.

    Returns the child runs row when every field matches the plan-derived
    expectation exactly. Raises PlanRejected on any disagreement, before the
    caller may trust the child's recorded status.
    """
    row_value, column_value = mapping["row"], mapping["column"]
    expected_run_id = _recovery_child_run_id(plan_sha256, row_value, column_value)
    bin_record = next(
        b for b in plan.selected_bins if (b.row, b.column) == (row_value, column_value)
    )
    bin_dir = Path(mapping_root(plan_sha256)) / "bins" / f"r{row_value}-c{column_value}"
    expected_raw = str(bin_dir / "results.jsonl")
    proxy_sha = plan.config.get("proxy_sha256")
    expected_config = _recovery_child_config_json(plan, bin_record, proxy_sha, plan_sha256)
    expected_bbox_json = json.dumps(bin_record.bbox.__dict__, sort_keys=True)

    if mapping["run_id"] != expected_run_id:
        raise PlanRejected(f"mapping r{row_value}-c{column_value} run_id is not the deterministic child ID")
    if mapping["tier"] != bin_record.tier:
        raise PlanRejected(f"mapping r{row_value}-c{column_value} tier disagrees with the plan")
    if mapping["planned_searches"] != (bin_record.planned_searches or 0):
        raise PlanRejected(f"mapping r{row_value}-c{column_value} planned_searches disagrees with the plan")
    if mapping["container_name"] != container_names[(row_value, column_value)]:
        raise PlanRejected(f"mapping r{row_value}-c{column_value} container_name disagrees with the plan")
    mapping_bbox = json.loads(mapping["bbox_json"])
    plan_bbox = json.loads(expected_bbox_json)
    if mapping_bbox != plan_bbox:
        raise PlanRejected(f"mapping r{row_value}-c{column_value} bbox disagrees with the plan")

    child = conn.execute(
        "SELECT * FROM runs WHERE id = ?", (expected_run_id,)
    ).fetchone()
    if child is None:
        raise PlanRejected(f"child run {expected_run_id} for r{row_value}-c{column_value} does not exist")
    if child["area_name"] != f"{plan.area_name}-rr-r{row_value}-c{column_value}":
        raise PlanRejected(f"child {expected_run_id} area_name disagrees with the plan")
    if child["bbox_json"] != expected_bbox_json:
        raise PlanRejected(f"child {expected_run_id} bbox_json disagrees with the plan")
    if float(child["cell_km"]) != plan.recovery_cell_km:
        raise PlanRejected(f"child {expected_run_id} cell_km disagrees with the plan")
    if child["depth"] != plan.depth:
        raise PlanRejected(f"child {expected_run_id} depth disagrees with the plan")
    if child["queries_json"] != json.dumps(list(plan.queries), ensure_ascii=False):
        raise PlanRejected(f"child {expected_run_id} queries_json disagrees with the plan")
    if child["scraper_image"] != plan.scraper_image:
        raise PlanRejected(f"child {expected_run_id} scraper_image disagrees with the plan")
    if child["config_json"] != expected_config:
        raise PlanRejected(f"child {expected_run_id} config_json disagrees with the plan-derived configuration")
    if child["raw_path"] != expected_raw:
        raise PlanRejected(f"child {expected_run_id} raw_path disagrees with the deterministic bin path")
    # SR-E4-01: child status must be a known lifecycle state; corrupted
    # status is an inconsistent-state rejection, never silently normalized.
    if child["status"] not in ("running", "interrupted", "failed", "complete"):
        raise PlanRejected(
            f"child {expected_run_id} has invalid status {child['status']!r}"
        )
    # SR-A54-01: started_at must be a non-empty string.
    if not isinstance(child["started_at"], str) or not child["started_at"]:
        raise PlanRejected(f"child {expected_run_id} has invalid started_at")

    # SR-F5-04: status-dependent terminal lifecycle fields must be consistent.
    status = child["status"]
    finished = child["finished_at"]
    error = child["error"]
    if status == "complete":
        if not isinstance(finished, str) or not finished:
            raise PlanRejected(f"child {expected_run_id} is complete but has no finished_at")
        if error is not None:
            raise PlanRejected(f"child {expected_run_id} is complete but carries an error")
    elif status == "running":
        if finished is not None:
            raise PlanRejected(f"child {expected_run_id} is running but has a finished_at")
    else:
        if not isinstance(finished, str) or not finished:
            raise PlanRejected(
                f"child {expected_run_id} is {status} but has no finished_at"
            )
    return child


_MAPPING_ROOT_HOLDER: dict[str, str] = {}


def mapping_root(plan_sha256: str) -> Path:
    """Current execution root for provenance path checks (set per invocation)."""
    return Path(_MAPPING_ROOT_HOLDER["root"])


def _validate_complete_parent(conn, plan, plan_sha256, mappings, container_names) -> str:
    """Structural validation before reporting an already-complete execution."""
    incomplete = 0
    for mapping in mappings:
        child = _validate_child_provenance(conn, plan, plan_sha256, mapping, container_names)
        if child["status"] != "complete":
            incomplete += 1
    if incomplete:
        raise PlanRejected(
            "parent is marked complete but " + str(incomplete) +
            " mapped child run(s) are not complete; the execution state is inconsistent"
        )
    result_raw = conn.execute(
        "SELECT result_json FROM recovery_executions WHERE plan_sha256 = ?",
        (plan_sha256,),
    ).fetchone()["result_json"]
    if not isinstance(result_raw, str) or not result_raw:
        raise PlanRejected("complete parent has no stored result_json")
    def _no_dup_result_keys(pairs):
        seen = set()
        for key, _ in pairs:
            if key in seen:
                raise ValueError(f"duplicate result key: {key!r}")
            seen.add(key)
        return dict(pairs)

    try:
        result = json.loads(
            result_raw,
            parse_constant=lambda t: (_ for _ in ()).throw(ValueError(f"non-standard constant {t!r}")),
            object_pairs_hook=_no_dup_result_keys,
        )
    except ValueError as exc:
        raise PlanRejected(f"complete parent result_json is not valid strict JSON: {exc}")
    if not isinstance(result, dict) or set(result) != set(_RESULT_V1_INT_FIELDS):
        raise PlanRejected("complete parent result_json does not match the v1 result field set")
    for field in _RESULT_V1_INT_FIELDS:
        value = result[field]
        if isinstance(value, bool) or not isinstance(value, int):
            raise PlanRejected(f"complete parent result_json field {field} must be an integer")
        if value < 0:
            raise PlanRejected(f"complete parent result_json field {field} must be nonnegative")
    # SR-E3-01: cross-check stable fields against the frozen plan identity.
    if result["child_runs"] != len(plan.selected_bins):
        raise PlanRejected("complete parent result_json child_runs does not match the plan's selected-bin count")
    if result["planned_searches"] != plan.targeted_searches:
        raise PlanRejected("complete parent result_json planned_searches does not match the plan")
    if result["source_membership_at_plan_count"] != plan.associated_businesses:
        raise PlanRejected("complete parent result_json source_membership_at_plan_count does not match the plan")
    return result_raw


def _child_ki_guard(conn, run_id):
    """F5-F02: child interrupt repair after KI.

    Returns "absent" (no child row), "complete" (nothing to repair), or
    "repaired" (durably marked interrupted). A failure of the repair
    transaction itself propagates so the caller classifies the interrupt
    as an operational failure instead of a clean exit 130.
    """
    row = conn.execute(
        "SELECT status FROM runs WHERE id = ?", (run_id,)
    ).fetchone()
    if row is None:
        return "absent"
    if row["status"] == "complete":
        return "complete"
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "UPDATE runs SET status = 'interrupted', finished_at = ?, "
            "error = 'recovery child interrupted' WHERE id = ? AND status != 'complete'",
            (utc_now(), run_id),
        )
        conn.commit()
        return "repaired"
    except BaseException:
        conn.rollback()
        raise



def _remove_stopped_container(container_id, container_name):
    """Remove a stopped owned container by its immutable ID.

    Returns None on success. Raises _ChildFailure on failure.
    """
    try:
        removed = subprocess.run(
            ["docker", "rm", container_id],
            capture_output=True, text=True, check=False,
        )
    except OSError as rm_exc:
        raise _ChildFailure(
            status="failed", process_exit=1,
            error=f"docker rm process creation failed: {rm_exc}",
        ) from rm_exc
    if removed.returncode != 0:
        stderr_text = removed.stderr or ""
        if "no such container" in stderr_text.lower():
            recheck = _docker_inspect_container(container_name)
            if recheck is not None:
                raise _ChildFailure(
                    status="none", process_exit=2,
                    error=(
                        f"container name {container_name} now holds a replacement "
                        "after the inspected container disappeared; refusing to "
                        "remove a replacement without fresh ownership verification"
                    ),
                )
            return None  # container gone; safe to continue
        raise _ChildFailure(
            status="failed", process_exit=1,
            error=f"failed to remove stopped owned container {container_id}: "
                  f"{stderr_text.strip() or removed.returncode}",
        )
    return None

def _execute_recovery_child(
    conn, plan, plan_sha256, execution_root, bin_record, mapping, proxy_path, container_name,
    *, mark_parent_running=None, owned_child=None,
) -> str:
    row, column = bin_record.row, bin_record.column
    run_id = _recovery_child_run_id(plan_sha256, row, column)
    bin_dir = execution_root / "bins" / f"r{row}-c{column}"
    _deferred_rm = None  # 429: stopped-container ID, removed after provenance
    output_file = bin_dir / "results.jsonl"
    area = AreaConfig(f"{plan.area_name}-rr-r{row}-c{column}", bin_record.bbox)

    child_lock = RunLock(bin_dir / ".sara.lock")
    try:
        child_lock.acquire()
    except OSError as exc:
        raise _ChildFailure(
            status="failed", process_exit=1,
            error=f"child lock/bin directory creation failed: {exc}",
        ) from exc
    except RuntimeError as exc:
        # Active child lock: another process owns this bin; no new effect.
        raise _ChildFailure(status="none", process_exit=2, error=str(exc)) from exc

    def child_mark_no_exit(status: str, error: str | None) -> None:
        """Lifecycle-only child status update; exit provenance is untouched."""
        child_mark(status, None, error)

    def record_child_exit(exit_code: int) -> None:
        """Record a newly observed scraper process exit code."""
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "UPDATE runs SET exit_code = ? WHERE id = ?", (exit_code, run_id)
            )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise

    def child_mark(status: str, exit_code: int | None, error: str | None) -> None:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "UPDATE runs SET status = ?, finished_at = ?, error = ? WHERE id = ?",
                (status, utc_now(), error, run_id),
            )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise

    try:
        inspect = _docker_inspect_container(container_name)
    except (RuntimeError, OSError) as exc:
        raise _ChildFailure(
            status="failed", process_exit=1,
            error=f"container liveness reconciliation failed: {exc}",
        ) from exc
    try:
        pass  # beginning of original guarded body
        if inspect is not None:
            labels = inspect["labels"]
            if labels.get(_RECOVERY_LABEL_PLAN) != plan_sha256 or labels.get(
                _RECOVERY_LABEL_RUN
            ) != run_id:
                raise _ChildFailure(
                    status="none", process_exit=2,
                    error=(
                        f"container name {container_name} exists with wrong ownership labels; "
                        "refusing to touch an unrelated container"
                    ),
                )
            if inspect["running"]:
                raise _ChildFailure(
                    status="none", process_exit=2,
                    error=(
                        f"matching recovery container {container_name} is still active; "
                        "prior acquisition remains in progress; refusing a second launch"
                    ),
                )
            # Matching stopped container still reserves its deterministic
            # name: remove the inspected immutable container ID (not the
            # 429/SR-A54-02: DEFER stopped-container removal until AFTER
            # provenance and query snapshot validation.
            stopped_container_id = inspect.get("container_id")
            if not stopped_container_id or not isinstance(stopped_container_id, str):
                raise _ChildFailure(
                    status="failed", process_exit=1,
                    error=f"docker inspect returned no usable container ID for {container_name}",
                )
            _deferred_rm = stopped_container_id  # removed later, after provenance

        options = ScrapeOptions(
            cell_km=plan.recovery_cell_km, depth=plan.depth,
            concurrency=plan.config["concurrency"],
            browser_pool_size=plan.config["browser_pool_size"],
            pages_per_browser=plan.config["pages_per_browser"],
            lang=plan.config["lang"], zoom=plan.config["zoom"],
            resume=True, image=plan.scraper_image, proxy_file=proxy_path,
        )
        options.validate()
        expected_inputs = expected_resume_input_ids(area, list(plan.queries), plan.recovery_cell_km)
        if len(expected_inputs) != (bin_record.planned_searches or 0):
            raise _ChildFailure(
                status="failed", process_exit=2,
                error=(
                    "live completion model disagrees with the plan's recorded per-bin "
                    "search count; plan/coder compatibility broken"
                ),
            )
        proxy_sha = plan.config.get("proxy_sha256")
        if proxy_path is not None:
            try:
                current = hashlib.sha256(proxy_path.read_bytes()).hexdigest()
            except OSError as exc:
                raise _ChildFailure(
                    status="failed", process_exit=1,
                    error=f"proxy file re-read failed: {exc}",
                ) from exc
            if current != proxy_sha:
                raise _ChildFailure(
                    status="failed", process_exit=2,
                    error="proxy file changed since verification; refusing to launch",
                )
        child_config = _recovery_child_config_json(plan, bin_record, proxy_sha, plan_sha256)
        bbox_json = json.dumps(bin_record.bbox.__dict__, sort_keys=True)
        queries_json = json.dumps(list(plan.queries), ensure_ascii=False)

        existing_run = None
        if mapping["run_id"] is not None:
            existing_run = conn.execute(
                "SELECT * FROM runs WHERE id = ?", (mapping["run_id"],)
            ).fetchone()
            if existing_run is None:
                raise _ChildFailure(
                    status="failed", process_exit=2,
                    error=f"mapped child run {mapping['run_id']} does not exist",
                )
            if (
                existing_run["config_json"] != child_config
                or existing_run["bbox_json"] != bbox_json
                or existing_run["raw_path"] != str(output_file)
            ):
                raise _ChildFailure(
                    status="failed", process_exit=2,
                    error="existing child run does not match the plan-derived configuration",
                )
            if existing_run["status"] == "complete":
                return "already_complete"

            # SR-F5-01: enforce exact query snapshot before any resume action
            _validate_query_snapshot(bin_dir, plan, is_started=True)

            # 429: Now remove the stopped owned container (deferred from
            # the inspection phase) after provenance and snapshot validation.
            if _deferred_rm:
                _remove_stopped_container(_deferred_rm, container_name)
                _deferred_rm = None  # consumed
                inspect = None  # container is now absent; sidecar path may proceed

            if inspect is None:
                try:
                    completed_ids = load_resume_completed_input_ids(
                        output_file, plan.scraper_image
                    )
                except (RuntimeError, OSError) as exc:
                    # E4-F04: invalid evidence is a child failure, not just
                    # a parent failure; preserve the child's exit_code.
                    child_mark_no_exit("failed", f"existing resume evidence is invalid: {exc}")
                    raise _ChildFailure(
                        status="failed", process_exit=1,
                        error=f"existing resume evidence is invalid: {exc}",
                    ) from exc
                comparison = expected_inputs.compare_completed(completed_ids)
                if comparison.unexpected:
                    child_mark_no_exit("failed", "existing resume evidence contains unexpected input IDs")
                    raise _ChildFailure(
                        status="failed", process_exit=1,
                        error="existing resume evidence contains unexpected input IDs",
                    )
                if comparison.missing == 0:
                    # V2-F05: actual progress (direct ingestion) begins now.
                    if mark_parent_running is not None:
                        mark_parent_running()
                    try:
                        recorded_exit = conn.execute(
                            "SELECT exit_code FROM runs WHERE id = ?",
                            (mapping["run_id"],),
                        ).fetchone()["exit_code"]
                        ingest_records(
                            conn, mapping["run_id"], iter_jsonl(output_file),
                            bbox=bin_record.bbox,
                            finalize_run=("complete", recorded_exit, None),
                        )
                    except KeyboardInterrupt:
                        child_mark_no_exit(
                            "interrupted", "recovery child interrupted during direct ingestion"
                        )
                        raise
                    except Exception as ingest_exc:
                        child_mark_no_exit(
                            "failed", f"direct-sidecar ingestion failed: {ingest_exc}"
                        )
                        raise _ChildFailure(
                            status="failed", process_exit=1,
                            error=f"direct-sidecar ingestion failed: {ingest_exc}",
                        ) from ingest_exc
                    return "complete"  # E3-F04: outer guard handles reporting
            # V2-F05: parent transitions to running only now, after lock/
            # container no-new-effect checks passed and real progress begins.
            if mark_parent_running is not None:
                mark_parent_running()
            conn.execute("BEGIN IMMEDIATE")
            try:
                # SR-I03: exit_code is the last observed scraper exit; it is
                # never cleared here and only replaced by a new scraper return.
                conn.execute(
                    "UPDATE runs SET status = 'running', finished_at = NULL, error = NULL "
                    "WHERE id = ?",
                    (mapping["run_id"],),
                )
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        else:
            # SR-F5-02: validate/repair unstarted snapshot (no child started yet)
            try:
                bin_dir.mkdir(parents=True, exist_ok=True)
                _validate_query_snapshot(bin_dir, plan, is_started=False)
            except OSError as exc:
                raise _ChildFailure(
                    status="failed", process_exit=1,
                    error=f"child bin directory creation failed: {exc}",
                ) from exc
            query_snapshot = bin_dir / "queries.txt"
            snapshot_bytes = recovery_query_snapshot_bytes(list(plan.queries))
            if query_snapshot.exists():
                try:
                    existing_bytes = query_snapshot.read_bytes()
                except OSError as exc:
                    raise _ChildFailure(
                        status="failed", process_exit=1,
                        error=f"child query snapshot read failed: {exc}",
                    ) from exc
                if existing_bytes != snapshot_bytes:
                    raise _ChildFailure(
                        status="none", process_exit=2,
                        error="existing child query snapshot does not match the plan queries",
                    )
            else:
                try:
                    with open(query_snapshot, "wb") as handle:
                        handle.write(snapshot_bytes)
                except OSError as exc:
                    raise _ChildFailure(
                        status="failed", process_exit=1,
                        error=f"child query snapshot write failed: {exc}",
                    ) from exc
            # RRI-F07: refuse to silently adopt orphan raw/resume evidence.
            orphan_raw = bin_dir / "results.jsonl"
            orphan_resume = bin_dir / "results.jsonl.resume.json"
            if orphan_raw.exists() or orphan_resume.exists():
                raise _ChildFailure(
                    status="none", process_exit=2,
                    error=(
                        "unstarted child bin directory contains pre-existing raw/resume "
                        "files; refusing to adopt untracked scraper evidence"
                    ),
                )
            collision = conn.execute(
                "SELECT id FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
            if collision is not None:
                raise _ChildFailure(
                    status="none", process_exit=2,
                    error=f"derived child run ID {run_id} collides with an unrelated run",
                )
            # Build the command without output-path mutation first; the host
            # file is prepared only after the child row exists.
            try:
                command = build_docker_command(
                    area=area, queries_file=bin_dir / "queries.txt",
                    output_file=output_file, options=options,
                    prepare_paths=False,
                    container_name=container_name,
                    labels={_RECOVERY_LABEL_PLAN: plan_sha256, _RECOVERY_LABEL_RUN: run_id},
                )
            except (OSError, RuntimeError, ValueError) as exc:
                raise _ChildFailure(
                    status="failed", process_exit=1,
                    error=f"child preparation failed: {exc}",
                ) from exc
            # V2-F05: parent transitions to running only now, after
            # lock/container/option/precision no-new-effect checks passed.
            if mark_parent_running is not None:
                mark_parent_running()
            conn.execute("BEGIN IMMEDIATE")
            try:
                create_recovery_child_run(
                    conn,
                    run_id=run_id,
                    area_name=area.name,
                    bbox_json=bbox_json,
                    cell_km=plan.recovery_cell_km,
                    depth=plan.depth,
                    queries_json=queries_json,
                    scraper_image=plan.scraper_image,
                    config_json=child_config,
                    raw_path=str(output_file),
                    plan_sha256=plan_sha256,
                    row=row,
                    column=column,
                )
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            # The deterministic child row is durable: from here on this
            # invocation owns it and outer failure handlers may repair it.
            if owned_child is not None:
                owned_child["run_id"] = run_id

            # V2-F01: the child row is durable; exclusively create the
            # host-owned results file. Failure leaves the row in place and
            # truthfully marks the child failed without fabricating an exit.
            try:
                fd = os.open(
                    output_file,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
                    0o600,
                )
                os.close(fd)
            except FileExistsError:
                if output_file.stat().st_size != 0:
                    child_mark_no_exit(
                        "failed",
                        f"child output file exists non-empty before first launch: {output_file}",
                    )
                    raise _ChildFailure(
                        status="failed", process_exit=1,
                        error="child output file exists non-empty before first launch",
                    )
            except OSError as open_exc:
                child_mark_no_exit("failed", f"child output creation failed: {open_exc}")
                raise _ChildFailure(
                    status="failed", process_exit=1,
                    error=f"child output creation failed: {open_exc}",
                ) from open_exc

        try:
            command = build_docker_command(
                area=area, queries_file=bin_dir / "queries.txt",
                output_file=output_file, options=options,
                container_name=container_name,
                labels={_RECOVERY_LABEL_PLAN: plan_sha256, _RECOVERY_LABEL_RUN: run_id},
            )
        except (OSError, RuntimeError) as exc:
            child_mark("failed", None, f"child preparation failed: {exc}")
            raise _ChildFailure(
                status="failed", process_exit=1, error=f"child preparation failed: {exc}"
            ) from exc

        try:
            print(command_for_display(command))
            scraper_exit = run_scraper(command)
        except KeyboardInterrupt:
            # Sara-side interrupt before any new scraper return: the child is
            # interrupted without inventing scraper-exit provenance.
            child_mark_no_exit("interrupted", "recovery child interrupted before scraper return")
            raise
        except (RuntimeError, OSError) as exc:
            child_mark_no_exit("failed", f"scraper launch failed: {exc}")
            raise _ChildFailure(
                status="failed", process_exit=1, error=f"scraper launch failed: {exc}"
            ) from exc
        # V2-F02: record every normally observed scraper exit (including
        # zero) before any completion evaluation, so runs.exit_code is
        # always the last observed scraper process exit code.
        record_child_exit(scraper_exit)

        if scraper_exit != 0:
            error = f"scraper failed with exit code {scraper_exit}"
            child_mark_no_exit("failed", error)
            raise _ChildFailure(status="failed", process_exit=1, error=error)

        try:
            completed_ids = load_resume_completed_input_ids(output_file, plan.scraper_image)
            comparison = expected_inputs.compare_completed(completed_ids)
        except KeyboardInterrupt:
            child_mark_no_exit("interrupted", "recovery child interrupted during completion verification")
            raise
        except RuntimeError as exc:
            error = f"completion verification failed after scraper exit 0: {exc}"
            child_mark("failed", 0, error)
            raise _ChildFailure(status="failed", process_exit=1, error=error) from exc

        if comparison.unexpected:
            error = (
                f"resume completion state does not match this child: "
                f"{comparison.unexpected} unexpected completed input(s)"
            )
            child_mark_no_exit("failed", error)
            raise _ChildFailure(status="failed", process_exit=1, error=error)
        if comparison.missing:
            error = (
                "scraper exited before all planned child searches completed: "
                f"completed {comparison.matched}/{len(expected_inputs)}"
            )
            child_mark_no_exit("interrupted", error)
            raise _ChildFailure(status="interrupted", process_exit=130, error=error)

        recorded_exit = conn.execute(
            "SELECT exit_code FROM runs WHERE id = ?", (run_id,)
        ).fetchone()["exit_code"]
        try:
            ingest_records(
                conn, run_id, iter_jsonl(output_file),
                bbox=bin_record.bbox,
                finalize_run=("complete", recorded_exit, None),
            )
        except KeyboardInterrupt:
            child_mark_no_exit("interrupted", "recovery child interrupted during ingestion")
            raise
        except Exception as exc:
            error = f"recovery child ingestion failed: {exc}"
            child_mark_no_exit("failed", error)
            raise _ChildFailure(status="failed", process_exit=1, error=error) from exc
        return "complete"
    finally:
        _safe_release(child_lock)

if __name__ == "__main__":
    raise SystemExit(main())
