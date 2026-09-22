from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

from .config import load_area, load_queries
from .grid import estimate_grid
from .scraper import DEFAULT_IMAGE, ScrapeOptions, build_docker_command, command_for_display, run_scraper
from .storage import connect, ingest_records, iter_jsonl, utc_now


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
    collect.add_argument("--depth", type=int, default=5)
    collect.add_argument("--concurrency", type=int, default=4)
    collect.add_argument("--browser-pool-size", type=int, default=1)
    collect.add_argument("--pages-per-browser", type=int, default=4)
    collect.add_argument("--lang", default="en")
    collect.add_argument("--zoom", type=int, default=15)
    collect.add_argument("--image", default=DEFAULT_IMAGE)
    collect.add_argument("--proxy-file")
    collect.add_argument("--output-dir", default="output")
    collect.add_argument("--no-resume", action="store_true")
    collect.add_argument("--dry-run", action="store_true")

    ingest = sub.add_parser("ingest", help="Ingest an existing scraper JSONL file")
    ingest.add_argument("--run-id", required=True)
    ingest.add_argument("--file", required=True)

    stats = sub.add_parser("stats", help="Show run and canonical business counts")
    stats.add_argument("--limit", type=int, default=20)
    return parser


def _ensure_run(conn, *, run_id: str, area, queries, cell_km: float, depth: int, image: str, raw_path: str, status: str):
    bbox_json = json.dumps(area.bbox.__dict__, sort_keys=True)
    queries_json = json.dumps(queries, ensure_ascii=False)
    existing = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    if existing is None:
        conn.execute(
            """
            INSERT INTO runs(
                id, area_name, bbox_json, cell_km, depth, queries_json, scraper_image,
                raw_path, status, started_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (run_id, area.name, bbox_json, cell_km, depth, queries_json, image, raw_path, status, utc_now()),
        )
    else:
        expected = (area.name, bbox_json, cell_km, depth, queries_json, image, raw_path)
        actual = tuple(existing[key] for key in (
            "area_name", "bbox_json", "cell_km", "depth", "queries_json", "scraper_image", "raw_path"
        ))
        if actual != expected:
            raise ValueError(f"run_id {run_id} exists with different crawl configuration")
        conn.execute(
            "UPDATE runs SET status = ?, finished_at = NULL, exit_code = NULL WHERE id = ?",
            (status, run_id),
        )
    conn.commit()


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
    run_id = args.run_id or uuid.uuid4().hex[:12]
    output_dir = Path(args.output_dir) / run_id
    output_file = output_dir / "results.jsonl"
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
        proxy_file=Path(args.proxy_file) if args.proxy_file else None,
    )
    command = build_docker_command(
        area=area,
        queries_file=Path(args.queries),
        output_file=output_file,
        options=options,
    )

    estimate = estimate_grid(area.bbox, args.cell_km, len(queries))
    print(f"run_id={run_id} cells={estimate.cells} planned_searches={estimate.searches}")
    print(command_for_display(command))
    if args.dry_run:
        return 0

    conn = connect(args.db)
    existing = conn.execute("SELECT status FROM runs WHERE id = ?", (run_id,)).fetchone()
    if existing is not None and existing["status"] == "complete":
        print(f"run_id {run_id} is already complete; choose a new run ID", file=sys.stderr)
        return 2
    _ensure_run(
        conn,
        run_id=run_id,
        area=area,
        queries=queries,
        cell_km=args.cell_km,
        depth=args.depth,
        image=args.image,
        raw_path=str(output_file),
        status="running",
    )

    exit_code = run_scraper(command)
    if exit_code != 0:
        conn.execute(
            "UPDATE runs SET status = 'failed', finished_at = ?, exit_code = ? WHERE id = ?",
            (utc_now(), exit_code, run_id),
        )
        conn.commit()
        print(f"scraper failed with exit code {exit_code}", file=sys.stderr)
        return exit_code

    stats = ingest_records(conn, run_id, iter_jsonl(output_file))
    conn.execute(
        "UPDATE runs SET status = 'complete', finished_at = ?, exit_code = 0 WHERE id = ?",
        (utc_now(), run_id),
    )
    conn.commit()
    print(f"raw={stats.raw_records} unique_seen={stats.unique_seen} new_businesses={stats.new_businesses}")
    return 0


def cmd_ingest(args) -> int:
    conn = connect(args.db)
    row = conn.execute("SELECT id FROM runs WHERE id = ?", (args.run_id,)).fetchone()
    if row is None:
        print("run_id does not exist; use collect to create tracked runs", file=sys.stderr)
        return 2
    stats = ingest_records(conn, args.run_id, iter_jsonl(args.file))
    print(f"raw={stats.raw_records} unique_seen={stats.unique_seen} new_businesses={stats.new_businesses}")
    return 0


def cmd_stats(args) -> int:
    conn = connect(args.db)
    total = conn.execute("SELECT COUNT(*) AS n FROM businesses").fetchone()["n"]
    print(f"canonical_businesses={total}")
    rows = conn.execute(
        """
        SELECT id, area_name, cell_km, depth, status, raw_records, unique_seen,
               new_businesses, started_at, finished_at
        FROM runs ORDER BY started_at DESC LIMIT ?
        """,
        (args.limit,),
    ).fetchall()
    for row in rows:
        print(dict(row))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "plan":
        return cmd_plan(args)
    if args.command == "collect":
        return cmd_collect(args)
    if args.command == "ingest":
        return cmd_ingest(args)
    if args.command == "stats":
        return cmd_stats(args)
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
