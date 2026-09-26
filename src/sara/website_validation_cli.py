from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Sequence

from .dossier import build_business_dossier
from .storage import connect_readonly
from .website_validation import (
    OperationalValidationError,
    _parser,
    _validate_arguments,
    run_validation,
)


def _legacy_schema_snapshot(conn: sqlite3.Connection) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for table in (
        "runs",
        "businesses",
        "run_businesses",
        "recovery_executions",
        "recovery_execution_bins",
    ):
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if exists is None:
            result[table] = {"present": False, "columns": [], "foreign_keys": []}
            continue
        columns = [
            {
                "cid": int(row[0]),
                "name": str(row[1]),
                "type": str(row[2]),
                "notnull": int(row[3]),
                "default": row[4],
                "pk": int(row[5]),
            }
            for row in conn.execute(f'PRAGMA table_info("{table}")')
        ]
        foreign_keys = sorted(
            (
                str(row[2]),
                str(row[3]),
                str(row[4]),
                str(row[5]),
                str(row[6]),
            )
            for row in conn.execute(f'PRAGMA foreign_key_list("{table}")')
        )
        result[table] = {
            "present": True,
            "columns": columns,
            "foreign_keys": foreign_keys,
        }
    return result


def _legacy_schema_check(source_db: str | Path, working_db: str | Path) -> dict[str, Any]:
    source = connect_readonly(source_db)
    working = connect_readonly(working_db)
    try:
        before = _legacy_schema_snapshot(source)
        after = _legacy_schema_snapshot(working)
    finally:
        working.close()
        source.close()
    changed = {
        table: {"source": before[table], "working_copy": after[table]}
        for table in before
        if before[table] != after[table]
    }
    return {
        "name": "legacy_collection_recovery_schemas_unchanged",
        "passed": not changed,
        "details": {"changed_tables": changed},
    }


def _selected_business_ids(report: dict[str, Any]) -> list[int]:
    samples = report["representative_samples"]
    result = {int(value) for value in samples["single_location_business_ids"]}
    for group in samples["multi_branch_groups"]:
        result.update(int(value) for value in group)
    return sorted(result)


def _representative_dossier_check(
    working_db: str | Path,
    business_ids: Sequence[int],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    conn = connect_readonly(working_db)
    summaries: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    try:
        conn.execute("BEGIN")
        for business_id in business_ids:
            try:
                dossier = build_business_dossier(conn, business_id=business_id)
            except BaseException as exc:
                failure = {
                    "business_id": business_id,
                    "error": f"{type(exc).__name__}: {exc}",
                }
                failures.append(failure)
                summaries.append(failure)
                continue
            preview = dossier["dossier_status"]["read_only_preview"]
            persisted = dossier["dossier_status"]["persisted_current_policy"]
            integrity_issues = list(dossier["integrity_issues"])
            if persisted is not None:
                integrity_issues.extend(persisted.get("integrity_issues", []))
            item = {
                "business_id": business_id,
                "business_entity_id": dossier["business_entity"]["id"],
                "location_count": len(dossier["locations"]),
                "fact_count": len(dossier["facts"]),
                "evidence_count": len(dossier["evidence"]),
                "unknown_count": len(dossier["unknowns"]),
                "preview_analysis_ready": bool(preview["analysis_ready"]),
                "preview_domains": [
                    {
                        "domain": domain["domain"],
                        "state": domain["state"],
                        "fact_count": domain["fact_count"],
                        "unresolved_count": domain["unresolved_count"],
                    }
                    for domain in preview["domains"]
                ],
                "persisted_analysis_ready": (
                    None if persisted is None else bool(persisted["analysis_ready"])
                ),
                "integrity_issues": integrity_issues,
            }
            summaries.append(item)
            if item["preview_analysis_ready"] or integrity_issues:
                failures.append(item)
    finally:
        conn.close()
    return (
        {
            "name": "representative_dossiers_generate_without_integrity_issues",
            "passed": bool(business_ids) and not failures,
            "details": {
                "business_ids": list(business_ids),
                "failures": failures,
                "preview_rule": "phase5 read-only preview must never mark analysis_ready",
            },
        },
        summaries,
    )


def _write_report(report: dict[str, Any]) -> None:
    report_path = Path(report["working_db"]).parent / "report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def run_validation_gate(**kwargs: Any) -> dict[str, Any]:
    report = run_validation(**kwargs)
    schema_check = _legacy_schema_check(report["source_db"], report["working_db"])
    dossier_check, dossier_summaries = _representative_dossier_check(
        report["working_db"], _selected_business_ids(report)
    )
    report["checks"].extend([schema_check, dossier_check])
    report["representative_dossiers"] = dossier_summaries
    report["production_ready"] = all(bool(check["passed"]) for check in report["checks"])
    _write_report(report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        _validate_arguments(args)
        report = run_validation_gate(
            source_db=args.source_db,
            workspace=args.workspace,
            single_location_business_ids=args.single_location_business_id,
            multi_branch_groups=args.multi_branch_group,
            merge_pair=args.merge_pair,
            passes=args.passes,
        )
    except (OperationalValidationError, FileNotFoundError, sqlite3.Error, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            report,
            ensure_ascii=False,
            sort_keys=True,
            indent=2 if args.pretty else None,
        )
    )
    return 0 if report["production_ready"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
