from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import socket
import sqlite3
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
    run_scraper,
)
from .recovery import (
    COMPLETION_CLAIM,
    RecoveryPolicy,
    build_recovery_plan,
    project_source_config,
    serialize_recovery_plan,
)
from .storage import connect, connect_readonly, ingest_records, iter_jsonl, utc_now

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
        row = conn.execute(
            """
            SELECT id, area_name, bbox_json, cell_km, depth, queries_json,
                   scraper_image, config_json, status, started_at, finished_at,
                   exit_code, unique_seen
            FROM runs WHERE id = ?
            """,
            (run_id,),
        ).fetchone()
        if row is None:
            return reject(f"run_id {run_id} does not exist")
        if row["status"] != "complete":
            return reject(f"run_id {run_id} is not complete (status={row['status']!r})")
        if not isinstance(row["finished_at"], str) or not row["finished_at"].strip():
            return reject("complete run has an invalid finished_at timestamp")
        if not isinstance(row["started_at"], str) or not row["started_at"].strip():
            return reject("run has an invalid started_at timestamp")
        if row["exit_code"] is not None and (
            isinstance(row["exit_code"], bool) or not isinstance(row["exit_code"], int)
        ):
            return reject("run has an invalid recorded exit_code")

        if not isinstance(row["bbox_json"], str):
            return reject("run bbox_json is not stored as text")
        try:
            bbox = BoundingBox(**json.loads(row["bbox_json"], parse_constant=_reject_json_constant))
            bbox.validate()
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            return reject(f"run has invalid recorded bbox: {exc}")

        if not isinstance(row["queries_json"], str):
            return reject("run queries_json is not stored as text")
        try:
            queries = json.loads(row["queries_json"], parse_constant=_reject_json_constant)
        except (TypeError, ValueError) as exc:
            return reject(f"run has invalid recorded queries: {exc}")
        if (
            not isinstance(queries, list)
            or not queries
            or not all(isinstance(query, str) and query for query in queries)
        ):
            return reject("run has invalid recorded query configuration")

        config_raw = row["config_json"]
        if not isinstance(config_raw, str) or not config_raw:
            return reject("run has no recorded configuration text")
        try:
            config = json.loads(config_raw, parse_constant=_reject_json_constant)
        except (TypeError, ValueError) as exc:
            return reject(f"run has invalid recorded configuration: {exc}")
        if not isinstance(config, dict):
            return reject("run configuration is not a JSON object")
        if config.get("resume") is not True:
            return reject("recovery planning requires a run recorded with resume=true")
        if config.get("strict_bounds") is not True:
            return reject("recovery planning requires a run recorded with strict_bounds=true")

        source_cell_km = row["cell_km"]
        if (
            isinstance(source_cell_km, bool)
            or not isinstance(source_cell_km, (int, float))
            or not math.isfinite(source_cell_km)
            or source_cell_km <= 0
        ):
            return reject("run has an invalid recorded cell size")
        if args.recovery_cell_km >= source_cell_km:
            return reject("recovery_cell_km must be strictly finer than the source cell size")

        # Cross-check the denormalized run columns against the recorded
        # configuration so a contradictory or corrupted run row cannot
        # produce a plan whose visible fields and configuration hash refer
        # to different crawl configurations.
        def config_mismatch(field: str) -> int:
            return reject(
                f"run {field} disagrees with the recorded configuration; "
                "the run row is internally inconsistent"
            )

        if not isinstance(config.get("area_name"), str) or config["area_name"] != row["area_name"]:
            return config_mismatch("area_name")
        if not isinstance(config.get("image"), str) or config["image"] != row["scraper_image"]:
            return config_mismatch("scraper_image")
        recorded_bbox = config.get("bbox")
        if not isinstance(recorded_bbox, dict):
            return config_mismatch("bbox")
        for key, value in (
            ("min_lat", bbox.min_lat),
            ("min_lon", bbox.min_lon),
            ("max_lat", bbox.max_lat),
            ("max_lon", bbox.max_lon),
        ):
            recorded = recorded_bbox.get(key)
            # Plain numeric equality: no float() coercion, so arbitrarily
            # large JSON integers cannot raise OverflowError, and NaN or
            # infinity simply compare unequal and are rejected here.
            if (
                isinstance(recorded, bool)
                or not isinstance(recorded, (int, float))
                or recorded != value
            ):
                return config_mismatch("bbox")
        if config.get("queries") != queries:
            return config_mismatch("queries")
        recorded_cell = config.get("cell_km")
        if (
            isinstance(recorded_cell, bool)
            or not isinstance(recorded_cell, (int, float))
            or recorded_cell != source_cell_km
        ):
            return config_mismatch("cell_km")
        recorded_depth = config.get("depth")
        if (
            isinstance(recorded_depth, bool)
            or not isinstance(recorded_depth, int)
            or not isinstance(row["depth"], int)
            or recorded_depth != row["depth"]
        ):
            return config_mismatch("depth")

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
    except (FileNotFoundError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
