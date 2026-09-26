from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

from .dossier import build_business_dossier
from .maps_sync import sync_maps_business_understanding
from .storage import connect_existing, connect_readonly, upsert_business
from .website.model import (
    COLLECTOR_NAME,
    OFFICIAL_WEB_SOURCE_ID,
    CrawlConfig,
)
from .website.surface import collect_official_website
from .website_validation_merge_probe import run_controlled_merge_survival_probe
from .website_validation import (
    REPORT_SCHEMA,
    CheckResult,
    OperationalValidationError,
    _bridge_record,
    _business_exists,
    _evidence_ids_for_subjects,
    _parser,
    _subject_ids_for_business,
    _validate_arguments,
    backup_database,
    backup_existing_database,
    bootstrap_understanding,
    compare_legacy_snapshots,
    legacy_snapshot,
    validate_dossier_readiness_integrity,
    validate_fact_provenance,
    validate_multi_branch_groups,
    validate_one_to_one_maps_anchors,
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _legacy_schema_snapshot(conn: sqlite3.Connection) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for table in (
        "runs",
        "businesses",
        "run_businesses",
        "recovery_executions",
        "recovery_execution_bins",
    ):
        table_row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if table_row is None:
            result[table] = {
                "present": False,
                "table_sql": None,
                "columns": [],
                "foreign_keys": [],
                "indexes": [],
                "triggers": [],
            }
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
        indexes: list[dict[str, Any]] = []
        for row in conn.execute(f'PRAGMA index_list("{table}")'):
            name = str(row[1])
            sql_row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' AND name=?", (name,)
            ).fetchone()
            indexes.append(
                {
                    "name": name,
                    "unique": int(row[2]),
                    "origin": str(row[3]),
                    "partial": int(row[4]),
                    "columns": [
                        str(info[2])
                        for info in conn.execute(f'PRAGMA index_info("{name}")')
                    ],
                    "sql": None if sql_row is None else sql_row[0],
                }
            )
        indexes.sort(key=lambda item: item["name"])
        triggers = [
            {"name": str(row[0]), "sql": row[1]}
            for row in conn.execute(
                "SELECT name,sql FROM sqlite_master "
                "WHERE type='trigger' AND tbl_name=? ORDER BY name",
                (table,),
            )
        ]
        result[table] = {
            "present": True,
            "table_sql": table_row[0],
            "columns": columns,
            "foreign_keys": foreign_keys,
            "indexes": indexes,
            "triggers": triggers,
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


def _selected_business_ids(
    single_location_business_ids: Sequence[int],
    multi_branch_groups: Sequence[Sequence[int]],
) -> list[int]:
    result = {int(value) for value in single_location_business_ids}
    for group in multi_branch_groups:
        result.update(int(value) for value in group)
    return sorted(result)


def _website_acquisition_business_ids(
    single_location_business_ids: Sequence[int],
) -> list[int]:
    return sorted({int(value) for value in single_location_business_ids})


def _run_acquisition_samples(
    conn: sqlite3.Connection,
    *,
    evidence_root: Path,
    business_ids: Sequence[int],
    passes: int,
    config: CrawlConfig,
    collector: Callable[..., Any] = collect_official_website,
) -> tuple[CheckResult, list[dict[str, Any]]]:
    results: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    complete_count = 0
    for business_id in business_ids:
        if not _business_exists(conn, business_id):
            failure = {"business_id": business_id, "error": "business does not exist"}
            failures.append(failure)
            results.append(failure)
            continue
        for pass_number in range(1, passes + 1):
            try:
                stats = collector(
                    conn,
                    evidence_root=evidence_root,
                    business_id=business_id,
                    config=config,
                )
            except Exception as exc:
                failure = {
                    "business_id": business_id,
                    "pass": pass_number,
                    "error": f"{type(exc).__name__}: {exc}",
                }
                failures.append(failure)
                results.append(failure)
                break
            item = asdict(stats) if hasattr(stats, "__dataclass_fields__") else dict(stats)
            item["business_id"] = business_id
            item["pass"] = pass_number
            results.append(item)
            if item.get("status") == "complete":
                complete_count += 1
            elif item.get("status") not in {"partial", "blocked"}:
                failures.append(
                    {
                        "business_id": business_id,
                        "pass": pass_number,
                        "error": f"unexpected terminal status {item.get('status')!r}",
                    }
                )
                break
    if results and complete_count == 0:
        failures.append({"error": "no representative acquisition completed successfully"})
    return (
        CheckResult(
            name="representative_website_acquisitions_finish_with_at_least_one_complete_run",
            passed=bool(business_ids) and not failures,
            details={
                "business_ids": list(business_ids),
                "passes_per_business": passes,
                "complete_runs": complete_count,
                "failures": failures,
            },
        ),
        results,
    )


def _validation_session_ids(acquisitions: Sequence[dict[str, Any]]) -> list[str]:
    return sorted(
        {
            str(item["session_id"])
            for item in acquisitions
            if isinstance(item.get("session_id"), str) and item["session_id"]
        }
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _website_session_integrity_check(
    conn: sqlite3.Connection,
    *,
    session_ids: Sequence[str],
    evidence_root: Path,
) -> CheckResult:
    failures: list[dict[str, Any]] = []
    checked_artifacts = 0
    root = evidence_root.resolve()
    for session_id in session_ids:
        row = conn.execute(
            "SELECT id,target_subject_id,source_id,collector_name,config_json,config_hash,status,"
            "finished_at,evidence_count,observation_count FROM acquisition_sessions WHERE id=?",
            (session_id,),
        ).fetchone()
        if row is None:
            failures.append({"session_id": session_id, "error": "session missing"})
            continue
        actual_evidence = int(
            conn.execute(
                "SELECT COUNT(*) FROM evidence_items WHERE acquisition_session_id=?",
                (session_id,),
            ).fetchone()[0]
        )
        actual_observations = int(
            conn.execute(
                "SELECT COUNT(*) FROM observations o JOIN evidence_items e ON e.id=o.evidence_id "
                "WHERE e.acquisition_session_id=?",
                (session_id,),
            ).fetchone()[0]
        )
        session_failures: list[str] = []
        config_json = str(row["config_json"])
        if str(row["config_hash"]) != hashlib.sha256(config_json.encode("utf-8")).hexdigest():
            session_failures.append("config_hash mismatch")
        if row["source_id"] != OFFICIAL_WEB_SOURCE_ID or row["collector_name"] != COLLECTOR_NAME:
            session_failures.append("source/collector identity mismatch")
        if row["target_subject_id"] in (None, ""):
            session_failures.append("target subject missing")
        if str(row["status"]) in {"planned", "running", "cancelled"} or row["finished_at"] is None:
            session_failures.append(f"non-terminal session lifecycle: {row['status']!r}")
        if int(row["evidence_count"]) != actual_evidence:
            session_failures.append(
                f"evidence count mismatch: stored={row['evidence_count']} actual={actual_evidence}"
            )
        if int(row["observation_count"]) != actual_observations:
            session_failures.append(
                "observation count mismatch: "
                f"stored={row['observation_count']} actual={actual_observations}"
            )
        for evidence in conn.execute(
            "SELECT id,status,content_sha256,artifact_ref FROM evidence_items "
            "WHERE acquisition_session_id=? ORDER BY id",
            (session_id,),
        ):
            evidence_id = str(evidence["id"])
            artifact_ref = evidence["artifact_ref"]
            content_sha256 = evidence["content_sha256"]
            if evidence["status"] != "usable":
                session_failures.append(
                    f"evidence {evidence_id} is not usable: {evidence['status']!r}"
                )
                continue
            if not isinstance(artifact_ref, str) or not artifact_ref:
                session_failures.append(f"evidence {evidence_id} has no artifact_ref")
                continue
            if not isinstance(content_sha256, str) or len(content_sha256) != 64:
                session_failures.append(f"evidence {evidence_id} has invalid content hash")
                continue
            artifact = Path(artifact_ref).resolve()
            if not artifact.is_relative_to(root):
                session_failures.append(
                    f"evidence {evidence_id} artifact is outside validation evidence root"
                )
                continue
            if not artifact.is_file():
                session_failures.append(f"evidence {evidence_id} artifact is missing")
                continue
            checked_artifacts += 1
            actual_hash = _sha256_file(artifact)
            if actual_hash != content_sha256:
                session_failures.append(
                    f"evidence {evidence_id} artifact hash mismatch"
                )
        if session_failures:
            failures.append({"session_id": session_id, "errors": session_failures})
    return CheckResult(
        name="validation_website_sessions_and_artifacts_are_durable",
        passed=bool(session_ids) and not failures,
        details={
            "session_ids": list(session_ids),
            "artifacts_verified": checked_artifacts,
            "failures": failures,
        },
    )


def _run_merge_survival_probe(
    source_db: Path,
    probe_db: Path,
    merge_pair: tuple[int, int] | None,
) -> CheckResult:
    if merge_pair is None:
        return CheckResult(
            name="maps_identity_merge_preserves_understanding_evidence",
            passed=False,
            details={"error": "no merge pair configured"},
        )
    backup_existing_database(source_db, probe_db)
    conn = connect_existing(probe_db)
    try:
        first_id, second_id = merge_pair
        first = conn.execute("SELECT * FROM businesses WHERE id=?", (first_id,)).fetchone()
        second = conn.execute("SELECT * FROM businesses WHERE id=?", (second_id,)).fetchone()
        if first is None or second is None:
            raise OperationalValidationError(
                f"merge pair businesses do not both exist: {merge_pair!r}"
            )
        first_subjects = _subject_ids_for_business(conn, first_id)
        second_subjects = _subject_ids_for_business(conn, second_id)
        durable_evidence = _evidence_ids_for_subjects(
            conn, (*first_subjects, *second_subjects)
        )
        if not durable_evidence:
            raise OperationalValidationError(
                "merge pair has no subject-linked evidence to prove survival"
            )
        bridge = _bridge_record(first, second)
        run_id = str(first["last_run_id"] or second["last_run_id"] or "")
        if not run_id:
            raise OperationalValidationError("merge pair has no usable run provenance")
        run = conn.execute("SELECT started_at FROM runs WHERE id=?", (run_id,)).fetchone()
        if run is None:
            raise OperationalValidationError(
                f"merge probe run {run_id!r} does not exist"
            )
        conn.execute("BEGIN IMMEDIATE")
        survivor_id, _created = upsert_business(
            conn,
            run_id,
            bridge,
            run_started_at=str(run["started_at"]),
        )
        conn.commit()
        sync_maps_business_understanding(conn)
        placeholders = ",".join("?" for _ in durable_evidence)
        remaining = {
            str(row[0])
            for row in conn.execute(
                f"SELECT id FROM evidence_items WHERE id IN ({placeholders})",
                tuple(sorted(durable_evidence)),
            )
        }
        observations_with_missing_evidence = int(
            conn.execute(
                "SELECT COUNT(*) FROM observations o "
                "LEFT JOIN evidence_items e ON e.id=o.evidence_id "
                "WHERE e.id IS NULL"
            ).fetchone()[0]
        )
        original_remaining = int(
            conn.execute(
                "SELECT COUNT(*) FROM businesses WHERE id IN (?,?)",
                (first_id, second_id),
            ).fetchone()[0]
        )
        anchor_check = validate_one_to_one_maps_anchors(conn)
        foreign_key_rows = [tuple(row) for row in conn.execute("PRAGMA foreign_key_check")]
        passed = (
            original_remaining == 1
            and survivor_id in {first_id, second_id}
            and remaining == durable_evidence
            and observations_with_missing_evidence == 0
            and anchor_check.passed
            and not foreign_key_rows
        )
        return CheckResult(
            name="maps_identity_merge_preserves_understanding_evidence",
            passed=passed,
            details={
                "merge_pair": [first_id, second_id],
                "survivor_business_id": survivor_id,
                "durable_evidence_count_before": len(durable_evidence),
                "durable_evidence_count_after": len(remaining),
                "missing_evidence_ids": sorted(durable_evidence - remaining),
                "observations_with_missing_evidence": observations_with_missing_evidence,
                "post_merge_anchor_check": asdict(anchor_check),
                "post_merge_foreign_key_violations": foreign_key_rows,
            },
        )
    except Exception as exc:
        if conn.in_transaction:
            conn.rollback()
        return CheckResult(
            name="maps_identity_merge_preserves_understanding_evidence",
            passed=False,
            details={
                "merge_pair": list(merge_pair),
                "error": f"{type(exc).__name__}: {exc}",
            },
        )
    finally:
        conn.close()


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
            except Exception as exc:
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
                "maps_business_count": len(dossier["maps_businesses"]),
                "location_count": len(dossier["locations"]),
                "current_location_count": sum(
                    1 for location in dossier["locations"] if location["current_for_entity"]
                ),
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


def _single_location_check(
    dossier_summaries: Sequence[dict[str, Any]],
    business_ids: Sequence[int],
) -> dict[str, Any]:
    by_business = {
        int(item["business_id"]): item
        for item in dossier_summaries
        if "business_id" in item
    }
    failures: list[dict[str, Any]] = []
    for business_id in business_ids:
        item = by_business.get(int(business_id))
        if item is None or "maps_business_count" not in item:
            failures.append({"business_id": int(business_id), "error": "dossier unavailable"})
            continue
        if int(item["maps_business_count"]) != 1 or int(item["current_location_count"]) != 1:
            failures.append(
                {
                    "business_id": int(business_id),
                    "maps_business_count": int(item["maps_business_count"]),
                    "current_location_count": int(item["current_location_count"]),
                }
            )
    return {
        "name": "single_location_samples_have_one_current_location",
        "passed": bool(business_ids) and not failures,
        "details": {
            "business_ids": [int(value) for value in business_ids],
            "failures": failures,
        },
    }


def _write_report(report: dict[str, Any]) -> None:
    report_path = Path(report["working_db"]).parent / "report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _write_fatal_report(args: Any, exc: Exception, *, allow_nonempty: bool) -> None:
    workspace = Path(args.workspace)
    if workspace.exists() and any(workspace.iterdir()) and not allow_nonempty:
        return
    workspace.mkdir(parents=True, exist_ok=True)
    report = {
        "schema": REPORT_SCHEMA,
        "generated_at": _utc_now(),
        "source_db": str(Path(args.source_db)),
        "working_db": str(workspace / "validation.sqlite"),
        "merge_probe_db": str(workspace / "merge-probe.sqlite"),
        "evidence_root": str(workspace / "evidence"),
        "bootstrap": {},
        "representative_samples": {
            "single_location_business_ids": [int(v) for v in args.single_location_business_id],
            "multi_branch_groups": [list(group) for group in args.multi_branch_group],
            "merge_pair": None if args.merge_pair is None else list(args.merge_pair),
            "merge_probe_business_id": getattr(args, "merge_probe_business_id", None),
            "passes_per_business": args.passes,
        },
        "acquisitions": [],
        "checks": [],
        "fatal_error": f"{type(exc).__name__}: {exc}",
        "production_ready": False,
    }
    (workspace / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def run_validation_gate(
    *,
    source_db: str | Path,
    workspace: str | Path,
    single_location_business_ids: Sequence[int],
    multi_branch_groups: Sequence[Sequence[int]],
    merge_pair: tuple[int, int] | None,
    merge_probe_business_id: int | None = None,
    passes: int = 2,
    config: CrawlConfig | None = None,
    collector: Callable[..., Any] = collect_official_website,
) -> dict[str, Any]:
    workspace_path = Path(workspace)
    workspace_path.mkdir(parents=True, exist_ok=True)
    working_db = workspace_path / "validation.sqlite"
    probe_db = workspace_path / "merge-probe.sqlite"
    evidence_root = workspace_path / "evidence"

    source_conn = connect_readonly(source_db)
    try:
        source_snapshot = legacy_snapshot(source_conn)
    finally:
        source_conn.close()

    backup_database(source_db, working_db)
    conn = connect_existing(working_db)
    checks: list[CheckResult] = []
    acquisitions: list[dict[str, Any]] = []
    bootstrap: dict[str, Any] = {}
    representative_selected = _selected_business_ids(
        single_location_business_ids, multi_branch_groups
    )
    acquisition_selected = _website_acquisition_business_ids(
        single_location_business_ids
    )
    try:
        copy_snapshot = legacy_snapshot(conn)
        checks.append(
            CheckResult(
                name="sqlite_backup_matches_source_logically",
                passed=copy_snapshot == source_snapshot,
                details={"source": source_snapshot, "copy": copy_snapshot},
            )
        )
        bootstrap = bootstrap_understanding(conn)
        checks.append(compare_legacy_snapshots(source_snapshot, legacy_snapshot(conn)))
        checks.append(validate_one_to_one_maps_anchors(conn))
        checks.append(validate_multi_branch_groups(conn, multi_branch_groups))
        acquisition_check, acquisitions = _run_acquisition_samples(
            conn,
            evidence_root=evidence_root,
            business_ids=acquisition_selected,
            passes=passes,
            config=config or CrawlConfig(),
            collector=collector,
        )
        checks.append(acquisition_check)
        checks.append(compare_legacy_snapshots(source_snapshot, legacy_snapshot(conn)))
        checks.append(validate_one_to_one_maps_anchors(conn))
        checks.append(validate_multi_branch_groups(conn, multi_branch_groups))
        checks.append(validate_fact_provenance(conn))
        checks.append(validate_dossier_readiness_integrity(conn))
        checks.append(
            _website_session_integrity_check(
                conn,
                session_ids=_validation_session_ids(acquisitions),
                evidence_root=evidence_root,
            )
        )
        foreign_key_rows = [tuple(row) for row in conn.execute("PRAGMA foreign_key_check")]
        checks.append(
            CheckResult(
                name="working_copy_foreign_keys_clean",
                passed=not foreign_key_rows,
                details={"violations": foreign_key_rows},
            )
        )
    finally:
        conn.close()

    if merge_probe_business_id is not None:
        checks.append(
            run_controlled_merge_survival_probe(
                Path(source_db), probe_db, merge_probe_business_id
            )
        )
    else:
        checks.append(_run_merge_survival_probe(working_db, probe_db, merge_pair))
    schema_check = _legacy_schema_check(source_db, working_db)
    dossier_check, dossier_summaries = _representative_dossier_check(
        working_db, representative_selected
    )
    single_location_check = _single_location_check(
        dossier_summaries, single_location_business_ids
    )
    report_checks = [asdict(check) for check in checks]
    report_checks.extend([schema_check, dossier_check, single_location_check])
    report = {
        "schema": REPORT_SCHEMA,
        "generated_at": _utc_now(),
        "source_db": str(Path(source_db)),
        "working_db": str(working_db),
        "merge_probe_db": str(probe_db),
        "evidence_root": str(evidence_root),
        "bootstrap": bootstrap,
        "representative_samples": {
            "single_location_business_ids": [int(v) for v in single_location_business_ids],
            "multi_branch_groups": [
                [int(v) for v in group] for group in multi_branch_groups
            ],
            "merge_pair": None if merge_pair is None else list(merge_pair),
            "merge_probe_business_id": merge_probe_business_id,
            "passes_per_business": passes,
        },
        "acquisitions": acquisitions,
        "representative_dossiers": dossier_summaries,
        "checks": report_checks,
        "production_ready": all(bool(check["passed"]) for check in report_checks),
    }
    _write_report(report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    arguments_validated = False
    try:
        _validate_arguments(args)
        arguments_validated = True
        report = run_validation_gate(
            source_db=args.source_db,
            workspace=args.workspace,
            single_location_business_ids=args.single_location_business_id,
            multi_branch_groups=args.multi_branch_group,
            merge_pair=args.merge_pair,
            merge_probe_business_id=args.merge_probe_business_id,
            passes=args.passes,
        )
    except KeyboardInterrupt:
        print("website operational validation interrupted", file=sys.stderr)
        return 130
    except Exception as exc:
        _write_fatal_report(args, exc, allow_nonempty=arguments_validated)
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
