from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import socket
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
from .storage import connect, ingest_records, iter_jsonl, utc_now

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
    return parser


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
        # ERROR_INVALID_PARAMETER is the normal response for a PID that no longer
        # exists. For access-denied/other errors, conservatively treat the process
        # as alive so we never clear a potentially live writer lock.
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
            except Exception as exc:
                error = f"completion verification failed after scraper exit 0: {exc}"
                _mark_run(conn, run_id, status="failed", exit_code=scraper_exit_code, error=error)
                print(error, file=sys.stderr)
                return 1

            unexpected_inputs = completed_inputs - expected_inputs
            if unexpected_inputs:
                error = (
                    "resume completion state does not match this run: "
                    f"{len(unexpected_inputs)} unexpected completed input(s)"
                )
                _mark_run(conn, run_id, status="failed", exit_code=scraper_exit_code, error=error)
                print(error, file=sys.stderr)
                return 1

            missing_inputs = expected_inputs - completed_inputs
            if missing_inputs:
                error = (
                    "scraper exited before all planned searches completed: "
                    f"completed {len(completed_inputs)}/{len(expected_inputs)}"
                )
                _mark_run(conn, run_id, status="interrupted", exit_code=scraper_exit_code, error=error)
                print(error, file=sys.stderr)
                return 130

            stats = ingest_records(
                conn,
                run_id,
                iter_jsonl(output_file),
                bbox=area.bbox if strict_bounds else None,
            )
            _mark_run(conn, run_id, status="complete", exit_code=scraper_exit_code, error=None)
        except KeyboardInterrupt:
            recorded_exit = scraper_exit_code if scraper_exit_code is not None else 130
            _mark_run(conn, run_id, status="interrupted", exit_code=recorded_exit, error="interrupted")
            print("collection interrupted", file=sys.stderr)
            return 130
        except Exception as exc:
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
        except (OSError, RuntimeError, ValueError) as exc:
            print(f"ingest completion verification failed: {exc}", file=sys.stderr)
            return 2

        unexpected_inputs = completed_inputs - expected_inputs
        missing_inputs = expected_inputs - completed_inputs
        if unexpected_inputs or missing_inputs:
            print(
                "ingest requires verified complete crawl evidence: "
                f"completed {len(completed_inputs)}/{len(expected_inputs)}, "
                f"unexpected={len(unexpected_inputs)}",
                file=sys.stderr,
            )
            return 2

        try:
            stats = ingest_records(
                conn,
                run_id,
                iter_jsonl(supplied),
                bbox=bbox if strict_bounds else None,
            )
        except Exception as exc:
            print(f"ingest failed: {exc}", file=sys.stderr)
            return 1

        if row["status"] != "complete":
            _mark_run(conn, run_id, status="complete", exit_code=row["exit_code"], error=None)
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
    except (FileNotFoundError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
