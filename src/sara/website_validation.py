from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from .maps_backfill import backfill_maps_business_understanding
from .maps_sync import sync_maps_business_understanding
from .migrations import apply_migrations
from .storage import connect_existing, connect_readonly, upsert_business
from .understanding_vocabulary import (
    DOSSIER_DOMAIN_SEED_V1,
    seed_business_understanding_vocabulary,
)
from .website.model import CrawlConfig
from .website.surface import collect_official_website


REPORT_SCHEMA = "sara-website-operational-validation-v1"
LEGACY_TABLES = (
    "runs",
    "businesses",
    "run_businesses",
    "recovery_executions",
    "recovery_execution_bins",
)
IDENTITY_FIELDS = ("place_id", "cid", "data_id")
NULL_FACT_STATUSES = {"unknown", "not_observed", "not_applicable"}


class OperationalValidationError(RuntimeError):
    """Operational validation cannot be completed safely."""


@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    details: dict[str, Any]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        is not None
    )


def _row_dicts(conn: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    cursor = conn.execute(f'SELECT * FROM "{table}"')
    columns = [str(item[0]) for item in cursor.description or ()]
    rows = [
        {column: row[index] for index, column in enumerate(columns)}
        for row in cursor.fetchall()
    ]
    rows.sort(key=_canonical_json)
    return rows


def logical_table_fingerprint(conn: sqlite3.Connection, table: str) -> dict[str, Any]:
    if not _table_exists(conn, table):
        return {"present": False, "row_count": 0, "sha256": None}
    rows = _row_dicts(conn, table)
    payload = _canonical_json(rows).encode("utf-8")
    return {
        "present": True,
        "row_count": len(rows),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def legacy_snapshot(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    return {table: logical_table_fingerprint(conn, table) for table in LEGACY_TABLES}


def compare_legacy_snapshots(
    before: dict[str, dict[str, Any]],
    after: dict[str, dict[str, Any]],
) -> CheckResult:
    changed = {
        table: {"before": before.get(table), "after": after.get(table)}
        for table in LEGACY_TABLES
        if before.get(table) != after.get(table)
    }
    return CheckResult(
        name="legacy_collection_recovery_tables_unchanged",
        passed=not changed,
        details={"changed_tables": changed},
    )


def backup_database(source_db: str | Path, destination_db: str | Path) -> None:
    source_path = Path(source_db)
    destination_path = Path(destination_db)
    if not source_path.is_file():
        raise FileNotFoundError(f"source database does not exist: {source_path}")
    if destination_path.exists():
        raise OperationalValidationError(
            f"validation database already exists: {destination_path}"
        )
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    source = connect_readonly(source_path)
    destination = sqlite3.connect(destination_path)
    try:
        source.backup(destination)
        destination.commit()
    finally:
        destination.close()
        source.close()


def backup_existing_database(source_db: str | Path, destination_db: str | Path) -> None:
    source_path = Path(source_db)
    destination_path = Path(destination_db)
    if destination_path.exists():
        destination_path.unlink()
    source = connect_readonly(source_path)
    destination = sqlite3.connect(destination_path)
    try:
        source.backup(destination)
        destination.commit()
    finally:
        destination.close()
        source.close()


def bootstrap_understanding(conn: sqlite3.Connection) -> dict[str, Any]:
    migrations = apply_migrations(conn)
    seeded = seed_business_understanding_vocabulary(conn)
    business_count = int(conn.execute("SELECT COUNT(*) FROM businesses").fetchone()[0])
    link_count = int(
        conn.execute("SELECT COUNT(*) FROM maps_business_location_links").fetchone()[0]
    )
    backfill = None
    if business_count and link_count == 0:
        backfill = asdict(backfill_maps_business_understanding(conn))
    sync = asdict(sync_maps_business_understanding(conn))
    return {
        "migrations_applied": list(migrations),
        "predicates_seeded": list(seeded),
        "backfill": backfill,
        "sync": sync,
    }


def validate_one_to_one_maps_anchors(conn: sqlite3.Connection) -> CheckResult:
    business_count = int(conn.execute("SELECT COUNT(*) FROM businesses").fetchone()[0])
    rows = list(
        conn.execute(
            "SELECT m.business_id,m.location_id,bl.business_entity_id "
            "FROM maps_business_location_links m "
            "JOIN business_locations bl ON bl.id=m.location_id "
            "ORDER BY m.business_id"
        )
    )
    distinct_businesses = len({int(row[0]) for row in rows})
    distinct_locations = len({str(row[1]) for row in rows})
    missing = [
        int(row[0])
        for row in conn.execute(
            "SELECT b.id FROM businesses b "
            "LEFT JOIN maps_business_location_links m ON m.business_id=b.id "
            "WHERE m.business_id IS NULL ORDER BY b.id"
        )
    ]
    passed = (
        len(rows) == business_count
        and distinct_businesses == business_count
        and distinct_locations == business_count
        and not missing
    )
    return CheckResult(
        name="maps_one_to_one_understanding_anchors",
        passed=passed,
        details={
            "business_count": business_count,
            "link_count": len(rows),
            "distinct_businesses": distinct_businesses,
            "distinct_locations": distinct_locations,
            "missing_business_ids": missing,
        },
    )


def validate_multi_branch_groups(
    conn: sqlite3.Connection,
    groups: Sequence[Sequence[int]],
) -> CheckResult:
    failures: list[dict[str, Any]] = []
    inspected: list[dict[str, Any]] = []
    for group in groups:
        ids = tuple(int(value) for value in group)
        placeholders = ",".join("?" for _ in ids)
        rows = list(
            conn.execute(
                "SELECT m.business_id,bl.business_entity_id "
                "FROM maps_business_location_links m "
                "JOIN business_locations bl ON bl.id=m.location_id "
                f"WHERE m.business_id IN ({placeholders}) ORDER BY m.business_id",
                ids,
            )
        )
        mapping = {int(row[0]): str(row[1]) for row in rows}
        distinct_entities = set(mapping.values())
        item = {
            "business_ids": list(ids),
            "entity_ids": {str(key): value for key, value in mapping.items()},
        }
        inspected.append(item)
        if len(mapping) != len(ids) or len(distinct_entities) != len(ids):
            failures.append(item)
    return CheckResult(
        name="multi_branch_samples_remain_deliberately_separated",
        passed=bool(groups) and not failures,
        details={"groups_inspected": inspected, "failures": failures},
    )


def validate_fact_provenance(conn: sqlite3.Connection) -> CheckResult:
    value_statuses = ("confirmed", "single_source", "conflicted", "stale")
    placeholders = ",".join("?" for _ in value_statuses)
    unsupported_value_facts = [
        str(row[0])
        for row in conn.execute(
            "SELECT f.id FROM facts f "
            f"WHERE f.status IN ({placeholders}) "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM fact_observation_support fos "
            "  JOIN observations o ON o.id=fos.observation_id "
            "  JOIN evidence_items e ON e.id=o.evidence_id "
            "  JOIN acquisition_sessions a ON a.id=e.acquisition_session_id "
            "  JOIN sources s ON s.id=a.source_id "
            "  WHERE fos.fact_id=f.id AND fos.support_role='supports'"
            ") ORDER BY f.id",
            value_statuses,
        )
    ]
    unsupported_not_observed = [
        str(row[0])
        for row in conn.execute(
            "SELECT f.id FROM facts f "
            "WHERE f.status='not_observed' "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM fact_acquisition_support fas "
            "  JOIN acquisition_sessions a ON a.id=fas.acquisition_session_id "
            "  JOIN sources s ON s.id=a.source_id "
            "  WHERE fas.fact_id=f.id "
            "    AND fas.support_role='supports_absence' "
            "    AND a.status='complete'"
            ") ORDER BY f.id"
        )
    ]
    return CheckResult(
        name="fact_provenance_is_complete",
        passed=not unsupported_value_facts and not unsupported_not_observed,
        details={
            "value_facts_without_full_chain": unsupported_value_facts,
            "not_observed_without_complete_absence_support": unsupported_not_observed,
        },
    )


def validate_dossier_readiness_integrity(conn: sqlite3.Connection) -> CheckResult:
    mandatory = {
        seed.name for seed in DOSSIER_DOMAIN_SEED_V1 if seed.mandatory_for_initial_analysis
    }
    bad: list[dict[str, Any]] = []
    rows = list(
        conn.execute(
            "SELECT da.id,da.business_entity_id "
            "FROM finalized_dossier_assessments da "
            "WHERE da.analysis_ready=1 ORDER BY da.id"
        )
    )
    for assessment_id, entity_id in rows:
        domain_rows = list(
            conn.execute(
                "SELECT domain,state FROM dossier_domain_assessments "
                "WHERE assessment_id=? ORDER BY domain",
                (assessment_id,),
            )
        )
        actual = {str(row[0]): str(row[1]) for row in domain_rows}
        below = sorted(
            domain
            for domain in mandatory
            if actual.get(domain) not in {"sufficient", "strong", "not_applicable"}
        )
        if below:
            bad.append(
                {
                    "assessment_id": str(assessment_id),
                    "business_entity_id": str(entity_id),
                    "mandatory_domains_below_sufficient": below,
                }
            )
    return CheckResult(
        name="persisted_analysis_ready_assessments_are_internally_consistent",
        passed=not bad,
        details={
            "analysis_ready_assessments_checked": len(rows),
            "invalid_assessments": bad,
        },
    )


def _business_exists(conn: sqlite3.Connection, business_id: int) -> bool:
    return (
        conn.execute("SELECT 1 FROM businesses WHERE id=?", (business_id,)).fetchone()
        is not None
    )


def run_acquisition_samples(
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
            except BaseException as exc:
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
    return (
        CheckResult(
            name="representative_website_acquisitions_complete_without_integrity_failure",
            passed=bool(business_ids) and not failures,
            details={
                "business_ids": list(business_ids),
                "passes_per_business": passes,
                "failures": failures,
            },
        ),
        results,
    )


def _subject_ids_for_business(
    conn: sqlite3.Connection, business_id: int
) -> tuple[str, str]:
    row = conn.execute(
        "SELECT m.location_id,bl.business_entity_id "
        "FROM maps_business_location_links m "
        "JOIN business_locations bl ON bl.id=m.location_id "
        "WHERE m.business_id=?",
        (business_id,),
    ).fetchone()
    if row is None:
        raise OperationalValidationError(
            f"business {business_id} has no Understanding location link"
        )
    return str(row[1]), str(row[0])


def _evidence_ids_for_subjects(
    conn: sqlite3.Connection, subject_ids: Iterable[str]
) -> set[str]:
    ids = tuple(subject_ids)
    if not ids:
        return set()
    placeholders = ",".join("?" for _ in ids)
    return {
        str(row[0])
        for row in conn.execute(
            "SELECT DISTINCT evidence_id FROM observations "
            f"WHERE subject_id IN ({placeholders}) ORDER BY evidence_id",
            ids,
        )
    }


def _bridge_record(first: sqlite3.Row, second: sqlite3.Row) -> dict[str, Any]:
    bridge: dict[str, Any] = {}
    matched_rows: set[int] = set()
    for field in IDENTITY_FIELDS:
        left = first[field]
        right = second[field]
        values = {str(value) for value in (left, right) if value not in (None, "")}
        if len(values) > 1:
            raise OperationalValidationError(
                f"merge pair has conflicting {field} values: {sorted(values)!r}"
            )
        if values:
            value = next(iter(values))
            bridge[field] = value
            if left == value:
                matched_rows.add(int(first["id"]))
            if right == value:
                matched_rows.add(int(second["id"]))
    if len(matched_rows) != 2:
        raise OperationalValidationError(
            "merge pair cannot be bridged by complementary strong identifiers"
        )
    for field in (
        "title",
        "category",
        "address",
        "latitude",
        "longitude",
        "phone",
        "website",
        "review_rating",
        "review_count",
        "status",
    ):
        bridge[field] = first[field] if first[field] not in (None, "") else second[field]
    return bridge


def run_merge_survival_probe(
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
        passed = (
            original_remaining == 1
            and survivor_id in {first_id, second_id}
            and remaining == durable_evidence
            and observations_with_missing_evidence == 0
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
            },
        )
    except BaseException as exc:
        if conn.in_transaction:
            conn.rollback()
        return CheckResult(
            name="maps_identity_merge_preserves_understanding_evidence",
            passed=False,
            details={
                "merge_pair": None if merge_pair is None else list(merge_pair),
                "error": f"{type(exc).__name__}: {exc}",
            },
        )
    finally:
        conn.close()


def _parse_group(value: str) -> tuple[int, ...]:
    try:
        items = tuple(int(part.strip()) for part in value.split(",") if part.strip())
    except ValueError as exc:
        raise argparse.ArgumentTypeError("business IDs must be integers") from exc
    if len(items) < 2 or any(item <= 0 for item in items) or len(set(items)) != len(items):
        raise argparse.ArgumentTypeError(
            "multi-branch group must contain at least two distinct positive business IDs"
        )
    return items


def _parse_pair(value: str) -> tuple[int, int]:
    items = _parse_group(value)
    if len(items) != 2:
        raise argparse.ArgumentTypeError("merge pair must contain exactly two business IDs")
    return items[0], items[1]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sara-website-validate",
        description=(
            "Run the official-website production-readiness gate on disposable copies "
            "of a representative Sara database."
        ),
    )
    parser.add_argument("--source-db", required=True)
    parser.add_argument("--workspace", required=True)
    parser.add_argument(
        "--single-location-business-id",
        action="append",
        type=int,
        default=[],
        help="known single-location Maps business ID; specify at least two",
    )
    parser.add_argument(
        "--multi-branch-group",
        action="append",
        type=_parse_group,
        default=[],
        help="comma-separated Maps business IDs belonging to one known multi-branch brand",
    )
    parser.add_argument(
        "--merge-pair",
        type=_parse_pair,
        help="comma-separated complementary-identifier Maps business IDs for the merge-survival probe",
    )
    parser.add_argument(
        "--passes",
        type=int,
        default=2,
        help="bounded acquisition passes per representative business (default: 2)",
    )
    parser.add_argument("--pretty", action="store_true")
    return parser


def _validate_arguments(args: argparse.Namespace) -> None:
    if args.passes < 2 or args.passes > 3:
        raise OperationalValidationError("passes must be between 2 and 3")
    singles = [int(value) for value in args.single_location_business_id]
    if len(singles) < 2 or len(set(singles)) != len(singles):
        raise OperationalValidationError(
            "provide at least two distinct --single-location-business-id values"
        )
    if not args.multi_branch_group:
        raise OperationalValidationError("provide at least one --multi-branch-group")
    if args.merge_pair is None:
        raise OperationalValidationError("provide --merge-pair for the evidence-survival probe")
    workspace = Path(args.workspace)
    if workspace.exists() and any(workspace.iterdir()):
        raise OperationalValidationError(
            f"workspace must be absent or empty: {workspace}"
        )


def run_validation(
    *,
    source_db: str | Path,
    workspace: str | Path,
    single_location_business_ids: Sequence[int],
    multi_branch_groups: Sequence[Sequence[int]],
    merge_pair: tuple[int, int] | None,
    passes: int = 2,
    config: CrawlConfig | None = None,
    collector: Callable[..., Any] = collect_official_website,
) -> dict[str, Any]:
    workspace_path = Path(workspace)
    workspace_path.mkdir(parents=True, exist_ok=True)
    working_db = workspace_path / "validation.sqlite"
    probe_db = workspace_path / "merge-probe.sqlite"
    evidence_root = workspace_path / "evidence"
    report_path = workspace_path / "report.json"

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

        selected = sorted(
            set(int(value) for value in single_location_business_ids).union(
                int(value) for group in multi_branch_groups for value in group
            )
        )
        acquisition_check, acquisitions = run_acquisition_samples(
            conn,
            evidence_root=evidence_root,
            business_ids=selected,
            passes=passes,
            config=config or CrawlConfig(),
            collector=collector,
        )
        checks.append(acquisition_check)
        checks.append(compare_legacy_snapshots(source_snapshot, legacy_snapshot(conn)))
        checks.append(validate_one_to_one_maps_anchors(conn))
        checks.append(validate_fact_provenance(conn))
        checks.append(validate_dossier_readiness_integrity(conn))
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

    checks.append(run_merge_survival_probe(working_db, probe_db, merge_pair))
    ready = all(check.passed for check in checks)
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
            "passes_per_business": passes,
        },
        "acquisitions": acquisitions,
        "checks": [asdict(check) for check in checks],
        "production_ready": ready,
    }
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        _validate_arguments(args)
        report = run_validation(
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
